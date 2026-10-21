"""Engine behavior that needs no browser: the shared IP throttle and the 'try again later' round."""

import threading

from buybox_checker import engine
from buybox_checker.engine import IpThrottle, Session


def test_throttle_pauses_the_ip_when_several_zipcodes_are_blocked_and_backs_off():
    throttle = IpThrottle("this computer")
    throttle.record("blocked", "35203")  # nothing succeeded yet: assume the IP
    assert (throttle._cooldown, throttle._slowdown) == (30, 2.0)
    throttle.record("timeout", "99501")  # another session reporting the same pushback during the cooldown
    assert (throttle._cooldown, throttle._slowdown) == (30, 2.0)

    throttle._resume_at = 0  # the cooldown is over, and Amazon pushes back on two zipcodes again
    throttle.record("error", "90012")
    assert (throttle._cooldown, throttle._slowdown) == (60, 4.0)

    throttle.record("ok", "10001")  # success: the next pushback starts from 30 s again, checks speed up a little
    assert (throttle._cooldown, throttle._slowdown) == (0, 3.5)
    throttle.record("zip_mismatch", "10001")  # not a pushback: nothing changes
    assert (throttle._cooldown, throttle._slowdown) == (0, 3.5)


def test_throttle_counts_no_success_yet_even_right_after_boot(monkeypatch):
    monkeypatch.setattr(engine.time, "monotonic", lambda: 5.0)  # a machine up for 5 s, like a fresh CI runner
    throttle = IpThrottle("this computer")
    throttle.record("blocked", "35203")
    assert throttle._cooldown == 30  # nothing has succeeded: the IP pauses


def test_throttle_ignores_one_flagged_zipcode_while_others_succeed():
    throttle = IpThrottle("this computer")
    throttle.record("ok", "99501")
    throttle.record("blocked", "35203")
    throttle.record("ok", "90012")
    throttle.record("blocked", "35203")
    assert (throttle._cooldown, throttle._slowdown, throttle._resume_at) == (0, 0, 0)
    throttle.record("blocked", "10001")  # a second zipcode is blocked too: now it's the IP
    assert throttle._cooldown == 30


class FakeBrowser:
    def is_connected(self) -> bool:
        return True


def test_a_check_that_keeps_failing_is_tried_again_later_in_the_run(monkeypatch):
    monkeypatch.setattr(engine, "CHECK_DELAY_SECONDS", (0, 0))
    monkeypatch.setattr(engine, "COOLDOWN_SECONDS", 0)
    monkeypatch.setattr(engine, "SLOWDOWN_STEP_SECONDS", 0)
    calls = []

    cleared = []
    monkeypatch.setattr(engine, "clear_saved_session", cleared.append)

    def fake_check_once(session, asin, zipcode):
        calls.append(zipcode)
        first_round = calls.count("10001") <= engine.MAX_ATTEMPTS
        if zipcode == "10001" and first_round:
            return {"status": "blocked"}
        return {"status": "ok", "has_buybox": "yes"}

    monkeypatch.setattr(engine, "check_once", fake_check_once)
    checks = engine.build_check_queue(["B0BCMLGPYB"], {"NY": "10001", "CA": "90012"})
    rows = []
    session = Session(FakeBrowser(), "test agent", IpThrottle("test"))
    engine.process_checks(session, checks, rows.append, threading.Event())

    assert [(row["state"], row["status"], row["attempts"]) for row in rows] == [("CA", "ok", 1), ("NY", "ok", 3)]
    assert calls == ["10001", "10001", "90012", "10001"]  # NY failed twice, CA ran, then NY again later


def test_a_zipcode_still_blocked_after_every_round_gets_a_fresh_session_next_run(monkeypatch):
    monkeypatch.setattr(engine, "CHECK_DELAY_SECONDS", (0, 0))
    monkeypatch.setattr(engine, "COOLDOWN_SECONDS", 0)
    monkeypatch.setattr(engine, "SLOWDOWN_STEP_SECONDS", 0)
    cleared = []
    monkeypatch.setattr(engine, "clear_saved_session", cleared.append)
    monkeypatch.setattr(engine, "check_once", lambda session, asin, zipcode: {"status": "blocked"})
    rows = []
    session = Session(FakeBrowser(), "test agent", IpThrottle("test"))
    engine.process_checks(session, engine.build_check_queue(["B0BCMLGPYB"], {"AL": "35203"}), rows.append,
                          threading.Event())
    assert [(row["status"], row["attempts"]) for row in rows] == [("blocked", 4)]
    assert cleared == ["35203"]
