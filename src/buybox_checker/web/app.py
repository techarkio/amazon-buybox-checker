"""Web UI: paste Amazon links/ASINs or upload a .txt/.csv file, then watch the buybox fill a US map.

Input:  HTTP requests from the page in static/ (start, poll, cancel, retry, download a job; list its products).
Output: JSON about jobs, their products and the states, the CSV of a job, and the static page itself.
        Polling returns a summary, never every row: a 500-product, 50-state job has 25,000 rows. The
        optional "store" query parameter is the user's seller name; results are measured against it.
Constraints: the server runs checks with this computer's IP and browsers, so it listens on 127.0.0.1 by
        default, and every POST must carry the X-Buybox-Checker header, which another website open in
        the same browser cannot send without a CORS preflight that this server never approves.
        When it is reachable by others (e.g. deployed in a container), set BUYBOX_CHECKER_PASSWORD: every
        page and API call then needs that password (HTTP Basic auth, any user name), except the health check.
"""

import argparse
import base64
import binascii
import logging
import os
import re
import secrets
import threading
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from buybox_checker import engine, http_page
from buybox_checker.asin_input import decode_upload, extract_asins, find_short_links
from buybox_checker.catalog import (
    FILTER_ALL,
    FILTER_ATTENTION,
    GROUPS,
    SORT_RISK,
    SORTS,
    build_catalog,
    overview,
    product_page,
    recent_products,
)
from buybox_checker.config import (
    JOBS_DIR,
    contact_from_env,
    max_asins_from_env,
    public_from_env,
    remote_urls_from_env,
)
from buybox_checker.jobs import STATUS_RUNNING, Job, JobBusyError, JobManager
from buybox_checker.proxies import proxies_from_env
from buybox_checker.resources import detect_resources, parse_sessions
from buybox_checker.states import GMV_TIERS, PRESETS, STATES, pick_states

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_UPLOAD_BYTES = 2_000_000
PRODUCTS_PER_PAGE = 25
MAX_PRODUCTS_PER_PAGE = 100
HISTORY_ASINS = 3  # ASINs named in a "Past checks" line; the rest are counted
MAX_LOCAL_SESSIONS = 6
MAX_RUNNING_JOBS = 20  # checks running at once when product pages go over HTTP; each IP is paced across all
WARM_UP_PRESET = "core-growth"  # zipcodes whose cookies are made on every proxy at start
APP_HEADER = "X-Buybox-Checker"
HEALTH_PATH = "/api/health"
PASSWORD_ENV = "BUYBOX_CHECKER_PASSWORD"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def require_app_header(x_buybox_checker: str | None = Header(default=None)) -> None:
    """Reject POSTs that don't come from this app's own page (see the module docstring)."""
    if x_buybox_checker != "1":
        raise HTTPException(status_code=403, detail=f"Missing {APP_HEADER} header")


def is_authorized(authorization: str | None, password: str) -> bool:
    """Tell whether an HTTP Basic 'Authorization' header carries the password (any user name)."""
    scheme, _, encoded = (authorization or "").partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        _, _, given = base64.b64decode(encoded, validate=True).decode("utf-8").partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return False
    return secrets.compare_digest(given.encode(), password.encode())


