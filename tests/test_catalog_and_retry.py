import json

from fastapi.testclient import TestClient

from buybox_checker.catalog import build_catalog, overview, product_page, recent_products
from buybox_checker.jobs import STATUS_DONE, STATUS_INTERRUPTED, JobBusyError, JobManager
from buybox_checker.results import read_rows
from buybox_checker.web.app import create_app

from conftest import BlockingRun, fake_run_checks, make_row, wait_until_finished

HEADER = {"X-Buybox-Checker": "1"}
STATES = ["CA", "TX", "FL"]


def catalog_rows():
    """Six products, one per group, measured against Acme (who wins most often)."""
    rows = []
    for state in STATES:
        rows.append(make_row("B0ONE00001", state, seller="Acme", title="Dog bed"))
        rows.append(make_row("B0SPLIT001", state, seller="BrightHome" if state == "TX" else "Acme",
                             price="$18.49" if state == "TX" else "$19.99", title="Water fountain"))
        rows.append(make_row("B0OTHER001", state, seller="Amazon.com", title="Cooling mat"))
        rows.append(make_row("B0NONE0001", state, seller="" if state == "FL" else "Acme", title="Cat tree"))
        rows.append(make_row("B0FAIL0001", state, status="blocked" if state == "CA" else "ok", title="Glove"))
    rows.append(make_row("B0PEND0001", "CA", title="Stroller"))  # TX and FL not checked yet
    return rows


ASINS = ["B0ONE00001", "B0SPLIT001", "B0OTHER001", "B0NONE0001", "B0FAIL0001", "B0PEND0001"]


def test_products_are_grouped_against_the_top_winner():
    catalog = build_catalog(ASINS, STATES, catalog_rows())
    assert catalog.reference == "Acme" and not catalog.reference_is_store
    by_asin = {p["asin"]: p for p in catalog.products}
    assert {asin: p["group"] for asin, p in by_asin.items()} == {
        "B0ONE00001": "one", "B0SPLIT001": "split", "B0OTHER001": "other",
        "B0NONE0001": "none", "B0FAIL0001": "failed", "B0PEND0001": "pending"}
    split = by_asin["B0SPLIT001"]
    assert split["held"] == 2 and split["others"] == [{"seller": "BrightHome", "states": ["TX"], "price": "$18.49"}]
    assert split["gap"] == 1.5 and split["price"] == "$18.49–$19.99"
    assert [cell["kind"] for cell in by_asin["B0NONE0001"]["cells"]] == ["win", "win", "none"]


def test_the_users_store_becomes_the_reference_and_an_unknown_store_is_reported():
    mine = build_catalog(ASINS, STATES, catalog_rows(), store=" amazon.COM ")
    assert mine.reference == "Amazon.com" and mine.reference_is_store
    assert next(p for p in mine.products if p["asin"] == "B0OTHER001")["group"] == "one"
    unknown = build_catalog(ASINS, STATES, catalog_rows(), store="Nobody")
    assert unknown.reference == "Acme" and unknown.store_missing


def test_overview_adds_up_checks_groups_and_losses_per_state():
    catalog = build_catalog(ASINS, STATES, catalog_rows())
    summary = overview(catalog, STATES)
    assert summary["checks"] == {"total": 18, "win": 10, "other": 4, "none": 1, "failed": 1, "pending": 2}
    assert summary["sellers"][0] == {"seller": "Acme", "checks": 10}
    assert summary["groups"]["split"] == 1 and summary["groups"]["pending"] == 1
    losses = {loss["state"]: loss for loss in summary["losses"]}
    assert losses["TX"] == {"state": "TX", "lost": 2, "main_taker": "BrightHome"}
    assert losses["FL"]["lost"] == 2  # Amazon.com takes one product, nobody holds another
    assert not summary["all_clear"] and not summary["one_seller_list"]
    assert recent_products(catalog)[0]["asin"] == "B0FAIL0001"  # its last row came last


def test_a_list_won_everywhere_by_one_seller_is_all_clear():
    rows = [make_row(asin, state) for asin in ("B0AAAAAAAA", "B0BBBBBBBB") for state in STATES]
    summary = overview(build_catalog(["B0AAAAAAAA", "B0BBBBBBBB"], STATES, rows), STATES)
    assert summary["all_clear"] and summary["one_seller_list"] and summary["groups"]["one"] == 2


def test_product_pages_filter_search_sort_and_list_every_match():
    products = build_catalog(ASINS, STATES, catalog_rows()).products
    attention = product_page(products, group="attention")
    assert attention["total"] == 4 and attention["items"][0]["asin"] == "B0OTHER001"  # holds the fewest states
    assert product_page(products, query="fountain")["asins"] == ["B0SPLIT001"]
    assert product_page(products, sort="gap")["items"][0]["asin"] == "B0SPLIT001"
    paged = product_page(products, per_page=4, page=2)
    assert paged["pages"] == 2 and len(paged["items"]) == 2 and len(paged["asins"]) == 6


