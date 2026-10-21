"""Where the checker keeps its files.

Everything lives under one data folder, ~/.buybox-checker by default, so every way of running the
checker shares the same cached zipcode sessions no matter which folder it starts in.
Set the BUYBOX_CHECKER_HOME environment variable to use another folder.

BUYBOX_CHECKER_REMOTE lists remote Playwright servers (comma-separated ws:// URLs); the CLI and the web UI
also take them as --remote options. BUYBOX_CHECKER_PROXIES lists HTTP proxies for the local browsers
(see proxies.py).

BUYBOX_CHECKER_MAX_ASINS caps the ASINs per web UI check (default 500), and BUYBOX_CHECKER_CONTACT_TECHARK=1
shows "contact TECHARK" where the page talks about that limit (used by TECHARK's hosted site).
BUYBOX_CHECKER_PUBLIC=1 is for a site anyone can open: visitors only see the checks they started or were
given a link to, never the latest check of someone else.
"""

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("BUYBOX_CHECKER_HOME", Path.home() / ".buybox-checker")).expanduser()
SESSIONS_DIR = DATA_DIR / "sessions"  # cached Amazon cookies, one file per zipcode
RESULTS_DIR = DATA_DIR / "results"  # one CSV per run
JOBS_DIR = DATA_DIR / "jobs"  # what each web UI job asked for and how it ended, so jobs survive a restart
DEBUG_DIR = DATA_DIR / "debug_pages"  # HTML of pages that did not load as expected


MAX_ASINS_ENV = "BUYBOX_CHECKER_MAX_ASINS"
CONTACT_ENV = "BUYBOX_CHECKER_CONTACT_TECHARK"
PUBLIC_ENV = "BUYBOX_CHECKER_PUBLIC"
DEFAULT_MAX_ASINS = 500
TRUE_WORDS = ("1", "true", "yes", "on")


def max_asins_from_env() -> int:
    """Return the most ASINs one web UI check may hold (BUYBOX_CHECKER_MAX_ASINS, default 500)."""
    value = os.environ.get(MAX_ASINS_ENV, "").strip()
    return int(value) if value else DEFAULT_MAX_ASINS


def contact_from_env() -> bool:
    """Tell whether the page should point people to TECHARK for bigger checks (BUYBOX_CHECKER_CONTACT_TECHARK)."""
    return os.environ.get(CONTACT_ENV, "").strip().lower() in TRUE_WORDS


def public_from_env() -> bool:
    """Tell whether the site is open to anyone (BUYBOX_CHECKER_PUBLIC): then nobody browses others' checks."""
    return os.environ.get(PUBLIC_ENV, "").strip().lower() in TRUE_WORDS


def remote_urls_from_env() -> list[str]:
    """Return the remote Playwright server URLs listed in BUYBOX_CHECKER_REMOTE (comma-separated)."""
    return [url.strip() for url in os.environ.get("BUYBOX_CHECKER_REMOTE", "").split(",") if url.strip()]
