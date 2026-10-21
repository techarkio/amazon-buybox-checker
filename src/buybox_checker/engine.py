"""Run every ASIN x state check across parallel sessions, local and remote.

Input:  ASINs, {state code: zipcode}, a callback for finished rows, and the session settings.
Output: on_result(row) is called once per ASIN x state pair, from the session threads, as soon as
        that check is done; checks no session could do are reported at the end as "not_checked".
Constraints: Amazon blocks heavy traffic, so checks on one IP address start a few seconds apart, however
        many sessions and runs share it. When Amazon pushes back (block page, timeout, failed location
        setup), every session on that IP address pauses together and then goes slower; a failed check
        is retried later in the run; a session stops when Amazon keeps blocking it. Captchas are reported,
        never solved ("Continue shopping" pages, which ask nothing, are clicked through once). Playwright
        objects are not thread-safe, so every session runs in its own thread with its own Playwright and
        browser. When BUYBOX_CHECKER_PROXIES lists proxies, each local check goes through the proxy that
        can start it soonest, and each proxy is paced as its own IP. Cookies are kept per zipcode and proxy,
        so they always stay on the IP that got them.
        Local sessions read product pages over HTTP with each zipcode's cached cookies (see http_page.py)
        and start a browser only to make those cookies: when a zipcode has none yet, or Amazon stops
        accepting them. Remote sessions, and local ones where curl_cffi does not load, use their browser.
"""

import logging
import queue
import random
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime
from urllib.parse import urlparse

from playwright.sync_api import Browser, Playwright, sync_playwright

from buybox_checker import http_page
from buybox_checker.page import (
    STATUS_BLOCKED,
    STATUS_NEW_SESSION_NEEDED,
    STATUS_NOT_FOUND,
    STATUS_OK,
    STATUS_TIMEOUT,
    STATUS_ZIP_MISMATCH,
    inspect_product_html,
    inspect_product_page,
)
from buybox_checker.proxies import Proxy, proxies_from_env
from buybox_checker.results import STATUS_ERROR, STATUS_NOT_CHECKED
from buybox_checker.zipcode import (
    clear_all_saved_sessions,
    clear_saved_session,
    has_saved_session,
    load_saved_cookies,
    open_context_for_zipcode,
)

logger = logging.getLogger(__name__)

DEFAULT_LOCAL_SESSIONS = 2
SECONDS_PER_CHECK = 6  # one check over HTTP plus the wait for its IP's turn, per session (a browser check: 7-9)
MAX_ATTEMPTS = 2  # tries in a row for one check
RETRY_ROUNDS = 2  # a check that still fails goes back to the end of the queue once, to be tried again later
CHECK_DELAY_SECONDS = (2, 4)  # random gap between check starts on one IP, shared by all its sessions and runs
NEW_SESSION_SECONDS = 15  # about the time a browser takes to make a zipcode's cookies on a proxy
COOLDOWN_SECONDS = 30  # pause for every session on an IP after Amazon pushes back; doubles each time
MAX_COOLDOWN_SECONDS = 300
SLOWDOWN_STEP_SECONDS = 2.0  # extra pause per check added after each pushback, worn off by successes
MAX_SLOWDOWN_SECONDS = 20.0
MAX_FAILED_CHECKS_IN_A_ROW = 3  # then the session stops: Amazon is probably blocking its IP
FLAG_WINDOW_SECONDS = 120  # how far back the throttle looks to tell "one zipcode's session" from "the whole IP"
ACTION_TIMEOUT_MS = 5_000
REMOTE_CONNECT_TIMEOUT_MS = 30_000
ERROR_TEXT_LIMIT = 300
FINAL_STATUSES = {STATUS_OK, STATUS_NOT_FOUND}  # retrying would not change these
PUSHBACK_STATUSES = {STATUS_BLOCKED, STATUS_TIMEOUT, STATUS_ERROR}  # signs that Amazon is slowing us down

COOKIE_BROWSERS = 2  # HTTP sessions: browsers open at once, across all runs, to make cookies
BROWSERS_FOR_COOKIES = threading.BoundedSemaphore(COOKIE_BROWSERS)
_zip_locks: dict[tuple[str, str], threading.Lock] = {}
_throttles: dict[str, "IpThrottle"] = {}
_registry_lock = threading.Lock()

ResultCallback = Callable[[dict], None]


def zip_lock(zipcode: str, proxy: Proxy | None = None) -> threading.Lock:
    """Return the lock that one session at a time holds to create, read or clear a zipcode's cookies on a proxy."""
    with _registry_lock:
        return _zip_locks.setdefault((zipcode, proxy.name if proxy else ""), threading.Lock())


