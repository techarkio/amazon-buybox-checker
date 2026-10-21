"""Load one Amazon product page in a zipcode session, prove it loaded correctly, and read its buybox.

Input:  a Playwright page whose context is already set to a zipcode (see zipcode.py), or the HTTP status and
        HTML of a product page fetched with that session's cookies (see http_page.py); an ASIN and that zipcode.
Output: a dict with "status" (ok / not_found / blocked / timeout / zip_mismatch, or new_session_needed for
        fetched HTML) and, when ok, the buybox fields: has_buybox, price, sold_by, ships_from, seller_id, ...
Constraints: Amazon answers bots with captcha pages, "Continue shopping" pages and error pages,
        some of them with HTTP 200, so a page counts as loaded only when the product title is
        shown and "Deliver to" shows our zipcode. A "Continue shopping" page is clicked through once;
        a captcha is never answered. Both ways read the buybox from the page's HTML, the same way.
"""

import logging
import re

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from selectolax.lexbor import LexborHTMLParser, LexborNode

from buybox_checker.config import DEBUG_DIR
from buybox_checker.zipcode import CONTINUE_SHOPPING_FORM_SELECTOR, DELIVER_TO_SELECTOR, click_continue_shopping

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
STATUS_NEW_SESSION_NEEDED = "new_session_needed"  # fetched HTML only: the cookies no longer open product pages

PRODUCT_TITLE_SELECTOR = "#productTitle"
BLOCK_PAGE_SELECTOR = CONTINUE_SHOPPING_FORM_SELECTOR  # also matches the captcha page
BUY_BUTTON_SELECTOR = "#add-to-cart-button, #buy-now-button"
SEE_ALL_BUYING_OPTIONS_SELECTOR = "#buybox-see-all-buying-choices"
MERCHANT_ID_SELECTOR = "input#merchantID"
SELLER_LINK_SELECTOR = "#sellerProfileTriggerId"
CANNOT_SHIP_TEXT = "cannot be shipped to your selected delivery location"
HIDDEN_TAGS = ["script", "style", "noscript", "template"]  # their text is never shown on the page

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
    tree = LexborHTMLParser(page_html(page))
    deliver_to = first_text(tree, [DELIVER_TO_SELECTOR])
    if is_location_lost(status, deliver_to, zipcode):
        status = STATUS_ZIP_MISMATCH  # the cached session no longer has our zipcode: it must be set again
    if status != STATUS_OK:
        save_debug_html(tree.html or "", asin, zipcode, status)
        return {"status": status, "deliver_to": deliver_to}
    return {"status": STATUS_OK, "deliver_to": deliver_to} | clean_buybox(read_buybox(tree))


def inspect_product_html(status_code: int, html: str, asin: str, zipcode: str) -> dict[str, str]:
    """Tell what a product page fetched over HTTP shows, and read its buybox when it is the real page.

    Any page without the product, or showing another location, means Amazon no longer accepts the session's
    cookies ("Continue shopping" page, challenge, expired location): a browser has to make new ones.
    """
    if status_code == HTTP_NOT_FOUND:
        return {"status": STATUS_NOT_FOUND}
    if status_code >= HTTP_FIRST_ERROR:
        save_debug_html(html, asin, zipcode, STATUS_BLOCKED)
        return {"status": STATUS_BLOCKED}
    tree = LexborHTMLParser(html)
    deliver_to = first_text(tree, [DELIVER_TO_SELECTOR])
    if tree.css_first(PRODUCT_TITLE_SELECTOR) is None or zipcode not in deliver_to:
        save_debug_html(html, asin, zipcode, STATUS_NEW_SESSION_NEEDED)
        return {"status": STATUS_NEW_SESSION_NEEDED, "deliver_to": deliver_to}
    return {"status": STATUS_OK, "deliver_to": deliver_to} | clean_buybox(read_buybox(tree))


def is_location_lost(status: str, deliver_to: str, zipcode: str) -> bool:
    """Tell whether the page shows the session lost its location (e.g. "Deliver to Canada" after it expired).

    A product page must show our zipcode. A block or timeout page that shows another location means
    the cached session itself is stale, so retrying with it would fail the same way every time.
    """
    if status == STATUS_OK:
        return zipcode not in deliver_to
    return status in (STATUS_BLOCKED, STATUS_TIMEOUT) and bool(deliver_to) and zipcode not in deliver_to


