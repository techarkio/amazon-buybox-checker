"""Run checks in the background and report their progress, for the web UI and other front ends.

Input:  ASINs, {state code: zipcode} and session settings for a new job; job ids to look up, cancel or retry.
Output: Job objects whose rows fill in while the run goes, plus a CSV file per job. Given a jobs folder,
        each job's settings and status are also saved as JSON, so jobs survive a restart.
Constraints: at most max_running jobs run at once (one by default, as a browser per session is heavy;
        with product pages over HTTP, the web app allows more, and the engine paces every IP across all of
        them). Starting or retrying a job beyond that raises JobBusyError. A job that was running when the server
        stopped comes back "interrupted": its finished rows are kept and the rest can be retried.
        Retrying adds new rows for the same ASIN x state; the latest row of each pair is the one that counts.
"""

import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from buybox_checker import engine
from buybox_checker.config import RESULTS_DIR
from buybox_checker.page import STATUS_NOT_FOUND, STATUS_OK
from buybox_checker.results import append_row, read_rows, summarize, write_rows

logger = logging.getLogger(__name__)

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"
FINAL_STATUSES = {STATUS_OK, STATUS_NOT_FOUND}  # a retry would not change these rows
MAX_LOADED_JOBS = 30  # saved jobs loaded back at start, newest first


class JobBusyError(RuntimeError):
    """Raised when a job is started or retried while another one is still running."""


@dataclass
class Job:
    """One check run: what was asked, how far it got, and its rows so far."""

    id: str
    asins: list[str]
    states: dict[str, str]
    csv_path: Path
    status: str = STATUS_RUNNING
    rows: list[dict] = field(default_factory=list)
    error: str = ""
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    finished_at: str = ""
    sessions: int = 1
    stop_event: threading.Event = field(default_factory=threading.Event)
    run_started: datetime = field(default_factory=datetime.now)  # start of the current run or retry
    run_total: int = 0  # checks the current run or retry has to do
    run_rows_before: int = 0  # rows the job had when it started; each check of a run adds one row

    @property
    def total(self) -> int:
        """Number of ASIN x state checks in this job."""
        return len(self.asins) * len(self.states)

    def latest_rows(self) -> list[dict]:
        """Return the latest row of each ASIN x state, in the order they finished."""
        latest: dict[tuple[str, str], dict] = {}
        for row in list(self.rows):  # copy: session threads keep appending
            key = (row["asin"], row["state"])
            latest.pop(key, None)
            latest[key] = row
        return list(latest.values())

    def retry_pairs(self) -> list[tuple[str, str]]:
        """Return the ASIN x state pairs that failed or were never checked."""
        done = {(row["asin"], row["state"]) for row in self.latest_rows() if row["status"] in FINAL_STATUSES}
        return [(asin, state) for asin in self.asins for state in self.states if (asin, state) not in done]

    def eta_seconds(self) -> int | None:
        """Estimate the seconds left in the current run, from its pace so far."""
        if self.status != STATUS_RUNNING:
            return None
        done = self.run_done()
        left = max(0, self.run_total - done)
        elapsed = (datetime.now() - self.run_started).total_seconds()
        if done < self.sessions or elapsed <= 0:  # too early to measure: use the typical pace
            return round(left * engine.SECONDS_PER_CHECK / max(1, self.sessions))
        return round(left * elapsed / done)

    def run_done(self) -> int:
        """Return how many checks the current run or retry has finished."""
        return len(self.rows) - self.run_rows_before

    def to_dict(self, include_rows: bool = True) -> dict:
        """Describe the job as plain JSON-ready data, with its rows and per-ASIN summaries."""
        rows = self.latest_rows()
        data = {
            "id": self.id, "status": self.status, "error": self.error,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "asins": self.asins, "states": list(self.states), "total": self.total, "done": len(rows),
            "csv_path": str(self.csv_path), "summaries": summarize(rows),
        }
        if include_rows:
            data["rows"] = rows
        return data

    def settings(self) -> dict:
        """Return what is saved to disk about this job (its rows live in its CSV file)."""
        return {"id": self.id, "asins": self.asins, "states": self.states, "csv_path": str(self.csv_path),
                "status": self.status, "error": self.error, "started_at": self.started_at,
                "finished_at": self.finished_at, "sessions": self.sessions}


