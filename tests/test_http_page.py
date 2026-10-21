"""Reading product pages fetched over HTTP, and making new cookies only when Amazon rejects the cached ones."""

from buybox_checker import engine
from buybox_checker.page import inspect_product_html

PRODUCT_HTML = """<html><body>
<span id="glow-ingress-line2">Houston 77002&zwnj;</span>
<span id="productTitle">  StrongTek Balance Pad </span>
<div id="corePrice_feature_div"><span class="a-price"><span class="a-offscreen">$26.99</span></span></div>
<div id="merchantInfoFeature_feature_div"><span class="offer-display-feature-text-message">StrongTek</span></div>
<a id="sellerProfileTriggerId" href="/gp/help/seller/at-a-glance.html?seller=A1B2C3">StrongTek</a>
<input id="add-to-cart-button" type="submit">
<script>var t = "This item cannot be shipped to your selected delivery location";</script>
</body></html>"""
CONTINUE_SHOPPING_HTML = """<html><body><form method="get" action="/errors_page/validateCaptcha">
<button type="submit">Continue shopping</button></form></body></html>"""


def test_a_product_page_fetched_over_http_is_read_like_the_browser_reads_it():
    result = inspect_product_html(200, PRODUCT_HTML, "B07FNDF2R7", "77002")
    assert result["status"] == "ok"
    assert (result["title"], result["price"], result["sold_by"], result["seller_id"], result["deliver_to"]) == (
        "StrongTek Balance Pad", "$26.99", "StrongTek", "A1B2C3", "Houston 77002")
    assert result["has_buybox"] == "yes"
    assert result["cannot_ship"] == "no"  # the sentence is only in a script, not shown on the page


def test_the_cannot_ship_sentence_counts_when_it_is_shown():
    shown = PRODUCT_HTML.replace("<script>", "<div>").replace("</script>", "</div>")
    assert inspect_product_html(200, shown, "B07FNDF2R7", "77002")["cannot_ship"] == "yes"


def test_pages_that_need_new_cookies_are_told_apart_from_missing_products_and_errors(tmp_path, monkeypatch):
    monkeypatch.setattr("buybox_checker.page.DEBUG_DIR", tmp_path)
    new_session = "new_session_needed"
    assert inspect_product_html(200, CONTINUE_SHOPPING_HTML, "B07FNDF2R7", "77002")["status"] == new_session
    assert inspect_product_html(200, PRODUCT_HTML, "B07FNDF2R7", "10001")["status"] == new_session  # location lost
    assert inspect_product_html(404, "", "B000000000", "77002")["status"] == "not_found"
    assert inspect_product_html(503, "", "B07FNDF2R7", "77002")["status"] == "blocked"


def test_rejected_cookies_are_replaced_once_even_when_two_sessions_report_them(monkeypatch):
    saved = {"cookies": [{"name": "session-id", "value": "old"}]}
    made = []

    def make_session(zipcode, proxy, is_headed):
        made.append(zipcode)
        saved["cookies"] = [{"name": "session-id", "value": f"new-{len(made)}"}]

    monkeypatch.setattr(engine, "load_saved_cookies", lambda zipcode, proxy: saved["cookies"])
    monkeypatch.setattr(engine, "clear_saved_session", lambda zipcode, proxy: saved.update(cookies=None))
    monkeypatch.setattr(engine, "make_zipcode_session", make_session)

    old = engine.zipcode_cookies("77002", None, False)
    assert engine.zipcode_cookies("77002", None, False, rejected=old)[0]["value"] == "new-1"
    assert engine.zipcode_cookies("77002", None, False, rejected=old)[0]["value"] == "new-1"  # already replaced
    assert made == ["77002"]
