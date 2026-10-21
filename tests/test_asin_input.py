from buybox_checker.asin_input import decode_upload, extract_asins, find_short_links


def test_reads_asins_from_every_product_link_form():
    text = """
    https://www.amazon.com/Little-Sleepies-Pajamas/dp/B0BCMLGPYB/ref=sr_1_1?keywords=x
    https://www.amazon.com/gp/product/B09B8V1LZ3?th=1
    https://www.amazon.com/gp/aw/d/B07XJ8C8F5
    """
    assert extract_asins(text) == ["B0BCMLGPYB", "B09B8V1LZ3", "B07XJ8C8F5"]


def test_reads_bare_asins_in_any_separator_and_case():
    assert extract_asins("b0bcmlgpyb, B09B8V1LZ3;B07XJ8C8F5\tB0BCMLGPYB") == ["B0BCMLGPYB", "B09B8V1LZ3", "B07XJ8C8F5"]


def test_reads_asins_from_a_csv_with_other_columns():
    csv_text = "sku,asin,price\nSKU-1,B0BCMLGPYB,19.99\nSKU-2,B09B8V1LZ3,39.99\n"
    assert extract_asins(csv_text) == ["B0BCMLGPYB", "B09B8V1LZ3"]


def test_reads_isbn10_books_but_not_ordinary_words():
    assert extract_asins("0735211299 BUYBOXTEST abcdefghij 12345") == ["0735211299"]


def test_ignores_longer_codes_that_contain_an_asin():
    assert extract_asins("B0BCMLGPYB12 XB0BCMLGPYB") == []


def test_reports_short_links_it_cannot_resolve():
    assert find_short_links("see https://amzn.to/3abcXYZ and https://a.co/d/1234") == [
        "https://amzn.to/3abcXYZ", "https://a.co/d/1234"]


def test_decodes_uploads_with_bom_or_latin1():
    assert decode_upload("﻿B0BCMLGPYB".encode("utf-8")) == "B0BCMLGPYB"
    assert decode_upload("café B0BCMLGPYB".encode("latin-1")) == "café B0BCMLGPYB"


SPONSORED_SEARCH_LINK = (
    "https://www.amazon.com/INTO-AM-T-Shirt-Heather-3X-Large/dp/B083JNRL62/ref=sr_1_1_sspa?_encoding=UTF8"
    "&content-id=amzn1.sym.6a9b1bc1-425c-4df6-b738-27952a93105a&dib=eyJ2IjoiMSJ9.TRtolTVL4KZB&dib_tag=se"
    "&keywords=fitness%2Bclothing&pd_rd_r=dfa2711c-5a2e-42d6-b99b-9dfd6e918b82&qid=1792485189"
    "&sr=8-1-spons&th=1&psc=1"
)


def test_numbers_in_a_links_query_string_are_not_asins():
    assert extract_asins(SPONSORED_SEARCH_LINK) == ["B083JNRL62"]  # qid=1792485189 is a timestamp, not an ISBN


def test_links_and_bare_asins_mixed_in_one_csv_row_stay_separate():
    text = f"{SPONSORED_SEARCH_LINK},B09B8V1LZ3;www.amazon.com/dp/0735211299 amazon.com/gp/product/B07XJ8C8F5"
    assert extract_asins(text) == ["B083JNRL62", "B09B8V1LZ3", "0735211299", "B07XJ8C8F5"]


def test_a_link_without_a_product_path_gives_no_asin():
    assert extract_asins("https://www.amazon.com/s?k=shirts&qid=1792485189") == []
