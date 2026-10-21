"""Fetch Amazon product pages over plain HTTP, presenting Chrome's network fingerprint (curl_cffi).

Input:  an ASIN, the cookies of a zipcode session that a browser made (see zipcode.py), and its proxy, if any.
Output: the HTTP status and the HTML of the product page, for page.inspect_product_html.
Constraints: a first visit needs a real browser (Amazon's JavaScript challenge, maybe a "Continue shopping"
        page), but once a browser has made the session, its cookies open product pages over plain HTTP: no
        images, scripts or rendering, so a check takes a second or two and almost no memory. curl_cffi is
        loaded only if it can be: some builds do not load on recent macOS, and then every check uses the browser.
"""

import logging

from buybox_checker.proxies import Proxy

logger = logging.getLogger(__name__)

try:
    from curl_cffi import requests as curl_requests
except (ImportError, OSError) as error:  # missing, or a build that does not load on this system
    curl_requests = None
    UNAVAILABLE_REASON = f"{type(error).__name__}: {error}"
else:
    UNAVAILABLE_REASON = ""

PRODUCT_URL = "https://www.amazon.com/dp/{asin}"
IMPERSONATE = "chrome"  # curl_cffi's latest Chrome: TLS, HTTP/2 and headers all match one real browser
TIMEOUT_SECONDS = 30
HEADERS = {"Accept-Language": "en-US,en;q=0.9"}


def is_available() -> bool:
    """Tell whether product pages can be fetched over HTTP on this system."""
    return curl_requests is not None


def fetch_product_page(asin: str, cookies: list[dict], proxy: Proxy | None) -> tuple[int, str]:
    """Fetch the product page with the session's cookies, through the proxy; return the status and HTML."""
    proxies = {"http": proxy.url(), "https": proxy.url()} if proxy else None
    with curl_requests.Session(impersonate=IMPERSONATE, timeout=TIMEOUT_SECONDS, proxies=proxies) as http:
        for cookie in cookies:
            http.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"])
        response = http.get(PRODUCT_URL.format(asin=asin), headers=HEADERS)
        return response.status_code, response.text