def throttle_for(ip_name: str) -> "IpThrottle":
    """Return the throttle of an IP address, shared by every run in this process."""
    with _registry_lock:
        return _throttles.setdefault(ip_name, IpThrottle(ip_name))


class IpThrottle:
    """Paces all sessions that share one IP address, and slows them down together when Amazon pushes back.

    Amazon rate-limits by IP, so checks on one IP start a random 2-4 s apart, whichever session or run asks.
    When checks for several zipcodes are blocked, the other sessions on the
    same IP are about to be too: every session on the IP pauses (30 s, doubling while pushbacks continue,
    up to 5 min) and then waits a little longer before each check; successes wear that extra wait off.
    When only one zipcode keeps being blocked while others on the IP succeed, Amazon has flagged that
    zipcode's session, not the IP, so nobody pauses for it.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._lock = threading.Lock()
        self._resume_at = 0.0  # time.monotonic() before which no session on this IP starts a check
        self._next_start = 0.0  # time.monotonic() of the next free turn on this IP
        self._cooldown = 0.0
        self._slowdown = 0.0
        self._last_success_at = float("-inf")  # no success yet (0.0 would be "at boot" on a monotonic clock)
        self._blocked_at: dict[str, float] = {}  # zipcode -> when a check for it was last pushed back

    def next_start_at(self) -> float:
        """Return when (time.monotonic()) the next check on this IP could start."""
        with self._lock:
            return max(time.monotonic(), self._resume_at, self._next_start)

    def wait_turn(self, stop_event: threading.Event) -> bool:
        """Take the next turn on this IP and wait for it; return False if the run was cancelled meanwhile."""
        with self._lock:
            start = max(time.monotonic(), self._resume_at, self._next_start)
            self._next_start = start + random.uniform(*CHECK_DELAY_SECONDS) + self._slowdown
        return not stop_event.wait(max(0.0, start - time.monotonic()))

    def record(self, status: str, zipcode: str) -> None:
        """Note a check attempt's outcome: speed back up after a success, pause everyone after an IP pushback."""
        now = time.monotonic()
        with self._lock:
            if status not in PUSHBACK_STATUSES:
                if status in FINAL_STATUSES:
                    self._last_success_at = now
                    self._cooldown = 0.0
                    self._slowdown = max(0.0, self._slowdown - SLOWDOWN_STEP_SECONDS / 4)
                return
            self._blocked_at[zipcode] = now
            if not self._is_ip_pushback(now):
                logger.warning("Only zipcode %s keeps being blocked on %s: its session looks flagged, not the IP",
                               zipcode, self.name)
                return
            if now < self._resume_at:
                return  # already cooling down: other sessions are reporting the same pushback
            self._cooldown = min(MAX_COOLDOWN_SECONDS, max(COOLDOWN_SECONDS, self._cooldown * 2))
            self._slowdown = min(MAX_SLOWDOWN_SECONDS, self._slowdown + SLOWDOWN_STEP_SECONDS)
            self._resume_at = time.monotonic() + self._cooldown
            cooldown, slowdown = self._cooldown, self._slowdown
        logger.warning("Amazon pushed back on %s: its sessions pause %d s, then wait %.1f s longer per check",
                       self.name, cooldown, slowdown)

    def _is_ip_pushback(self, now: float) -> bool:
        """Tell whether recent pushbacks hit the IP (several zipcodes, or no recent success) or one session."""
        recent_blocks = [z for z, at in self._blocked_at.items() if now - at < FLAG_WINDOW_SECONDS]
        has_recent_success = now - self._last_success_at < FLAG_WINDOW_SECONDS
        return len(recent_blocks) >= 2 or not has_recent_success


class Route:
    """Where checks leave from: a proxy, or the session's own IP (proxy None), with the throttle of that IP."""

    def __init__(self, proxy: Proxy | None, throttle: IpThrottle) -> None:
        self.proxy = proxy
        self.throttle = throttle


