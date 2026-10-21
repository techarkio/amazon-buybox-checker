"""Save check results to CSV and summarize them per ASIN.

Input:  result rows (dicts with the RESULT_COLUMNS keys) as the engine produces them.
Output: one CSV file per run, and per-ASIN summaries (a dict for the page, a text line for logs).
Constraints: rows arrive from several session threads at once, so writing is locked. Each run writes
        its own new file, so re-running never duplicates rows. Summaries are pure functions.
"""

import csv
import threading
from collections import Counter
from pathlib import Path

from buybox_checker.page import STATUS_NOT_FOUND, STATUS_OK

STATUS_ERROR = "error"
STATUS_NOT_CHECKED = "not_checked"
CSV_ENCODING = "utf-8-sig"  # the BOM makes Excel show non-English seller names correctly
RESULT_COLUMNS = [
    "checked_at", "session", "asin", "state", "zipcode", "status", "attempts", "duration_seconds",
    "deliver_to", "title", "has_buybox", "price", "sold_by", "ships_from", "seller_id",
    "availability", "delivery_message", "cannot_ship", "see_all_buying_options", "error",
]

CSV_LOCK = threading.Lock()  # one session at a time writes to a results file


def append_row(csv_path: Path, row: dict) -> None:
    """Add one result row to a run's CSV file, writing the header first when the file is new."""
    with CSV_LOCK:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        is_new_file = not csv_path.exists()
        with csv_path.open("a", newline="", encoding=CSV_ENCODING) as file:
            writer = csv.DictWriter(file, fieldnames=RESULT_COLUMNS, extrasaction="ignore")
            if is_new_file:
                writer.writeheader()
            writer.writerow(row)


def write_rows(csv_path: Path, rows: list[dict]) -> None:
    """Replace a run's CSV file with these rows (used after failed checks were retried)."""
    with CSV_LOCK:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with csv_path.open("w", newline="", encoding=CSV_ENCODING) as file:
            writer = csv.DictWriter(file, fieldnames=RESULT_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)


def read_rows(csv_path: Path) -> list[dict[str, str]]:
    """Load the rows of a run's CSV file."""
    with csv_path.open(newline="", encoding=CSV_ENCODING) as file:
        return list(csv.DictReader(file))


def summarize_asin(asin: str, rows: list[dict]) -> dict:
    """Summarize one ASIN's rows: how many states have a buybox, who wins them, and what failed."""
    checked = [row for row in rows if row["status"] == STATUS_OK]
    with_buybox = [row for row in checked if row.get("has_buybox") == "yes"]
    winners = Counter(row.get("sold_by") or "Unknown seller" for row in with_buybox)
    return {
        "asin": asin,
        "title": next((row["title"] for row in checked if row.get("title")), ""),
        "not_found": any(row["status"] == STATUS_NOT_FOUND for row in rows),
        "states": len(rows),
        "with_buybox": len(with_buybox),
        "winners": [{"seller": seller, "states": count} for seller, count in winners.most_common()],
        "prices": sorted({row["price"] for row in with_buybox if row.get("price")}, key=price_value),
        "no_buybox": sorted(row["state"] for row in checked if row.get("has_buybox") != "yes"),
        "failed": [{"state": row["state"], "status": row["status"]}
                   for row in sorted(rows, key=lambda row: row["state"])
                   if row["status"] not in (STATUS_OK, STATUS_NOT_FOUND)],
    }


def summary_line(summary: dict) -> str:
    """Write a summary as one readable log line."""
    if summary["not_found"]:
        return f"{summary['asin']}: product page not found on Amazon, check the ASIN"
    winners = ", ".join(f"{w['seller']} {w['states']}" for w in summary["winners"]) or "-"
    failed = ", ".join(f"{f['state']} ({f['status']})" for f in summary["failed"]) or "none"
    return (
        f"{summary['asin']}: buybox in {summary['with_buybox']}/{summary['states']} states | "
        f"winners: {winners} | no buybox: {', '.join(summary['no_buybox']) or 'none'} | failed: {failed}"
    )


def summarize(rows: list[dict]) -> list[dict]:
    """Summarize every ASIN in the rows, in the order the ASINs first appear."""
    asins = dict.fromkeys(row["asin"] for row in rows)
    return [summarize_asin(asin, [row for row in rows if row["asin"] == asin]) for asin in asins]


def price_value(price: str) -> float:
    """Turn a price like '$1,249.99' into 1249.99, for sorting."""
    return float(price.replace("$", "").replace(",", "") or 0)
