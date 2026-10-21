"""Proxies: reading BUYBOX_CHECKER_PROXIES, keeping each zipcode on one proxy, and caching cookies per proxy."""

import threading
import time

import pytest

from buybox_checker import engine, zipcode
from buybox_checker.engine import IpThrottle, Route, Session
from buybox_checker.proxies import Proxy, parse_proxy, proxies_from_env


def test_proxies_are_read_in_webshare_list_url_and_plain_formats(monkeypatch):
    monkeypatch.setenv("BUYBOX_CHECKER_PROXIES", " 1.2.3.4:6540:user:pa:ss , http://u%40x:p@5.6.7.8:80,9.9.9.9:3128,")
    with pytest.raises(ValueError, match="host:port:username:password"):
        proxies_from_env()  # a password containing ":" can't be told apart: use the URL format for it

    monkeypatch.setenv("BUYBOX_CHECKER_PROXIES", " 1.2.3.4:6540:user:secret , http://u%40x:p@5.6.7.8:80,9.9.9.9:3128,")
    assert proxies_from_env() == [Proxy("1.2.3.4", 6540, "user", "secret"), Proxy("5.6.7.8", 80, "u@x", "p"),
                                  Proxy("9.9.9.9", 3128)]
    assert Proxy("1.2.3.4", 6540, "user", "secret").playwright_settings() == {
        "server": "http://1.2.3.4:6540", "username": "user", "password": "secret"}
    assert Proxy("9.9.9.9", 3128).playwright_settings() == {"server": "http://9.9.9.9:3128"}


def test_a_bad_proxy_is_refused_without_showing_its_password():
    with pytest.raises(ValueError) as error:
        parse_proxy("1.2.3.4:port:user:secret")
    assert "secret" not in str(error.value)


def test_each_check_goes_through_the_proxy_free_soonest_preferring_one_with_cookies(tmp_path, monkeypatch):
    monkeypatch.setattr(zipcode, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(engine, "has_saved_session", zipcode.has_saved_session)
    routes = [Route(Proxy(f"10.0.0.{n}", 8000), IpThrottle(f"proxy {n}")) for n in range(3)]
    session = Session(FakeBrowser(), "test agent", routes)
    zipcode.state_file_for("10001", routes[2].proxy).write_text("{}")
    assert session.pick_route("10001") is routes[2]  # the others would first need a browser to make cookies

    routes[2].throttle._next_start = time.monotonic() + 60  # busy for a minute: making cookies elsewhere is quicker
    assert session.pick_route("10001") is routes[0]
    routes[0].throttle._resume_at = float("inf")  # cooling down after a pushback
    assert session.pick_route("10001") is routes[1]

    monkeypatch.setattr(engine, "CHECK_DELAY_SECONDS", (0, 0))
    opened = []
    monkeypatch.setattr(engine, "open_context_for_zipcode",
                        lambda browser, zipcode, user_agent, proxy: opened.append((zipcode, proxy)) or FakeContext())
    monkeypatch.setattr(engine, "inspect_product_page", lambda page, asin, zipcode: {"status": "ok"})
    engine.check_with_retries(session, "B0BCMLGPYB", "NY", "10001", threading.Event())
    assert opened == [("10001", routes[1].proxy)]
    assert [route.throttle._last_success_at > float("-inf") for route in routes] == [False, True, False]


def test_turns_on_one_ip_are_shared_by_every_session_and_run(monkeypatch):
    monkeypatch.setattr(engine, "CHECK_DELAY_SECONDS", (3, 3))
    waits = []

    class RecordingEvent:
        def wait(self, seconds: float) -> bool:
            waits.append(round(seconds))
            return False

    throttle = IpThrottle("proxy 1")
    for _ in range(3):  # three sessions, maybe of three runs, asking at the same moment
        assert throttle.wait_turn(RecordingEvent())
    assert waits == [0, 3, 6]  # one check starts every 3 s on this IP


def test_cookies_are_cached_and_cleared_per_proxy(tmp_path, monkeypatch):
    monkeypatch.setattr(zipcode, "SESSIONS_DIR", tmp_path)
    proxy = Proxy("1.2.3.4", 6540, "user", "secret")
    assert zipcode.state_file_for("10001", proxy).name == "zip_10001_via_1.2.3.4_6540.json"
    files = [zipcode.state_file_for("10001"), zipcode.state_file_for("10001", proxy),
             zipcode.state_file_for("10001", Proxy("5.6.7.8", 80)), zipcode.state_file_for("90012", proxy)]
    for file in files:
        file.write_text("{}")
    zipcode.clear_saved_session("10001", proxy)
    assert [file.exists() for file in files] == [True, False, True, True]
    zipcode.clear_all_saved_sessions("10001")
    assert [file.exists() for file in files] == [False, False, False, True]


def test_warm_up_makes_the_missing_cookies_on_every_proxy(tmp_path, monkeypatch):
    monkeypatch.setattr(zipcode, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(engine, "has_saved_session", zipcode.has_saved_session)
    monkeypatch.setattr(engine, "CHECK_DELAY_SECONDS", (0, 0))
    monkeypatch.setattr(engine.http_page, "is_available", lambda: True)
    monkeypatch.setenv("BUYBOX_CHECKER_PROXIES", "10.0.0.1:8000:u:p,10.0.0.2:8000:u:p")
    zipcode.state_file_for("10001", Proxy("10.0.0.1", 8000, "u", "p")).write_text("{}")
    made = []
    monkeypatch.setattr(engine, "zipcode_cookies", lambda zipcode, proxy, is_headed: made.append((zipcode, proxy.host)))

    engine.warm_up(["10001", "90012"])
    assert sorted(made) == [("10001", "10.0.0.2"), ("90012", "10.0.0.1"), ("90012", "10.0.0.2")]


class FakeContext:
    def set_default_timeout(self, timeout_ms: int) -> None:
        pass

    def new_page(self) -> None:
        return None

    def close(self) -> None:
        pass


class FakeBrowser:
    def is_connected(self) -> bool:
        return True