def run_checks(asins: list[str], states: dict[str, str], on_result: ResultCallback, *,
               local_sessions: int = DEFAULT_LOCAL_SESSIONS, remote_urls: list[str] | None = None,
               is_headed: bool = False, stop_event: threading.Event | None = None,
               pairs: list[tuple[str, str]] | None = None) -> None:
    """Check every ASIN in every state (or only the given (ASIN, state) pairs) with all sessions.

    Each finished row is reported to on_result.
    """
    remote_urls = remote_urls or []
    if local_sessions < 0 or (local_sessions == 0 and not remote_urls):
        raise ValueError("Need at least one session: one or more local sessions, or a remote ws:// URL")
    checks = build_check_queue(asins, states, pairs)
    stop_event = stop_event or threading.Event()
    sessions = [(f"local-{number}", None) for number in range(1, local_sessions + 1)]
    sessions += [(f"remote-{number}", url) for number, url in enumerate(remote_urls, start=1)]
    proxy_routes = [Route(proxy, throttle_for(f"proxy {proxy.name}")) for proxy in proxies_from_env()]
    if proxy_routes:
        logger.info("Local sessions go through %d proxies", len(proxy_routes))
    if local_sessions and not http_page.is_available():
        logger.warning("curl_cffi does not load (%s): local sessions read every page in a browser",
                       http_page.UNAVAILABLE_REASON)
    threads = []
    for name, url in sessions:
        if proxy_routes and not url:
            routes = proxy_routes  # shared by all local sessions, like the IPs behind them
        else:  # one throttle per IP address: all local sessions share one, each remote machine has its own
            routes = [Route(None, throttle_for(urlparse(url).hostname if url else "this computer"))]
        threads.append(threading.Thread(target=run_session, name=name, daemon=True,
                                        args=(url, checks, on_result, is_headed, stop_event, routes)))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    report_not_checked(checks, on_result)


def build_check_queue(asins: list[str], states: dict[str, str],
                      pairs: list[tuple[str, str]] | None = None) -> queue.Queue:
    """Put every ASIN x state pair (or only the given pairs) in one queue that all sessions take work from."""
    checks = queue.Queue()
    for asin, state in pairs if pairs is not None else [(asin, state) for asin in asins for state in states]:
        checks.put((asin, state, states[state], 1))  # 1 = first round
    return checks


def run_session(remote_url: str | None, checks: queue.Queue, on_result: ResultCallback,
                is_headed: bool, stop_event: threading.Event, routes: list[Route]) -> None:
    """Run one session (local or remote) in this thread until there is no work left for it."""
    if not remote_url and http_page.is_available():
        logger.info("Session started on this computer: product pages over HTTP, a browser only to set zipcodes")
        process_checks(Session(None, "", routes, is_headed), checks, on_result, stop_event)
        return
    where = remote_url or "this computer"
    with sync_playwright() as playwright:
        try:
            browser = open_browser(playwright, remote_url, is_headed)
            user_agent = real_user_agent(browser)
        except Exception as error:
            logger.error("Could not start a session on %s: %s%s", where, short_error(error),
                         "" if remote_url else missing_browser_hint(error))
            return
        logger.info("Session started on %s", where)
        process_checks(Session(browser, user_agent, routes), checks, on_result, stop_event)
        browser.close()


def open_browser(playwright: Playwright, remote_url: str | None, is_headed: bool) -> Browser:
    """Start a browser on this computer, or connect to one on a remote Playwright server."""
    if remote_url:
        return playwright.chromium.connect(remote_url, timeout=REMOTE_CONNECT_TIMEOUT_MS)
    return playwright.chromium.launch(headless=not is_headed)


def missing_browser_hint(error: Exception) -> str:
    """Say how to install Chromium when this Playwright's build is missing.

    Each Playwright version needs its own Chromium build, so the command names this exact Python: a bare
    `playwright install` can run another environment's Playwright and install the wrong build.
    """
    if "Executable doesn't exist" not in str(error):
        return ""
    return f". Chromium is not installed for this Playwright. Run: {sys.executable} -m playwright install chromium"


def real_user_agent(browser: Browser) -> str:
    """Return the browser's own user agent (its real Chrome version and OS), minus the 'Headless' marker.

    The zipcode setup and every page then describe the same browser that actually runs, instead of a
    fixed string whose version and OS don't match it, which is an easy bot signal.
    """
    context = browser.new_context()
    try:
        return context.new_page().evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome")
    finally:
        context.close()


class Session:
    """One session: its browser (None: product pages over HTTP), its user agent, and the routes its checks use."""

    def __init__(self, browser: Browser | None, user_agent: str, routes: list[Route], is_headed: bool = False) -> None:
        self.browser = browser
        self.user_agent = user_agent
        self.routes = routes
        self.is_headed = is_headed  # how to show the browsers an HTTP session starts to make cookies

    def is_connected(self) -> bool:
        """Tell whether the session can still check pages (an HTTP session always can)."""
        return self.browser is None or self.browser.is_connected()

    def pick_route(self, zipcode: str) -> Route:
        """Return the route that can start this check soonest, counting the time to make cookies it lacks."""
        if len(self.routes) == 1:
            return self.routes[0]
        return min(self.routes, key=lambda route: route.throttle.next_start_at()
                   + (0 if has_saved_session(zipcode, route.proxy) else NEW_SESSION_SECONDS))


