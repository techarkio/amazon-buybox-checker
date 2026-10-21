"""Turn a job's rows into what the results page shows: the catalog overview, pages of products, one product.

Input:  a job's ASINs and state codes, its rows (the latest row per ASIN x state wins), and optionally the
        user's store: a seller name as Amazon shows it under "Sold by".
Output: JSON-ready dicts. Each product gets one cell per state and one group; the overview adds up the
        checks, sellers, groups and the products lost in each state.
Constraints: everything is measured against one "reference" seller: the user's store when it appears in
        the results, otherwise the seller that holds the buybox most often. Pure functions over plain
        rows; a 500 x 50 job is 25,000 rows, walked once per call.
"""

import math
from collections import Counter
from dataclasses import dataclass

from buybox_checker.page import STATUS_NOT_FOUND, STATUS_OK
from buybox_checker.results import price_value

# A state's cell for one product
WIN = "win"  # the reference seller holds the buybox
OTHER = "other"  # another seller holds it
NONE = "none"  # the page loaded, but nobody holds it (only "See all buying options")
FAILED = "failed"  # the check did not work (blocked, timed out, not found, ...)
PENDING = "pending"  # not checked yet

# Product groups, worst first, the order the page lists them in
GROUP_OTHER = "other"  # another seller holds the buybox wherever anyone does: the reference holds no state
GROUP_SPLIT = "split"  # the reference holds some states, other sellers the rest
GROUP_NONE = "none"  # nobody holds the buybox in some states
GROUP_FAILED = "failed"  # some states could not be checked
GROUP_ONE = "one"  # the reference holds every state
GROUP_PENDING = "pending"  # still being checked
GROUPS = [GROUP_OTHER, GROUP_SPLIT, GROUP_NONE, GROUP_FAILED, GROUP_ONE, GROUP_PENDING]
NEEDS_ATTENTION = {GROUP_OTHER, GROUP_SPLIT, GROUP_NONE, GROUP_FAILED}
FILTER_ALL = "all"
FILTER_ATTENTION = "attention"  # every group that needs attention

SORT_RISK = "risk"  # fewest states held by the reference first
SORT_GAP = "gap"  # biggest price gap to the cheapest other winner first
SORT_TITLE = "title"
SORTS = (SORT_RISK, SORT_GAP, SORT_TITLE)
UNKNOWN_SELLER = "Unknown seller"
NAMED_SELLERS = 3  # sellers named in the overview; the rest are added up as "other sellers"
RECENT_PRODUCTS = 5


@dataclass
class Catalog:
    """Every product of a job, measured against one reference seller."""

    reference: str
    reference_is_store: bool
    store_missing: bool  # a store was given but never holds the buybox here, so the top winner is used
    products: list[dict]
    finish_order: list[str]  # ASINs in the order their last check finished


def same_seller(first: str, second: str) -> bool:
    """Tell whether two seller names are the same seller, ignoring case and spaces around them."""
    return first.strip().casefold() == second.strip().casefold()


def winner(row: dict | None) -> str:
    """Return who holds the buybox in a row, or "" when the check failed or nobody holds it."""
    if not row or row.get("status") != STATUS_OK or row.get("has_buybox") != "yes":
        return ""
    return row.get("sold_by") or UNKNOWN_SELLER


def build_catalog(asins: list[str], states: list[str], rows: list[dict], store: str = "") -> Catalog:
    """Summarize every product of a job against the user's store, or against the top winner."""
    latest: dict[tuple[str, str], dict] = {}
    for row in rows:
        latest[(row["asin"], row["state"])] = row
    wins = Counter(seller for seller in map(winner, latest.values()) if seller)
    store_match = next((seller for seller in wins if store and same_seller(seller, store)), "")
    reference = store_match or (wins.most_common(1)[0][0] if wins else "")
    products = []
    for asin in asins:
        by_state = {state: latest[(asin, state)] for state in states if (asin, state) in latest}
        products.append(summarize_product(asin, by_state, states, reference))
    finish_order = list(dict.fromkeys(reversed([row["asin"] for row in rows])))[::-1]
    return Catalog(reference, bool(store_match), bool(store) and not store_match, products, finish_order)


def cell_kind(row: dict | None, reference: str) -> str:
    """Tell what happened in one state for one product, from the reference seller's point of view."""
    if row is None:
        return PENDING
    seller = winner(row)
    if seller:
        return WIN if same_seller(seller, reference) else OTHER
    return NONE if row.get("status") == STATUS_OK else FAILED


def summarize_product(asin: str, by_state: dict[str, dict], states: list[str], reference: str) -> dict:
    """Describe one product: a cell per state, its group, who else holds the buybox and at what price."""
    cells, others = [], {}
    for state in states:
        row = by_state.get(state)
        kind = cell_kind(row, reference)
        seller = winner(row)
        cell = {"state": state, "kind": kind, "seller": seller, "price": row.get("price", "") if seller else "",
                "status": row.get("status", "") if row else ""}
        cells.append(cell)
        if kind == OTHER:
            other = others.setdefault(seller, {"seller": seller, "states": [], "prices": []})
            other["states"].append(state)
            other["prices"].append(cell["price"])
    counts = Counter(cell["kind"] for cell in cells)
    sellers = Counter(cell["seller"] for cell in cells if cell["seller"])
    your_prices = [cell["price"] for cell in cells if cell["kind"] == WIN and cell["price"]]
    other_prices = [cell["price"] for cell in cells if cell["kind"] == OTHER and cell["price"]]
    gap = (round(min(map(price_value, your_prices)) - min(map(price_value, other_prices)), 2)
           if your_prices and other_prices else None)
    return {
        "asin": asin,
        "title": next((row["title"] for row in by_state.values() if row.get("title")), ""),
        "not_found": any(row.get("status") == STATUS_NOT_FOUND for row in by_state.values()),
        "group": group_of(counts),
        "held": counts[WIN],
        "counts": {kind: counts[kind] for kind in (WIN, OTHER, NONE, FAILED, PENDING)},
        "total": len(states),
        "top_seller": sellers.most_common(1)[0][0] if sellers else "",
        "cells": cells,
        "others": [{"seller": o["seller"], "states": o["states"], "price": price_range(o["prices"])}
                   for o in sorted(others.values(), key=lambda o: -len(o["states"]))],
        "price": price_range([cell["price"] for cell in cells if cell["price"]]),
        "your_price": price_range(your_prices),
        "gap": gap,  # reference's lowest price minus the cheapest other winner's; > 0: the reference is dearer
    }


