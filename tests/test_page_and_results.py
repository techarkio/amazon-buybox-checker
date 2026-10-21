from buybox_checker.page import clean_buybox, clean_text
from buybox_checker.results import append_row, read_rows, summarize, summary_line
from buybox_checker.states import STATES, pick_states

RAW_BUYBOX = {
    "title": "Echo Dot", "price": "$1,249.99$1,249.99", "sold_by": "Amazon.com", "ships_from": "",
    "merchant_id": "", "seller_link": "/gp/help/seller/at-a-glance.html?ie=UTF8&seller=A2XYZ123&asin=B0",
    "availability": "In Stock", "delivery_message": "FREE delivery Friday", "has_buy_button": True,
    "has_see_all_buying_options": False, "has_cannot_ship_text": False,
}


def test_clean_buybox_parses_price_and_seller_id():
    result = clean_buybox(RAW_BUYBOX)
    assert result["price"] == "$1,249.99"
    assert result["seller_id"] == "A2XYZ123"
    assert result["has_buybox"] == "yes"
    assert result["cannot_ship"] == "no"


def test_clean_buybox_prefers_the_merchant_id_input():
    assert clean_buybox(RAW_BUYBOX | {"merchant_id": "ATVPDKIKX0DER"})["seller_id"] == "ATVPDKIKX0DER"


def test_clean_text_drops_zero_width_characters():
    assert clean_text("  New York 10001‌ \n ") == "New York 10001"


def test_summaries_count_winners_no_buybox_and_failures():
    rows = [
        {"asin": "B0BCMLGPYB", "state": "NY", "status": "ok", "has_buybox": "yes", "sold_by": "Acme",
         "price": "$9.99"},
        {"asin": "B0BCMLGPYB", "state": "CA", "status": "ok", "has_buybox": "yes", "sold_by": "Acme",
         "price": "$10.49"},
        {"asin": "B0BCMLGPYB", "state": "AK", "status": "ok", "has_buybox": "no", "sold_by": ""},
        {"asin": "B0BCMLGPYB", "state": "TX", "status": "blocked"},
        {"asin": "B000000000", "state": "NY", "status": "not_found"},
    ]
    first, second = summarize(rows)
    assert first["with_buybox"] == 2
    assert first["winners"] == [{"seller": "Acme", "states": 2}]
    assert first["prices"] == ["$9.99", "$10.49"]
    assert first["no_buybox"] == ["AK"]
    assert first["failed"] == [{"state": "TX", "status": "blocked"}]
    assert summary_line(first) == (
        "B0BCMLGPYB: buybox in 2/4 states | winners: Acme 2 | no buybox: AK | failed: TX (blocked)"
    )
    assert summary_line(second) == "B000000000: product page not found on Amazon, check the ASIN"


def test_csv_round_trip_writes_one_header_and_one_bom(tmp_path):
    path = tmp_path / "results" / "run.csv"
    append_row(path, {"asin": "B0BCMLGPYB", "state": "NY", "status": "ok", "sold_by": "Café Ltd"})
    append_row(path, {"asin": "B0BCMLGPYB", "state": "CA", "status": "blocked", "unknown_key": "ignored"})
    assert path.read_bytes().count(b"\xef\xbb\xbf") == 1
    rows = read_rows(path)
    assert [row["state"] for row in rows] == ["NY", "CA"]
    assert rows[0]["sold_by"] == "Café Ltd"


def test_states_have_50_unique_five_digit_zipcodes():
    zipcodes = [state.zipcode for state in STATES.values()]
    assert len(STATES) == 50
    assert len(set(zipcodes)) == 50
    assert all(len(zipcode) == 5 and zipcode.isdigit() for zipcode in zipcodes)


def test_pick_states_accepts_any_case_and_rejects_unknown_codes():
    assert pick_states(["ny", " ca ", ""]) == {"NY": "10001", "CA": "90012"}
    assert len(pick_states(None)) == 50
    assert len(pick_states(["", " "])) == 50
    try:
        pick_states(["ZZ"])
    except ValueError as error:
        assert "ZZ" in str(error)
    else:
        raise AssertionError("expected ValueError")


def test_stale_sessions_are_detected_on_product_block_and_timeout_pages():
    from buybox_checker.page import is_location_lost

    assert is_location_lost("ok", "New York 10001", "10001") is False
    assert is_location_lost("ok", "Canada", "10001") is True
    assert is_location_lost("ok", "", "10001") is True  # a product page that can't prove its location
    assert is_location_lost("blocked", "Canada", "90012") is True  # expired session behind a block page
    assert is_location_lost("timeout", "Canada", "90012") is True
    assert is_location_lost("blocked", "", "90012") is False  # a captcha page has no header to judge by
    assert is_location_lost("not_found", "Canada", "90012") is False