def process_checks(session: Session, checks: queue.Queue, on_result: ResultCallback,
                   stop_event: threading.Event) -> None:
    """Take checks from the shared queue until it is empty, the run is stopped, or Amazon keeps blocking."""
    failed_in_a_row = 0
    while failed_in_a_row < MAX_FAILED_CHECKS_IN_A_ROW and not stop_event.is_set():
        try:
            item = checks.get_nowait()
        except queue.Empty:
            return
        asin, state, zipcode, round_number = item
        row = check_with_retries(session, asin, state, zipcode, stop_event)
        if row is None:  # the run was cancelled before the check started
            checks.put(item)
            return
        if not session.is_connected():
            checks.put(item)  # let a healthy session redo this check
            logger.error("Browser disconnected, stopping this session")
            return

        failed_in_a_row = 0 if row["status"] in FINAL_STATUSES else failed_in_a_row + 1
        if row["status"] in PUSHBACK_STATUSES and round_number < RETRY_ROUNDS and not stop_event.is_set():
            checks.put((asin, state, zipcode, round_number + 1))  # try again later in the run
            logger.info("%s %s: %s, will try again later in the run", asin, state, row["status"])
            continue
        row["attempts"] += (round_number - 1) * MAX_ATTEMPTS
        if row["status"] == STATUS_BLOCKED:
            forget_flagged_session(row["zipcode"])
        report_row(row, on_result)
    if failed_in_a_row >= MAX_FAILED_CHECKS_IN_A_ROW:
        logger.error("Stopping this session after %d failed checks in a row: Amazon is probably blocking it",
                     failed_in_a_row)


def forget_flagged_session(zipcode: str) -> None:
    """Drop the cached session of a zipcode that stayed blocked, so the next run starts it fresh."""
    clear_all_saved_sessions(zipcode)
    logger.warning("Zipcode %s stayed blocked: its cached sessions are cleared for the next run", zipcode)


def report_row(row: dict, on_result: ResultCallback) -> None:
    """Stamp a finished check with its session and time, hand it to on_result, and log it."""
    row.update(session=threading.current_thread().name, checked_at=datetime.now().isoformat(timespec="seconds"))
    on_result(row)
    logger.info("%s %s: %s | buybox %s | %s | sold by %s | %.1f s", row["asin"], row["state"], row["status"],
                row.get("has_buybox", "-"), row.get("price", "-"), row.get("sold_by", "-"), row["duration_seconds"])


def check_with_retries(session: Session, asin: str, state: str, zipcode: str,
                       stop_event: threading.Event) -> dict | None:
    """Check one ASIN in one state, retrying when Amazon pushes back or shows the wrong zipcode.

    Each attempt waits for the turn of the route that can start it soonest. Returns None when the run is
    cancelled before the first attempt starts.
    """
    result, started = None, None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        route = session.pick_route(zipcode)
        if not route.throttle.wait_turn(stop_event):
            break  # the run was cancelled while waiting: keep the last result, if any
        started = started or time.monotonic()
        try:
            result = check_once(session, asin, zipcode, route)
        except Exception as error:  # location setup blocked by the WAF, network error, crashed browser...
            result = {"status": STATUS_ERROR, "error": short_error(error)}
        result.update(asin=asin, state=state, zipcode=zipcode, attempts=attempt)
        route.throttle.record(result["status"], zipcode)
        if result["status"] in FINAL_STATUSES or not session.is_connected():
            break

        logger.warning("%s %s attempt %d/%d: %s %s", asin, state, attempt, MAX_ATTEMPTS,
                       result["status"], result.get("error", ""))
        if result["status"] == STATUS_ZIP_MISMATCH:
            with zip_lock(zipcode, route.proxy):
                clear_saved_session(zipcode, route.proxy)  # stale cookies: set the location again
    if result is None:
        return None
    result["duration_seconds"] = round(time.monotonic() - started, 1)
    return result


def check_once(session: Session, asin: str, zipcode: str, route: Route) -> dict:
    """Inspect the product page once in the zipcode's session on this route (through its proxy, if any)."""
    proxy = route.proxy
    if session.browser is None:
        return check_over_http(session, asin, zipcode, proxy)
    with zip_lock(zipcode, proxy):
        context = open_context_for_zipcode(session.browser, zipcode, session.user_agent, proxy)
    try:
        context.set_default_timeout(ACTION_TIMEOUT_MS)
        return inspect_product_page(context.new_page(), asin, zipcode)
    finally:
        context.close()