def group_of(counts: Counter) -> str:
    """Put a product in the first group that fits, worst first."""
    if counts[PENDING]:
        return GROUP_PENDING
    if counts[OTHER]:
        return GROUP_SPLIT if counts[WIN] else GROUP_OTHER
    if counts[NONE]:
        return GROUP_NONE
    if counts[FAILED]:
        return GROUP_FAILED
    return GROUP_ONE


def price_range(prices: list[str]) -> str:
    """Write prices as "$18.49" or "$18.49–$19.99"."""
    distinct = sorted({price for price in prices if price}, key=price_value)
    if not distinct:
        return ""
    return distinct[0] if len(distinct) == 1 else f"{distinct[0]}–{distinct[-1]}"


def overview(catalog: Catalog, states: list[str]) -> dict:
    """Add up the whole job: checks per seller, products per group, and products lost in each state."""
    cells = [cell for product in catalog.products for cell in product["cells"]]
    kinds = Counter(cell["kind"] for cell in cells)
    wins = Counter(cell["seller"] for cell in cells if cell["seller"])
    named = ([catalog.reference] if catalog.reference else []) + [
        seller for seller, _ in wins.most_common() if seller != catalog.reference]
    losses, takers = Counter(), {state: Counter() for state in states}
    for cell in cells:
        if cell["kind"] in (OTHER, NONE):
            losses[cell["state"]] += 1
        if cell["kind"] == OTHER:
            takers[cell["state"]][cell["seller"]] += 1
    groups = Counter(product["group"] for product in catalog.products)
    finished = [product for product in catalog.products if product["group"] != GROUP_PENDING]
    with_winner = [product for product in finished if product["top_seller"]]
    return {
        "reference": catalog.reference,
        "reference_is_store": catalog.reference_is_store,
        "store_missing": catalog.store_missing,
        "one_seller_list": bool(with_winner) and all(
            same_seller(product["top_seller"], catalog.reference) for product in with_winner),
        "checks": {"total": len(cells), WIN: kinds[WIN], OTHER: kinds[OTHER], NONE: kinds[NONE],
                   FAILED: kinds[FAILED], PENDING: kinds[PENDING]},
        "sellers": [{"seller": seller, "checks": wins[seller]} for seller in named[:NAMED_SELLERS]],
        "other_sellers_checks": sum(wins[seller] for seller in named[NAMED_SELLERS:]),
        "groups": {group: groups[group] for group in GROUPS},
        "products": len(catalog.products),
        "products_done": len(finished),
        "losses": [{"state": state, "lost": losses[state],
                    "main_taker": takers[state].most_common(1)[0][0] if takers[state] else ""} for state in states],
        "all_clear": bool(catalog.products) and len(finished) == len(catalog.products)
        and not any(groups[group] for group in NEEDS_ATTENTION),
    }


def recent_products(catalog: Catalog, count: int = RECENT_PRODUCTS) -> list[dict]:
    """Return the products whose last check finished most recently, newest first."""
    by_asin = {product["asin"]: product for product in catalog.products}
    finished = [asin for asin in reversed(catalog.finish_order) if by_asin[asin]["group"] != GROUP_PENDING]
    return [by_asin[asin] for asin in finished[:count]]


def product_page(products: list[dict], group: str = FILTER_ALL, query: str = "", sort: str = SORT_RISK,
                 page: int = 1, per_page: int = 25) -> dict:
    """Filter, search, sort and cut the products into pages; also list every matching ASIN, for previous/next."""
    if group == FILTER_ATTENTION:
        items = [product for product in products if product["group"] in NEEDS_ATTENTION]
    else:
        items = [product for product in products if group in ("", FILTER_ALL) or product["group"] == group]
    needle = query.strip().casefold()
    if needle:
        items = [p for p in items if needle in p["asin"].casefold() or needle in p["title"].casefold()]
    if sort == SORT_GAP:
        items.sort(key=lambda p: (p["gap"] is None, -(p["gap"] or 0), p["title"].casefold()))
    elif sort == SORT_TITLE:
        items.sort(key=lambda p: (p["title"].casefold() or "~", p["asin"]))
    else:
        items.sort(key=lambda p: (p["group"] == GROUP_PENDING, p["held"] / max(1, p["total"]),
                                  -p["counts"][OTHER], p["title"].casefold()))
    pages = max(1, math.ceil(len(items) / per_page))
    page = min(max(1, page), pages)
    return {
        "total": len(items), "page": page, "pages": pages, "per_page": per_page,
        "items": items[(page - 1) * per_page:page * per_page],
        "asins": [product["asin"] for product in items],
    }