def test_jobs_are_saved_and_come_back_after_a_restart(tmp_path):
    manager = JobManager(run_checks=fake_run_checks, jobs_dir=tmp_path / "jobs")
    job = manager.start(["B0BCMLGPYB"], {"NY": "10001", "CA": "90012"})
    wait_until_finished(job)
    reloaded = JobManager(run_checks=fake_run_checks, jobs_dir=tmp_path / "jobs").get(job.id)
    assert reloaded.status == STATUS_DONE and len(reloaded.latest_rows()) == 2

    saved = tmp_path / "jobs" / f"{job.id}.json"
    saved.write_text(json.dumps({**json.loads(saved.read_text()), "status": "running"}))
    interrupted = JobManager(run_checks=fake_run_checks, jobs_dir=tmp_path / "jobs").get(job.id)
    assert interrupted.status == STATUS_INTERRUPTED and "Retry" in interrupted.error


def test_retry_checks_only_failed_pairs_and_keeps_one_row_per_pair():
    def first_run(asins, states, on_result, *, local_sessions, remote_urls, stop_event, pairs=None):
        for state, zipcode in states.items():
            on_result(make_row(asins[0], state, zipcode, status="blocked" if state == "CA" else "ok"))

    retried = []

    def second_run(asins, states, on_result, *, local_sessions, remote_urls, stop_event, pairs=None):
        retried.extend(pairs)
        fake_run_checks(asins, states, on_result, local_sessions=local_sessions, remote_urls=remote_urls,
                        stop_event=stop_event, pairs=pairs)

    manager = JobManager(run_checks=first_run)
    job = manager.start(["B0BCMLGPYB"], {"NY": "10001", "CA": "90012"})
    wait_until_finished(job)
    assert job.retry_pairs() == [("B0BCMLGPYB", "CA")]
    manager._run_checks = second_run
    manager.retry(job.id)
    wait_until_finished(job)
    assert retried == [("B0BCMLGPYB", "CA")] and job.retry_pairs() == []
    assert len(job.rows) == 3 and len(job.latest_rows()) == 2
    assert job.run_total == 1 and job.run_done() == 1  # progress counts only the retried check
    assert sorted(row["status"] for row in read_rows(job.csv_path)) == ["ok", "ok"]  # CSV rewritten


def test_a_busy_manager_says_what_is_running():
    manager = JobManager(run_checks=BlockingRun())
    job = manager.start(["B0BCMLGPYB", "B09B8V1LZ3"], {"NY": "10001"})
    try:
        manager.start(["B09B8V1LZ3"], {"NY": "10001"})
        raise AssertionError("expected JobBusyError")
    except JobBusyError as error:
        assert "2 product(s) in 1 state(s)" in str(error)
    manager.cancel(job.id)
    wait_until_finished(job)


def test_web_lists_products_and_one_product_and_retries():
    manager = JobManager(run_checks=fake_run_checks)
    client = TestClient(create_app(manager=manager))
    started = client.post("/api/jobs", headers=HEADER, data={"text": "B0BCMLGPYB B09B8V1LZ3", "states": "NY CA"})
    job_id = started.json()["job"]["id"]
    wait_until_finished(manager.get(job_id))
    summary = client.get(f"/api/jobs/{job_id}", params={"store": "acme"}).json()
    assert summary["overview"]["reference_is_store"] and summary["overview"]["all_clear"]
    assert summary["retryable"] == 0 and len(summary["recent"]) == 2

    page = client.get(f"/api/jobs/{job_id}/products", params={"per_page": 1, "q": "b0bc"}).json()
    assert page["total"] == 1 and page["items"][0]["asin"] == "B0BCMLGPYB"
    assert client.get(f"/api/jobs/{job_id}/products", params={"group": "nope"}).status_code == 400
    product = client.get(f"/api/jobs/{job_id}/products/B09B8V1LZ3").json()
    assert product["product"]["held"] == 2 and len(product["rows"]) == 2
    assert client.get(f"/api/jobs/{job_id}/products/B000000000").status_code == 404
    assert client.post(f"/api/jobs/{job_id}/retry", headers=HEADER).status_code == 400  # nothing failed
    config = client.get("/api/config").json()
    assert config["max_asins"] == 500 and config["contact_techark"] is False  # the defaults


def test_hosted_site_limits_asins_and_points_to_techark(monkeypatch):
    monkeypatch.setenv("BUYBOX_CHECKER_MAX_ASINS", "5")
    monkeypatch.setenv("BUYBOX_CHECKER_CONTACT_TECHARK", "1")
    client = TestClient(create_app(manager=JobManager(run_checks=BlockingRun())))
    config = client.get("/api/config").json()
    assert config["max_asins"] == 5 and config["contact_techark"] is True
    too_many = client.post("/api/jobs", headers=HEADER, data={"text": " ".join(f"B0{i:08d}" for i in range(6))})
    assert too_many.status_code == 400 and "contact TECHARK" in too_many.json()["detail"]
    plain = TestClient(create_app(manager=JobManager(run_checks=BlockingRun()), max_asins=5, contact_techark=False))
    detail = plain.post("/api/jobs", headers=HEADER, data={"text": " ".join(f"B0{i:08d}" for i in range(6))}).json()
    assert "at most 5" in detail["detail"] and "TECHARK" not in detail["detail"]

