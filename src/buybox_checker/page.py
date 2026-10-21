"""Load one Amazon product page in a zipcode session, prove it loaded correctly, and read its buybox.

Input:  a Playwright page whose context is already set to a zipcode (see zipcode.py),
        an ASIN and that zipcode.
Output: a dict with "status" (ok / not_found / blocked / timeout / zip_mismatch) and, when ok,
        the buybox fields: has_buybox, price, sold_by, ships_from, seller_id, availability, ...
Constraints: Amazon answers bots with captcha pages, "Continue shopping" pages and error pages,
        some of them with HTTP 200, so a page counts as loaded only when the product title is
        shown and "Deliver to" shows our zipcode. Field readers never wait: a missing field is "".
"""

import logging
import re

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from buybox_checker.config import DEBUG_DIR
from buybox_checker.zipcode import DELIVER_TO_SELECTOR

logger = logging.getLogger(__name__)

PRODUCT_URL = "https://www.amazon.com/dp/{asin}"
GOTO_TIMEOUT_MS = 30_000
PAGE_READY_TIMEOUT_MS = 20_000  # long enough for Amazon's WAF challenge page to reload itself
HTTP_NOT_FOUND = 404
HTTP_FIRST_ERROR = 400

STATUS_OK = "ok"
STATUS_NOT_FOUND = "not_found"
STATUS_BLOCKED = "blocked"
STATUS_TIMEOUT = "timeout"
STATUS_ZIP_MISMATCH = "zip_mismatch"

PRODUCT_TITLE_SELECTOR = "#productTitle"
# Block pages: the captcha page, and both layouts of the "Continue shopping" page
BLOCK_PAGE_SELECTOR = 'form[action*="validateCaptcha"], form[action*="/checkpoint/"]'
BUY_BUTTON_SELECTOR = "#add-to-cart-button, #buy-now-button"
SEE_ALL_BUYING_OPTIONS_SELECTOR = "#buybox-see-all-buying-choices"
MERCHANT_ID_SELECTOR = "input#merchantID"
SELLER_LINK_SELECTOR = "#sellerProfileTriggerId"
CANNOT_SHIP_TEXT = "cannot be shipped to your selected delivery location"

# Each list is tried in order; the first selector that finds non-empty text wins.
PRICE_SELECTORS = [
    "#corePrice_feature_div .a-price .a-offscreen",
    "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
    "#buybox .a-price .a-offscreen",
    "#price_inside_buybox",
]
SOLD_BY_SELECTORS = [
    "#merchantInfoFeature_feature_div .offer-display-feature-text-message",
    SELLER_LINK_SELECTOR,
    '#tabular-buybox .tabular-buybox-text[tabular-attribute-name="Sold by"]',
    "#merchant-info",
]
SHIPS_FROM_SELECTORS = [
    "#fulfillerInfoFeature_feature_div .offer-display-feature-text-message",
    '#tabular-buybox .tabular-buybox-text[tabular-attribute-name="Ships from"]',
    "#merchant-info",
]
AVAILABILITY_SELECTORS = [  # spans only: #availability also holds a hidden JSON <script>
    "#availability .primary-availability-message",
    "#availability span",
    "#outOfStock span",
]
DELIVERY_SELECTORS = [
    "#mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE",
    "#mir-layout-DELIVERY_BLOCK",
    "#deliveryBlockMessage",
]

PRICE_PATTERN = r"\$[\d,]+(?:\.\d\d)?"
SELLER_ID_PATTERN = r"seller=(\w+)"
INVISIBLE_CHARACTERS = "\u200c\u200b\u200e"  # Amazon pads some texts with zero-width characters


def inspect_product_page(page: Page, asin: str, zipcode: str) -> dict[str, str]:
    """Load the product page, prove it is the real page for this zipcode, and read its buybox."""
    status = load_product_page(page, asin)
    deliver_to = first_text(page, [DELIVER_TO_SELECTOR])
    if is_location_lost(status, deliver_to, zipcode):
        status = STATUS_ZIP_MISMATCH  # the cached session no longer has our zipcode: it must be set again
    if status != STATUS_OK:
        save_debug_page(page, asin, zipcode, status)
        return {"status": status, "deliver_to": deliver_to}
    return {"status": STATUS_OK, "deliver_to": deliver_to} | clean_buybox(read_buybox(page))