def report_not_checked(checks: queue.Queue, on_result: ResultCallback) -> None:
    """Report the checks that no session did, because every session stopped or the run was cancelled."""
    if not checks.empty():
        logger.warning("%d checks were not done; they are saved as '%s'", checks.qsize(), STATUS_NOT_CHECKED)
    while not checks.empty():
        asin, state, zipcode, _ = checks.get_nowait()
        on_result({"asin": asin, "state": state, "zipcode": zipcode, "status": STATUS_NOT_CHECKED})


def short_error(error: Exception) -> str:
    """Return the error type and the first line of its message, short enough for a CSV cell."""
    lines = str(error).strip().splitlines()
    first_line = lines[0] if lines else ""
    return f"{type(error).__name__}: {first_line}"[:ERROR_TEXT_LIMIT]


def check_over_http(session: Session, asin: str, zipcode: str, proxy: Proxy | None) -> dict:
    """Read the product page over HTTP with the zipcode's cookies.

    When Amazon no longer accepts them, a browser makes new ones and the page is read once more.
    """
    rejected = None
    for is_last_try in (False, True):
        cookies = zipcode_cookies(zipcode, proxy, session.is_headed, rejected)
        result = inspect_product_html(*http_page.fetch_product_page(asin, cookies, proxy), asin, zipcode)
        if result["status"] != STATUS_NEW_SESSION_NEEDED:
            return result
        if is_last_try:
            return result | {"status": STATUS_BLOCKED}  # even new cookies don't open the page: Amazon pushes back
        logger.info("Amazon did not accept the cached session of zipcode %s: making a new one", zipcode)
        rejected = cookies


def zipcode_cookies(zipcode: str, proxy: Proxy | None, is_headed: bool,
                    rejected: list[dict] | None = None) -> list[dict]:
    """Return the cookies of the zipcode's session behind this proxy, making them in a browser when needed.

    Cookies that Amazon just rejected are replaced, unless another session already replaced them.
    """
    with zip_lock(zipcode, proxy):
        cookies = load_saved_cookies(zipcode, proxy)
        if cookies is not None and cookies == rejected:
            clear_saved_session(zipcode, proxy)
            cookies = None
        if cookies is None:
            make_zipcode_session(zipcode, proxy, is_headed)
            cookies = load_saved_cookies(zipcode, proxy) or []
    return cookies


def make_zipcode_session(zipcode: str, proxy: Proxy | None, is_headed: bool) -> None:
    """Start a browser just long enough to set the zipcode and save the session's cookies."""
    with BROWSERS_FOR_COOKIES, sync_playwright() as playwright:
        try:
            browser = open_browser(playwright, None, is_headed)
        except Exception as error:
            raise RuntimeError(f"{short_error(error)}{missing_browser_hint(error)}") from error
        try:
            open_context_for_zipcode(browser, zipcode, real_user_agent(browser), proxy).close()
        finally:
            browser.close()


def warm_up(zipcodes: list[str], stop_event: threading.Event | None = None) -> None:
    """Make the cookies of these zipcodes on every proxy that lacks them, so first checks need no browser.

    Runs only with proxies and HTTP checks; the browsers and the pace of each proxy are shared with the checks.
    """
    proxies = proxies_from_env()
    if not proxies or not http_page.is_available():
        return
    missing = [(zipcode, proxy) for zipcode in zipcodes for proxy in proxies if not has_saved_session(zipcode, proxy)]
    if not missing:
        return
    logger.info("Warm-up: making the cookies of %d zipcode sessions", len(missing))
    stop_event = stop_event or threading.Event()
    work = queue.Queue()
    for pair in missing:
        work.put(pair)

    def make_cookies() -> None:
        while not stop_event.is_set():
            try:
                zipcode, proxy = work.get_nowait()
            except queue.Empty:
                return
            throttle = throttle_for(f"proxy {proxy.name}")
            if not throttle.wait_turn(stop_event):
                return
            try:
                zipcode_cookies(zipcode, proxy, is_headed=False)
                throttle.record(STATUS_OK, zipcode)
            except Exception as error:  # Amazon pushed back: the first check of this zipcode tries again
                throttle.record(STATUS_ERROR, zipcode)
                logger.warning("Warm-up: no cookies for zipcode %s on %s: %s", zipcode, proxy.name, short_error(error))

    threads = [threading.Thread(target=make_cookies, name=f"warm-up-{number}", daemon=True)
               for number in range(1, COOKIE_BROWSERS + 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    logger.info("Warm-up done")