class JobManager:
    """Start, track, cancel and retry check jobs, up to max_running at a time."""

    def __init__(self, run_checks=engine.run_checks, jobs_dir: Path | None = None, max_running: int = 1) -> None:
        self._run_checks = run_checks  # replaceable in tests, so no browser is needed
        self._jobs_dir = jobs_dir  # None: jobs live in memory only
        self.max_running = max_running
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        if jobs_dir:
            self._load()

    def start(self, asins: list[str], states: dict[str, str], local_sessions: int = engine.DEFAULT_LOCAL_SESSIONS,
              remote_urls: list[str] | None = None) -> Job:
        """Start a job in a background thread and return it right away."""
        if not asins or not states:
            raise ValueError("Give at least one ASIN and one state")
        settings = {"local_sessions": local_sessions, "remote_urls": remote_urls or []}
        with self._lock:
            self._refuse_if_busy()
            job_id = uuid.uuid4().hex[:12]
            csv_path = RESULTS_DIR / f"buybox_{datetime.now():%Y%m%d_%H%M%S}_{job_id}.csv"
            job = Job(id=job_id, asins=asins, states=states, csv_path=csv_path,
                      sessions=max(1, local_sessions + len(settings["remote_urls"])))
            job.run_total = job.total
            self._jobs[job_id] = job
        self._save(job)
        threading.Thread(target=self._run, args=(job, settings, None), name=f"job-{job_id}", daemon=True).start()
        return job

    def retry(self, job_id: str, local_sessions: int = engine.DEFAULT_LOCAL_SESSIONS,
              remote_urls: list[str] | None = None) -> Job | None:
        """Check again the job's failed and unchecked pairs, in the background; finished rows are kept."""
        job = self._jobs.get(job_id)
        if job is None:
            return None
        settings = {"local_sessions": local_sessions, "remote_urls": remote_urls or []}
        with self._lock:
            self._refuse_if_busy()
            pairs = job.retry_pairs()
            if not pairs:
                raise ValueError("Every check of this job already worked; there is nothing to retry")
            job.status, job.error, job.finished_at = STATUS_RUNNING, "", ""
            job.stop_event = threading.Event()
            job.sessions = max(1, local_sessions + len(settings["remote_urls"]))
            job.run_started, job.run_total = datetime.now(), len(pairs)
            job.run_rows_before = len(job.rows)
        self._save(job)
        threading.Thread(target=self._run, args=(job, settings, pairs), name=f"retry-{job_id}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        """Return the job with this id, or None."""
        return self._jobs.get(job_id)

    def latest(self) -> Job | None:
        """Return the most recently started job, or None."""
        jobs = list(self._jobs.values())  # on equal start times, the one added last is the latest
        return max(reversed(jobs), key=lambda job: job.started_at, default=None)

    def recent(self, limit: int = MAX_LOADED_JOBS) -> list[Job]:
        """Return the most recently started jobs, newest first."""
        jobs = list(reversed(self._jobs.values()))  # on equal start times, the one added last comes first
        return sorted(jobs, key=lambda job: job.started_at, reverse=True)[:limit]

    def running(self) -> Job | None:
        """Return the job that is running now, or None."""
        return next((job for job in self._jobs.values() if job.status == STATUS_RUNNING), None)

    def cancel(self, job_id: str) -> Job | None:
        """Ask a running job to stop; sessions finish their current check, the rest becomes not_checked."""
        job = self._jobs.get(job_id)
        if job and job.status == STATUS_RUNNING:
            job.stop_event.set()
        return job

    def _refuse_if_busy(self) -> None:
        """Raise JobBusyError when max_running jobs already run (call with the lock held)."""
        running = [job for job in self._jobs.values() if job.status == STATUS_RUNNING]
        if len(running) < self.max_running:
            return
        if self.max_running > 1:
            raise JobBusyError(f"{len(running)} checks are running right now, the most this site runs at once.")
        job = running[0]
        eta = job.eta_seconds()
        left = f", about {format_duration(eta)} left" if eta else ""
        raise JobBusyError(f"A check is already running: {len(job.asins)} product(s) in {len(job.states)} "
                           f"state(s), started {job.started_at[11:16]}{left}.")

    def _run(self, job: Job, settings: dict, pairs: list[tuple[str, str]] | None) -> None:
        """Run the job's checks, saving each row to the job and its CSV, then record how it ended."""
        def save_row(row: dict) -> None:
            append_row(job.csv_path, row)
            job.rows.append(row)

        extra = {"pairs": pairs} if pairs is not None else {}
        try:
            self._run_checks(job.asins, job.states, save_row, stop_event=job.stop_event, **settings, **extra)
            job.status = STATUS_CANCELLED if job.stop_event.is_set() else STATUS_DONE
        except Exception as error:
            job.status, job.error = STATUS_FAILED, engine.short_error(error)
        if pairs is not None:
            write_rows(job.csv_path, job.latest_rows())  # one row per ASIN x state again
        job.finished_at = datetime.now().isoformat(timespec="seconds")
        self._save(job)

    def _save(self, job: Job) -> None:
        """Write the job's settings and status to the jobs folder, if there is one."""
        if not self._jobs_dir:
            return
        try:
            self._jobs_dir.mkdir(parents=True, exist_ok=True)
            (self._jobs_dir / f"{job.id}.json").write_text(json.dumps(job.settings()), encoding="utf-8")
        except OSError as error:
            logger.warning("Could not save job %s: %s", job.id, error)

    def _load(self) -> None:
        """Load the newest saved jobs and their rows; jobs that were running come back interrupted."""
        files = sorted(self._jobs_dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in files[:MAX_LOADED_JOBS]:
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                csv_path = Path(saved["csv_path"])
                job = Job(id=saved["id"], asins=saved["asins"], states=saved["states"], csv_path=csv_path,
                          status=saved["status"], error=saved.get("error", ""), started_at=saved["started_at"],
                          finished_at=saved.get("finished_at", ""), sessions=saved.get("sessions", 1),
                          rows=read_rows(csv_path) if csv_path.exists() else [])
            except (OSError, ValueError, KeyError) as error:
                logger.warning("Skipping saved job %s: %s", path.name, error)
                continue
            if job.status == STATUS_RUNNING:
                job.status = STATUS_INTERRUPTED
                job.error = "The server stopped during this check. Retry to finish it."
                self._save(job)
            self._jobs[job.id] = job


def format_duration(seconds: int) -> str:
    """Write a duration as "40 s", "12 min" or "1 h 45 min"."""
    if seconds < 60:
        return f"{seconds} s"
    minutes = round(seconds / 60)
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60:02d} min"