def create_app(manager: JobManager | None = None, default_sessions: int | None = None,
               remote_urls: list[str] | None = None, password: str | None = None,
               max_asins: int | None = None, contact_techark: bool | None = None,
               public: bool | None = None) -> FastAPI:
    """Build the web app around a job manager (a fresh one unless a test passes its own).

    default_sessions None ("auto") sizes the local sessions from the server's free CPU and memory at each request.
    max_asins, contact_techark and public default to the BUYBOX_CHECKER_MAX_ASINS / _CONTACT_TECHARK / _PUBLIC
    settings. A public site never hands out the latest job: a check is opened only by its (unguessable) id.
    """
    max_asins = max_asins_from_env() if max_asins is None else max_asins
    contact_techark = contact_from_env() if contact_techark is None else contact_techark
    public = public_from_env() if public is None else public
    app = FastAPI(title="Amazon Buybox Checker")
    remote_urls = remote_urls or []
    # several checks at once only over HTTP: a browser per session would not fit in memory
    concurrent = http_page.is_available() and not remote_urls
    manager = manager or JobManager(jobs_dir=JOBS_DIR, max_running=MAX_RUNNING_JOBS if concurrent else 1)

    if password:
        @app.middleware("http")
        async def require_password(request: Request, call_next):
            """Ask for the password on every request except the health check."""
            if request.url.path == HEALTH_PATH or is_authorized(request.headers.get("authorization"), password):
                return await call_next(request)
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Buybox Checker"'})

    @app.get(HEALTH_PATH)
    def health() -> dict:
        """Answer container health checks."""
        return {"status": "ok"}

    def sessions_default() -> int:
        """Return the sessions set at startup, or the server's current recommendation."""
        return detect_resources(MAX_LOCAL_SESSIONS).recommended_sessions if default_sessions is None \
            else default_sessions

    @app.get("/api/config")
    def get_config() -> dict:
        """Tell the page the states and their GMV tiers, and what it needs to estimate a check's duration."""
        return {
            "states": [vars(state) for state in STATES.values()],
            "tiers": [vars(tier) for tier in GMV_TIERS], "presets": [vars(preset) for preset in PRESETS.values()],
            "default_sessions": sessions_default(), "remote_sessions": len(remote_urls),
            "max_asins": max_asins, "contact_techark": contact_techark, "public": public,
            "seconds_per_check": engine.SECONDS_PER_CHECK,
        }

    @app.post("/api/jobs", dependencies=[Depends(require_app_header)])
    def start_job(text: str = Form(""), states: str = Form(""), local_sessions: int | None = Form(None),
                  file: UploadFile | None = File(None)) -> dict:
        """Start a job from pasted text and/or an uploaded file."""
        text = text + "\n" + read_upload(file)
        if local_sessions is None:
            local_sessions = sessions_default()
        asins = extract_asins(text)
        if not asins:
            raise HTTPException(400, "No ASINs found. Paste ASINs like B0BCMLGPYB or amazon.com/dp/... links.")
        if len(asins) > max_asins:
            more = " To check more, contact TECHARK at hello@techark.io." if contact_techark else ""
            raise HTTPException(400, f"Found {len(asins)} ASINs; check at most {max_asins} at a time.{more}")
        if not 0 <= local_sessions <= MAX_LOCAL_SESSIONS or local_sessions + len(remote_urls) == 0:
            raise HTTPException(400, f"Browser sessions must be between 1 and {MAX_LOCAL_SESSIONS}.")
        try:
            state_zipcodes = pick_states(re.split(r"[\s,]+", states))
            job = manager.start(asins, state_zipcodes, local_sessions, remote_urls)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        except JobBusyError as error:
            raise HTTPException(409, busy_message(error, public)) from error
        return {"job": job_view(job), "skipped_short_links": find_short_links(text)}

    @app.get("/api/jobs")
    def list_jobs() -> dict:
        """List the recent checks, newest first, for the page's "Past checks" (never on a public site)."""
        if public:
            raise HTTPException(404, "Past checks are not listed on a public site")
        return {"jobs": [history_item(job) for job in manager.recent()]}

    @app.get("/api/jobs/latest")
    def get_latest_job(store: str = "") -> dict:
        """Return the most recent job, so a reloaded page can pick it up again (never on a public site)."""
        job = None if public else manager.latest()
        if job is None:
            raise HTTPException(404, "No jobs yet")
        return job_view(job, store)

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, store: str = "") -> dict:
        """Return a job's progress and overview (no rows: see the products endpoints)."""
        return job_view(find_job(manager, job_id), store)

    @app.get("/api/jobs/{job_id}/products")
    def list_products(job_id: str, store: str = "", group: str = FILTER_ALL, q: str = "", sort: str = SORT_RISK,
                      page: int = Query(1, ge=1),
                      per_page: int = Query(PRODUCTS_PER_PAGE, ge=1, le=MAX_PRODUCTS_PER_PAGE)) -> dict:
        """Return one page of a job's products, filtered by group, searched and sorted."""
        if group not in (*GROUPS, FILTER_ALL, FILTER_ATTENTION):
            raise HTTPException(400, f"Unknown group: {group}")
        if sort not in SORTS:
            raise HTTPException(400, f"Unknown sort: {sort}")
        job = find_job(manager, job_id)
        catalog = build_catalog(job.asins, list(job.states), job.latest_rows(), store)
        return product_page(catalog.products, group, q, sort, page, per_page)

    @app.get("/api/jobs/{job_id}/products/{asin}")
    def get_product(job_id: str, asin: str, store: str = "") -> dict:
        """Return one product of a job: its summary and its row in each state."""
        job = find_job(manager, job_id)
        if asin not in job.asins:
            raise HTTPException(404, "This product is not in this check")
        rows = [row for row in job.latest_rows() if row["asin"] == asin]
        catalog = build_catalog([asin], list(job.states), job.latest_rows(), store)
        return {"product": catalog.products[0], "rows": rows, "reference": catalog.reference,
                "reference_is_store": catalog.reference_is_store}

    @app.post("/api/jobs/{job_id}/cancel", dependencies=[Depends(require_app_header)])
    def cancel_job(job_id: str) -> dict:
        """Stop a running job; its finished rows are kept."""
        find_job(manager, job_id)
        return job_view(manager.cancel(job_id))

    @app.post("/api/jobs/{job_id}/retry", dependencies=[Depends(require_app_header)])
    def retry_job(job_id: str) -> dict:
        """Check again the job's failed and unchecked pairs; its other rows are kept."""
        find_job(manager, job_id)
        try:
            return job_view(manager.retry(job_id, sessions_default(), remote_urls))
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        except JobBusyError as error:
            raise HTTPException(409, busy_message(error, public)) from error

    @app.get("/api/jobs/{job_id}/csv")
    def download_csv(job_id: str) -> FileResponse:
        """Download a job's CSV file."""
        job = find_job(manager, job_id)
        if not job.csv_path.exists():
            raise HTTPException(404, "No results saved yet")
        return FileResponse(job.csv_path, media_type="text/csv", filename=job.csv_path.name)

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


