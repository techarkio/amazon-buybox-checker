"""Open Amazon pages in Playwright with a chosen US delivery zipcode already applied.

Input:  a US zipcode (e.g. "10001") and a Playwright browser.
Output: a BrowserContext whose Amazon session is already set to that zipcode,
        so every page.goto() renders prices/availability for that location.
Constraints: Amazon keeps the location server-side, tied to the session cookies,
        so it cannot be set by a URL parameter or a hand-made cookie. We open the
        homepage once in a real page (the browser solves Amazon's WAF JavaScript
        challenge, which plain requests cannot, and clicks through a "Continue shopping" page if Amazon
        shows one), then call the same API the
        "Deliver to" popup uses (request-only, no page rendering), then cache the
        cookies per zipcode so later runs need zero extra requests. Behind a proxy, the cookies are
        cached per zipcode and proxy, since Amazon ties them to the IP that got them.
"""

import json
import logging
import re
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from buybox_checker.config import SESSIONS_DIR
from buybox_checker.proxies import Proxy

logger = logging.getLogger(__name__)

AMAZON_HOME_URL = "https://www.amazon.com/"
LOCATION_POPUP_URL = (
    "https://www.amazon.com/portal-migration/hz/glow/get-rendered-address-selections"
    "?deviceType=desktop&pageType=Gateway&storeContext=NoStoreName&actionSource=desktop-modal"
)
ADDRESS_CHANGE_URL = "https://www.amazon.com/portal-migration/hz/glow/address-change?actionSource=glow"

CSRF_HEADER = "anti-csrftoken-a2z"
HOME_TOKEN_PATTERN = r"anti-csrftoken-a2z&quot;:&quot;([^&]+)&quot;"
POPUP_TOKEN_PATTERN = r'CSRF_TOKEN\s*:\s*"([^"]+)"'
DELIVER_TO_SELECTOR = "#glow-ingress-line2"

WAF_HEADER = "x-amzn-waf-action"  # set to "challenge" when Amazon rate-limits the caller
WAF_CHALLENGE_TIMEOUT_MS = 20_000  # time for the browser to solve the challenge and reload the page
# Amazon's "Continue shopping" page (two layouts): one button, no question. The image captcha page uses the
# same validateCaptcha form plus a text box; that one is never answered, the check is reported as blocked.
CONTINUE_SHOPPING_FORM_SELECTOR = 'form[action*="validateCaptcha"], form[action*="/checkpoint/"]'
IMAGE_CAPTCHA_SELECTOR = "#captchacharacters"
CONTEXT_OPTIONS = {
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
    ),
    "locale": "en-US",
}


def pass_waf_challenge(context: BrowserContext) -> None:
    """Open the homepage once in a real page so the browser solves Amazon's WAF challenge for this context."""
    page = context.new_page()  # context.request calls share the resulting aws-waf-token cookie
    try:
        page.goto(AMAZON_HOME_URL, wait_until="domcontentloaded")
        homepage_or_gate = page.locator(f"{DELIVER_TO_SELECTOR}, {CONTINUE_SHOPPING_FORM_SELECTOR}").first
        homepage_or_gate.wait_for(timeout=WAF_CHALLENGE_TIMEOUT_MS)
        click_continue_shopping(page)
        page.locator(DELIVER_TO_SELECTOR).wait_for(timeout=WAF_CHALLENGE_TIMEOUT_MS)  # only on the real homepage
    except PlaywrightTimeoutError as error:
        raise RuntimeError("Amazon homepage did not load: WAF challenge or captcha not passed") from error
    finally:
        page.close()


def click_continue_shopping(page: Page) -> bool:
    """Click the button of Amazon's "Continue shopping" page once, like a shopper would; True if clicked.

    Behind a proxy new to Amazon, the first page is often this one-button page; one click lets the session
    through. An image captcha is left alone.
    """
    form = page.locator(CONTINUE_SHOPPING_FORM_SELECTOR)
    if form.count() == 0 or page.locator(IMAGE_CAPTCHA_SELECTOR).count() > 0:
        return False
    logger.info("Amazon showed its 'Continue shopping' page: clicking through once")
    form.locator("button, input[type=submit]").first.click()
    page.wait_for_load_state("domcontentloaded")
    return True


def fetch_home_token(context: BrowserContext) -> str:
    """Get the CSRF token that the 'Deliver to' popup needs, from the homepage HTML."""
    response = context.request.get(AMAZON_HOME_URL)
    if response.headers.get(WAF_HEADER) == "challenge":
        raise RuntimeError("Amazon is rate-limiting this IP (WAF challenge); slow down and retry later")
    match = re.search(HOME_TOKEN_PATTERN, response.text())
    if not match:
        raise RuntimeError("Could not read the location token from the Amazon homepage")
    return match.group(1)