def is_location_lost(status: str, deliver_to: str, zipcode: str) -> bool:
    """Tell whether the page shows the session lost its location (e.g. "Deliver to Canada" after it expired).

    A product page must show our zipcode. A block or timeout page that shows another location means
    the cached session itself is stale, so retrying with it would fail the same way every time.
    """
    if status == STATUS_OK:
        return zipcode not in deliver_to
    return status in (STATUS_BLOCKED, STATUS_TIMEOUT) and bool(deliver_to) and zipcode not in deliver_to


def load_product_page(page: Page, asin: str) -> str:
    """Open the product page and tell whether it loaded, was blocked, or does not exist."""
    url = PRODUCT_URL.format(asin=asin)
    response = page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
    if response is not None and response.status == HTTP_NOT_FOUND:
        return STATUS_NOT_FOUND
    if response is None or response.status >= HTTP_FIRST_ERROR:
        return STATUS_BLOCKED

    ready_marker = page.locator(f"{PRODUCT_TITLE_SELECTOR}, {BLOCK_PAGE_SELECTOR}").first
    try:
        ready_marker.wait_for(timeout=PAGE_READY_TIMEOUT_MS)  # keeps waiting across page reloads
    except PlaywrightTimeoutError:
        return STATUS_TIMEOUT
    if page.locator(BLOCK_PAGE_SELECTOR).count() > 0:
        return STATUS_BLOCKED
    return STATUS_OK


def read_buybox(page: Page) -> dict:
    """Read the raw buybox texts and flags from a loaded product page."""
    return {
        "title": first_text(page, [PRODUCT_TITLE_SELECTOR]),
        "price": first_text(page, PRICE_SELECTORS),
        "sold_by": first_text(page, SOLD_BY_SELECTORS),
        "ships_from": first_text(page, SHIPS_FROM_SELECTORS),
        "merchant_id": first_attribute(page, MERCHANT_ID_SELECTOR, "value"),
        "seller_link": first_attribute(page, SELLER_LINK_SELECTOR, "href"),
        "availability": first_text(page, AVAILABILITY_SELECTORS),
        "delivery_message": first_text(page, DELIVERY_SELECTORS),
        "has_buy_button": page.locator(BUY_BUTTON_SELECTOR).count() > 0,
        "has_see_all_buying_options": page.locator(SEE_ALL_BUYING_OPTIONS_SELECTOR).count() > 0,
        "has_cannot_ship_text": page.get_by_text(CANNOT_SHIP_TEXT).count() > 0,
    }


def clean_buybox(raw: dict) -> dict[str, str]:
    """Turn the raw page texts into result fields (pure function, testable without a browser)."""
    price = re.search(PRICE_PATTERN, raw["price"])
    seller_in_link = re.search(SELLER_ID_PATTERN, raw["seller_link"])
    return {
        "title": raw["title"],
        "has_buybox": yes_no(raw["has_buy_button"]),
        "price": price.group(0) if price else "",
        "sold_by": raw["sold_by"],
        "ships_from": raw["ships_from"],
        "seller_id": raw["merchant_id"] or (seller_in_link.group(1) if seller_in_link else ""),
        "availability": raw["availability"],
        "delivery_message": raw["delivery_message"],
        "cannot_ship": yes_no(raw["has_cannot_ship_text"]),
        "see_all_buying_options": yes_no(raw["has_see_all_buying_options"]),
    }


def first_text(page: Page, selectors: list[str]) -> str:
    """Return the cleaned text of the first selector that finds non-empty text, or '' (never waits)."""
    for selector in selectors:
        for text in page.locator(selector).all_text_contents():
            if clean_text(text):
                return clean_text(text)
    return ""


def first_attribute(page: Page, selector: str, name: str) -> str:
    """Return an attribute of the first element matching the selector, or '' (never waits)."""
    element = page.locator(selector).first
    if element.count() == 0:
        return ""
    return element.get_attribute(name) or ""


def clean_text(text: str) -> str:
    """Remove zero-width characters and collapse whitespace into single spaces."""
    for character in INVISIBLE_CHARACTERS:
        text = text.replace(character, "")
    return " ".join(text.split())


def yes_no(flag: bool) -> str:
    """Write a flag as 'yes' or 'no' so the CSV reads naturally."""
    return "yes" if flag else "no"


def save_debug_page(page: Page, asin: str, zipcode: str, status: str) -> None:
    """Keep the HTML of a page that did not load as expected, to see what Amazon returned."""
    path = DEBUG_DIR / f"{asin}_{zipcode}_{status}.html"
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(page.content(), encoding="utf-8")
        logger.info("Saved the %s page to %s", status, path)
    except Exception as error:  # the page may still be navigating; the check result matters more
        logger.warning("Could not save %s: %s", path, error)