def busy_message(error: JobBusyError, public: bool) -> str:
    """Say what is running and what to do: visitors of a public site can only wait."""
    advice = "Please try again in a few minutes." if public else "Wait for it or stop it first."
    return f"{error} {advice}"


def job_view(job: Job, store: str = "") -> dict:
    """Describe a job for the page: progress, time left, overview and the products that just finished."""
    rows = job.latest_rows()
    states = list(job.states)
    catalog = build_catalog(job.asins, states, rows, store)
    return {
        "id": job.id, "status": job.status, "error": job.error,
        "started_at": job.started_at, "finished_at": job.finished_at,
        "asins": job.asins, "states": states, "total": job.total, "done": len(rows),
        "eta_seconds": job.eta_seconds(),
        "run": {"done": job.run_done(), "total": job.run_total},  # the current run, or the retry of failed checks
        "retryable": 0 if job.status == STATUS_RUNNING else len(job.retry_pairs()),
        "overview": overview(catalog, states),
        "recent": recent_products(catalog),
    }


def history_item(job: Job) -> dict:
    """Describe a job in one line of "Past checks": when, what, and how many of its checks worked."""
    return {
        "id": job.id, "status": job.status, "started_at": job.started_at, "finished_at": job.finished_at,
        "products": len(job.asins), "asins": job.asins[:HISTORY_ASINS], "states": list(job.states),
        "total": job.total, "checked": job.total - len(job.retry_pairs()),
    }


def read_upload(file: UploadFile | None) -> str:
    """Return an uploaded file's text, refusing files too large to be a list of ASINs."""
    if file is None:
        return ""
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"File is larger than {MAX_UPLOAD_BYTES // 1_000_000} MB")
    return decode_upload(data)


def find_job(manager: JobManager, job_id: str):
    """Return the job with this id, or answer 404."""
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(404, "No job with this id (jobs are lost when the server restarts)")
    return job


def parse_args() -> argparse.Namespace:
    """Read the command-line options."""
    parser = argparse.ArgumentParser(description="Web UI for the Amazon buybox checker.")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"address to listen on (default {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port (default {DEFAULT_PORT})")
    parser.add_argument("--local-sessions", type=parse_sessions, default=None, metavar="N|auto",
                        help="number of browsers on this computer (default auto: sized from the free CPU and memory)")
    parser.add_argument("--remote", action="append", default=[], metavar="WS_URL",
                        help="Playwright server to add one session on, e.g. ws://192.168.1.20:3000/ (repeatable;"
                             " default: the BUYBOX_CHECKER_REMOTE environment variable)")
    parser.add_argument("--no-browser", action="store_true", help="don't open the page in a browser")
    return parser.parse_args()


def main() -> None:
    """Start the web server and open the page."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s")
    args = parse_args()
    password = os.environ.get(PASSWORD_ENV) or None
    remote_urls = args.remote or remote_urls_from_env()
    proxies = proxies_from_env()  # a mistyped BUYBOX_CHECKER_PROXIES stops the server here, not at the first check
    if proxies:
        logger.info("Browsers on this machine go through %d proxies: %s", len(proxies),
                    ", ".join(proxy.name for proxy in proxies))
        warm_up_zipcodes = list(pick_states([WARM_UP_PRESET]).values())
        threading.Thread(target=engine.warm_up, args=(warm_up_zipcodes,), name="warm-up", daemon=True).start()
    if args.host not in LOOPBACK_HOSTS and not password and not public_from_env():
        logger.warning("Listening on %s without a password: anyone who can reach it can start checks from your "
                       "IP. Set %s to require one.", args.host, PASSWORD_ENV)
    url = f"http://{'localhost' if args.host in LOOPBACK_HOSTS or args.host == '0.0.0.0' else args.host}:{args.port}/"
    if not args.no_browser:
        threading.Timer(1.5, webbrowser.open, args=[url]).start()
    logger.info("Open %s%s", url, " (password protected)" if password else "")
    uvicorn.run(create_app(default_sessions=args.local_sessions, remote_urls=remote_urls, password=password),
                host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