def fetch_address_change_token(context: BrowserContext, home_token: str) -> str:
    """Get the CSRF token required by the address-change API."""
    html = context.request.get(LOCATION_POPUP_URL, headers={CSRF_HEADER: home_token}).text()
    match = re.search(POPUP_TOKEN_PATTERN, html)
    if not match:
        raise RuntimeError("Could not read the address-change token from the location popup")
    return match.group(1)


def set_zipcode(context: BrowserContext, zipcode: str) -> None:
    """Set the Amazon delivery location of this browser context to a US zipcode."""
    pass_waf_challenge(context)
    home_token = fetch_home_token(context)
    change_token = fetch_address_change_token(context, home_token)
    payload = {
        "locationType": "LOCATION_INPUT",
        "zipCode": zipcode,
        "deviceType": "web",
        "storeContext": "generic",
        "pageType": "Gateway",
        "actionSource": "glow",
    }
    response = context.request.post(ADDRESS_CHANGE_URL, headers={CSRF_HEADER: change_token}, data=payload)
    result = response.json()
    if result.get("isAddressUpdated") != 1:
        raise RuntimeError(f"Amazon did not accept zipcode {zipcode}: {result}")
    logger.info("Delivery location set to %s %s", result["address"]["city"], zipcode)


def state_file_for(zipcode: str, proxy: Proxy | None = None) -> Path:
    """Return the file that caches the Amazon session cookies of a zipcode (behind this proxy, if any)."""
    if proxy:
        return SESSIONS_DIR / f"zip_{zipcode}_via_{proxy.host}_{proxy.port}.json"
    return SESSIONS_DIR / f"zip_{zipcode}.json"


def has_saved_session(zipcode: str, proxy: Proxy | None = None) -> bool:
    """Tell whether the zipcode has a cached session behind this proxy."""
    return state_file_for(zipcode, proxy).exists()


def clear_saved_session(zipcode: str, proxy: Proxy | None = None) -> None:
    """Forget the zipcode's cached session behind this proxy so the next open sets the location again."""
    state_file_for(zipcode, proxy).unlink(missing_ok=True)
    logger.info("Cleared saved session for zipcode %s%s", zipcode, f" on {proxy.name}" if proxy else "")


def clear_all_saved_sessions(zipcode: str) -> None:
    """Forget every cached session of a zipcode, direct and behind any proxy."""
    for state_file in [state_file_for(zipcode), *SESSIONS_DIR.glob(f"zip_{zipcode}_via_*.json")]:
        state_file.unlink(missing_ok=True)
    logger.info("Cleared saved sessions for zipcode %s", zipcode)


def load_saved_cookies(zipcode: str, proxy: Proxy | None = None) -> list[dict] | None:
    """Return the cookies of the zipcode's cached session behind this proxy, or None when there is none."""
    try:
        return json.loads(state_file_for(zipcode, proxy).read_text(encoding="utf-8"))["cookies"]
    except (OSError, ValueError, KeyError):
        return None


def open_context_for_zipcode(browser: Browser, zipcode: str, user_agent: str | None = None,
                             proxy: Proxy | None = None) -> BrowserContext:
    """Return a browser context already set to the zipcode, reusing cached cookies when present.

    Pass the browser's real user agent (see engine.real_user_agent) so the context describes the browser
    that actually runs; without it, CONTEXT_OPTIONS' fixed user agent is used. With a proxy, all of the
    context's traffic goes through it.
    """
    options = CONTEXT_OPTIONS | ({"user_agent": user_agent} if user_agent else {})
    if proxy:
        options["proxy"] = proxy.playwright_settings()
    state_file = state_file_for(zipcode, proxy)
    if state_file.exists():
        logger.debug("Reusing saved session for zipcode %s", zipcode)
        return browser.new_context(storage_state=state_file, **options)

    context = browser.new_context(**options)
    try:
        set_zipcode(context, zipcode)
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=state_file)  # overwrite per zipcode, safe to re-run
    except Exception:
        context.close()  # don't leave a half-set session open in the browser
        raise
    return context


def read_deliver_to(page: Page) -> str:
    """Read the 'Deliver to' text shown in the header of a loaded Amazon page."""
    return page.locator(DELIVER_TO_SELECTOR).inner_text().strip()

