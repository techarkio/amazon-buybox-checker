import pytest
from fastapi.testclient import TestClient

from buybox_checker.jobs import STATUS_CANCELLED, STATUS_DONE, JobBusyError, JobManager
from buybox_checker.web.app import create_app

from conftest import BlockingRun, fake_run_checks, wait_until_finished

HEADER = {"X-Buybox-Checker": "1"}


def test_job_collects_rows_writes_csv_and_summarizes():
    manager = JobManager(run_checks=fake_run_checks)
    job = manager.start(["B0BCMLGPYB", "B09B8V1LZ3"], {"NY": "10001", "CA": "90012"})
    wait_until_finished(job)
    data = job.to_dict()
    assert data["status"] == STATUS_DONE
    assert data["done"] == data["total"] == 4
    assert job.csv_path.exists()
    assert [s["with_buybox"] for s in data["summaries"]] == [2, 2]


def test_several_jobs_run_at_once_up_to_the_limit():
    run = BlockingRun()
    manager = JobManager(run_checks=run, max_running=2)
    first = manager.start(["B0BCMLGPYB"], {"NY": "10001"})
    second = manager.start(["B09B8V1LZ3"], {"CA": "90012"})
    with pytest.raises(JobBusyError, match="2 checks are running"):
        manager.start(["B07FNDF2R7"], {"TX": "77002"})
    manager.cancel(first.id)
    wait_until_finished(first)
    third = manager.start(["B07FNDF2R7"], {"TX": "77002"})  # a place freed up
    run.release.set()
    wait_until_finished(second)
    wait_until_finished(third)
    assert (second.status, third.status) == (STATUS_DONE, STATUS_DONE)


def test_only_one_job_runs_at_a_time_and_cancel_stops_it():
    run = BlockingRun()
    manager = JobManager(run_checks=run)
    job = manager.start(["B0BCMLGPYB"], {"NY": "10001"})
    with pytest.raises(JobBusyError):
        manager.start(["B09B8V1LZ3"], {"NY": "10001"})
    manager.cancel(job.id)
    wait_until_finished(job)
    assert job.status == STATUS_CANCELLED
    assert manager.latest() is job


def test_web_api_runs_a_job_from_text_and_csv_upload():
    client = TestClient(create_app(manager=JobManager(run_checks=fake_run_checks)))
    response = client.post(
        "/api/jobs", headers=HEADER,
        data={"text": "https://www.amazon.com/dp/B0BCMLGPYB https://amzn.to/abc", "states": "ny, ca"},
        files={"file": ("asins.csv", b"asin\nB09B8V1LZ3\n", "text/csv")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job"]["asins"] == ["B0BCMLGPYB", "B09B8V1LZ3"]
    assert body["skipped_short_links"] == ["https://amzn.to/abc"]
    assert "rows" not in body["job"] and body["job"]["overview"]["products"] == 2  # a summary, never every row

    job_id = body["job"]["id"]
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] != "running":
            break
    assert job["done"] == 4 and job["overview"]["groups"]["one"] == 2
    assert client.get("/api/jobs/latest").json()["id"] == job_id
    csv_response = client.get(f"/api/jobs/{job_id}/csv")
    assert csv_response.status_code == 200 and "B09B8V1LZ3" in csv_response.text


def test_web_api_lists_past_checks_newest_first():
    manager = JobManager(run_checks=fake_run_checks)
    client = TestClient(create_app(manager=manager))
    assert client.get("/api/jobs").json() == {"jobs": []}
    first = manager.start(["B0BCMLGPYB"], {"NY": "10001"})
    wait_until_finished(first)
    second = manager.start(["B0BCMLGPYB", "B09B8V1LZ3", "B000000001", "B000000002"], {"NY": "10001", "CA": "90012"})
    wait_until_finished(second)
    jobs = client.get("/api/jobs").json()["jobs"]
    assert [job["id"] for job in jobs] == [second.id, first.id]
    assert jobs[0]["products"] == 4 and jobs[0]["asins"] == ["B0BCMLGPYB", "B09B8V1LZ3", "B000000001"]
    assert (jobs[0]["checked"], jobs[0]["total"], jobs[0]["states"]) == (8, 8, ["NY", "CA"])

def test_web_api_rejects_bad_requests():
    client = TestClient(create_app(manager=JobManager(run_checks=BlockingRun())))
    assert client.post("/api/jobs", data={"text": "B0BCMLGPYB"}).status_code == 403  # no app header
    assert client.post("/api/jobs", headers=HEADER, data={"text": "nothing here"}).status_code == 400
    assert client.post("/api/jobs", headers=HEADER, data={"text": "B0BCMLGPYB", "states": "ZZ"}).status_code == 400
    assert client.post("/api/jobs", headers=HEADER,
                       data={"text": "B0BCMLGPYB", "local_sessions": "99"}).status_code == 400
    assert client.post("/api/jobs", headers=HEADER, data={"text": "B0BCMLGPYB"}).status_code == 200
    assert client.post("/api/jobs", headers=HEADER, data={"text": "B09B8V1LZ3"}).status_code == 409  # busy
    assert client.get("/api/jobs/unknown").status_code == 404
    assert client.get("/").status_code == 200 and "Amazon Buybox Checker" in client.get("/").text


def test_public_site_never_hands_out_someone_elses_check():
    manager = JobManager(run_checks=BlockingRun())
    client = TestClient(create_app(manager=manager, public=True))
    assert client.get("/api/config").json()["public"] is True
    started = client.post("/api/jobs", headers=HEADER, data={"text": "B0BCMLGPYB"}).json()["job"]
    assert client.get("/api/jobs/latest").status_code == 404  # no browsing the latest check
    assert client.get("/api/jobs").status_code == 404  # nor the past checks
    assert client.get(f"/api/jobs/{started['id']}").status_code == 200  # its own link still works
    busy = client.post("/api/jobs", headers=HEADER, data={"text": "B09B8V1LZ3"})
    assert busy.status_code == 409 and busy.json()["detail"].endswith("Please try again in a few minutes.")
    manager.cancel(started["id"])


def test_web_password_protects_everything_but_the_health_check():
    import base64

    from buybox_checker.web.app import is_authorized

    def basic(text):
        return "Basic " + base64.b64encode(text.encode()).decode()

    client = TestClient(create_app(manager=JobManager(run_checks=fake_run_checks), password="s3cret"))
    assert client.get("/api/health").status_code == 200
    assert client.get("/").status_code == 401
    assert client.get("/api/config", headers={"Authorization": basic("me:wrong")}).status_code == 401
    assert client.get("/api/config", headers={"Authorization": basic("anyone:s3cret")}).status_code == 200
    assert is_authorized("Basic !!not-base64", "s3cret") is False
    assert is_authorized(None, "s3cret") is False