def load_product_page(page: Page, asin: str) -> str:
    """Open the product page and tell whether it loaded, was blocked, or does not exist.

    The "Continue shopping" button leads to the homepage, not back to the product, so after clicking it
    the product page is opened once more.
    """
    url = PRODUCT_URL.format(asin=asin)
    ready_marker = page.locator(f"{PRODUCT_TITLE_SELECTOR}, {BLOCK_PAGE_SELECTOR}").first
    for is_last_try in (False, True):
        response = page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
        if response is not None and response.status == HTTP_NOT_FOUND:
            return STATUS_NOT_FOUND
        if response is None or response.status >= HTTP_FIRST_ERROR:
            return STATUS_BLOCKED
        try:
            ready_marker.wait_for(timeout=PAGE_READY_TIMEOUT_MS)  # keeps waiting across page reloads
        except PlaywrightTimeoutError:
            return STATUS_TIMEOUT
        if page.locator(BLOCK_PAGE_SELECTOR).count() == 0:
            return STATUS_OK
        if is_last_try or not click_continue_shopping(page):
            return STATUS_BLOCKED
    return STATUS_BLOCKED


def read_buybox(tree: LexborHTMLParser) -> dict:
    """Read the raw buybox texts and flags from the HTML of a loaded product page."""
    raw = {
        "title": first_text(tree, [PRODUCT_TITLE_SELECTOR]),
        "price": first_text(tree, PRICE_SELECTORS),
        "sold_by": first_text(tree, SOLD_BY_SELECTORS),
        "ships_from": first_text(tree, SHIPS_FROM_SELECTORS),
        "merchant_id": first_attribute(tree, MERCHANT_ID_SELECTOR, "value"),
        "seller_link": first_attribute(tree, SELLER_LINK_SELECTOR, "href"),
        "availability": first_text(tree, AVAILABILITY_SELECTORS),
        "delivery_message": first_text(tree, DELIVERY_SELECTORS),
        "has_buy_button": tree.css_first(BUY_BUTTON_SELECTOR) is not None,
        "has_see_all_buying_options": tree.css_first(SEE_ALL_BUYING_OPTIONS_SELECTOR) is not None,
    }
    tree.strip_tags(HIDDEN_TAGS)  # scripts may hold the sentence too; only shown text counts
    raw["has_cannot_ship_text"] = CANNOT_SHIP_TEXT in clean_text(tree.body.text() if tree.body else "").lower()
    return raw


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


def first_text(tree: LexborHTMLParser | LexborNode, selectors: list[str]) -> str:
    """Return the cleaned text of the first selector that finds non-empty text, or ''."""
    for selector in selectors:
        for node in tree.css(selector):
            if text := clean_text(node.text()):
                return text
    return ""


def first_attribute(tree: LexborHTMLParser, selector: str, name: str) -> str:
    """Return an attribute of the first element matching the selector, or ''."""
    node = tree.css_first(selector)
    return (node.attributes.get(name) or "") if node is not None else ""


def clean_text(text: str) -> str:
    """Remove zero-width characters and collapse whitespace into single spaces."""
    for character in INVISIBLE_CHARACTERS:
        text = text.replace(character, "")
    return " ".join(text.split())


def yes_no(flag: bool) -> str:
    """Write a flag as 'yes' or 'no' so the CSV reads naturally."""
    return "yes" if flag else "no"


def page_html(page: Page) -> str:
    """Return the page's current HTML, or '' while it is still navigating."""
    try:
        return page.content()
    except Exception as error:  # e.g. a WAF challenge page reloading itself
        logger.debug("Could not read the page HTML: %s", error)
        return ""


def save_debug_html(html: str, asin: str, zipcode: str, status: str) -> None:
    """Keep the HTML of a page that did not load as expected, to see what Amazon returned."""
    path = DEBUG_DIR / f"{asin}_{zipcode}_{status}.html"
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")
        logger.info("Saved the %s page to %s", status, path)
    except OSError as error:  # the check result matters more than the debug copy
        logger.warning("Could not save %s: %s", path, error)
