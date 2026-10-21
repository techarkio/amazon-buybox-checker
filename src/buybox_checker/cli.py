"""Command line: check the Amazon buybox of ASINs in all 50 US states and save a CSV.

Input:  ASINs or Amazon product links (arguments and/or a .txt/.csv file), optional state codes,
        the number of browser sessions on this computer, and optional remote Playwright servers.
Output: <data folder>/results/buybox_<timestamp>.csv with one row per ASIN x state, plus a summary per ASIN.

Examples:
    buybox-checker B0BCMLGPYB https://www.amazon.com/dp/B09B8V1LZ3
    buybox-checker --asin-file asins.csv --local-sessions 3
    buybox-checker B0BCMLGPYB --states NY CA --local-sessions 1 --headed
    buybox-checker B0BCMLGPYB --states core-growth    # core and growth markets: about 65% of US Amazon GMV
    buybox-checker --asin-file asins.txt --remote ws://192.168.1.20:3000/ --remote ws://192.168.1.21:3000/
"""

import argparse
import logging
from datetime import datetime
from pathlib import Path

from buybox_checker import engine
from buybox_checker.asin_input import decode_upload, extract_asins, find_short_links
from buybox_checker.config import RESULTS_DIR
from buybox_checker.resources import parse_sessions, sessions_or_auto
from buybox_checker.results import append_row, read_rows, summarize, summary_line
from buybox_checker.states import PRESETS, pick_states

logger = logging.getLogger(__name__)
LOG_FORMAT = "%(asctime)s %(levelname)s [%(threadName)s] %(message)s"


def parse_args() -> argparse.Namespace:
    """Read the command-line options."""
    parser = argparse.ArgumentParser(description="Check the Amazon buybox of ASINs in all 50 US states.")
    parser.add_argument("asins", nargs="*", help="ASINs or Amazon product links, e.g. B0BCMLGPYB")
    parser.add_argument("--asin-file", type=Path, help=".txt or .csv file with ASINs or links (any layout)")
    parser.add_argument("--states", nargs="+",
                        help=f"only these state codes or presets, e.g. NY CA or {' / '.join(PRESETS)}"
                             " (default: all 50)")
    parser.add_argument(
        "--local-sessions", type=parse_sessions, default=None, metavar="N|auto",
        help="browsers to run on this computer (default auto: sized from the free CPU and memory; 0 = remote only)",
    )
    parser.add_argument(
        "--remote", action="append", default=[], metavar="WS_URL",
        help="Playwright server to run one more session on, e.g. ws://192.168.1.20:3000/ (repeatable)",
    )
    parser.add_argument("--headed", action="store_true", help="show the local browser windows")
    return parser.parse_args()


def load_asins(cli_values: list[str], asin_file: Path | None) -> list[str]:
    """Collect ASINs from the arguments and the file, stopping with a message when there are none."""
    text = " ".join(cli_values)
    if asin_file:
        text += "\n" + decode_upload(asin_file.read_bytes())
    for link in find_short_links(text):
        logger.warning("Skipping short link %s: open it and paste the full amazon.com link instead", link)
    asins = extract_asins(text)
    if not asins:
        raise SystemExit("No ASINs found. Give ASINs (e.g. B0BCMLGPYB) or amazon.com/dp/... links.")
    return asins


def main() -> None:
    """Check every ASIN x state pair with all sessions, then save and summarize the results."""
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    args = parse_args()
    args.local_sessions = sessions_or_auto(args.local_sessions)
    if args.local_sessions < 0 or (args.local_sessions == 0 and not args.remote):
        raise SystemExit("Need at least one session: --local-sessions 1 or more, or --remote ws://...")
    asins = load_asins(args.asins, args.asin_file)
    try:
        states = pick_states(args.states)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    csv_path = RESULTS_DIR / f"buybox_{datetime.now():%Y%m%d_%H%M%S}.csv"

    logger.info("Checking %d ASIN(s) x %d state(s) = %d checks with %d local + %d remote session(s)",
                len(asins), len(states), len(asins) * len(states), args.local_sessions, len(args.remote))
    engine.run_checks(asins, states, lambda row: append_row(csv_path, row), local_sessions=args.local_sessions,
                      remote_urls=args.remote, is_headed=args.headed)
    for summary in summarize(read_rows(csv_path)):
        logger.info(summary_line(summary))
    logger.info("Results saved to %s", csv_path)


if __name__ == "__main__":
    main()
