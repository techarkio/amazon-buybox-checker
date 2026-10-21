"""Shared test helpers: a fake engine, so jobs and the web API run without a browser."""

import threading

import pytest

from buybox_checker import jobs


def make_row(asin: str, state: str, zipcode: str = "00000", seller: str = "Acme", status: str = "ok",
             price: str = "$19.99", title: str = "Test product") -> dict:
    """Build a result row like the engine reports; seller "" means the page has no buybox."""
    has_buybox = status == "ok" and bool(seller)
    return {
        "asin": asin, "state": state, "zipcode": zipcode, "status": status, "attempts": 1,
        "has_buybox": "yes" if has_buybox else ("no" if status == "ok" else ""),
        "sold_by": seller if has_buybox else "", "price": price if has_buybox else "",
        "title": title if status == "ok" else "",
    }


def fake_run_checks(asins, states, on_result, *, local_sessions, remote_urls, stop_event, pairs=None):
    """Report an 'ok' row for every ASIN x state pair (or the given pairs), like a run Amazon never blocks."""
    for asin, state in pairs if pairs is not None else [(asin, state) for asin in asins for state in states]:
        on_result(make_row(asin, state, states[state]))


class BlockingRun:
    """A fake engine that keeps running until released or cancelled, to test busy/cancel behavior."""

    def __init__(self) -> None:
        self.release = threading.Event()

    def __call__(self, asins, states, on_result, *, local_sessions, remote_urls, stop_event, pairs=None):
        while not (self.release.is_set() or stop_event.is_set()):
            stop_event.wait(0.01)


@pytest.fixture(autouse=True)
def results_in_tmp(tmp_path, monkeypatch):
    """Write job CSV files into a temporary folder instead of the user's data folder."""
    monkeypatch.setattr(jobs, "RESULTS_DIR", tmp_path / "results")


def wait_until_finished(job, timeout: float = 5) -> None:
    """Wait for a background job to leave the 'running' status."""
    finished = threading.Event()
    for _ in range(int(timeout * 100)):
        if job.status != jobs.STATUS_RUNNING:
            return
        finished.wait(0.01)
    raise AssertionError("job did not finish")
