"""Find ASINs in whatever the user gives: Amazon links, pasted lists, or a .txt/.csv file's text.

Input:  any text (one ASIN per line, comma/space separated, CSV with any columns, or product links).
Output: the ASINs found, upper-cased, in first-seen order, without duplicates.
Constraints: pure functions, no network. Short links (amzn.to, a.co) hide the ASIN behind a redirect,
        so they are reported back instead of being followed.
"""

import re

# The text is read as a series of either whole links or bare ASINs. A link is taken as one piece, so numbers
# in its query string (e.g. qid=1792485189, a timestamp) are never mistaken for a 10-digit book ISBN.
# Links end at whitespace, comma, semicolon, quote or angle bracket, so CSV cells stay separate.
# A bare ASIN is "B0" + 8 letters/digits, or a 10-character ISBN (books).
TOKEN_PATTERN = re.compile(
    r"(?:https?://|www\.|amazon\.)[^\s,;\"'<>]+"
    r"|(?<![A-Z0-9])(B0[A-Z0-9]{8}|\d{9}[\dX])(?![A-Z0-9])",
    re.IGNORECASE,
)
# Inside a link, only the product path counts: /dp/ASIN, /gp/product/ASIN, /gp/aw/d/ASIN (mobile), /product/ASIN
LINK_ASIN_PATTERN = re.compile(r"/(?:dp|gp/product|gp/aw/d|product)/([A-Z0-9]{10})(?![A-Z0-9])", re.IGNORECASE)
SHORT_LINK_PATTERN = re.compile(r"https?://(?:www\.)?(?:amzn\.to|a\.co)/\S+", re.IGNORECASE)


def extract_asins(text: str) -> list[str]:
    """Return every ASIN found in the text, in the order they appear."""
    found = []
    for token in TOKEN_PATTERN.finditer(text):
        bare_asin = token.group(1)
        link_asin = None if bare_asin else LINK_ASIN_PATTERN.search(token.group(0))
        if bare_asin or link_asin:
            found.append(bare_asin or link_asin.group(1))
    return list(dict.fromkeys(asin.upper() for asin in found))


def find_short_links(text: str) -> list[str]:
    """Return amzn.to / a.co short links, whose ASIN can't be read without opening them."""
    return list(dict.fromkeys(SHORT_LINK_PATTERN.findall(text)))


def decode_upload(data: bytes) -> str:
    """Turn an uploaded .txt/.csv file into text (UTF-8, with or without BOM, or Latin-1 as a fallback)."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")
