"""FastAPI application for the GitHub Social Agent dashboard.

Serves:
  * JSON API  — /api/*   (stats, users, profiles, actions, jobs, config)
  * Frontend  — static build of ``frontend/dist`` when present (SPA).

Job control runs the bot's CLI modes as subprocesses and records
their lifecycle in the ``job_runs`` table so the UI can display
running/finished jobs.

Run (local only — the API can start bot jobs and has no auth):
    uvicorn api.app:app --host 127.0.0.1 --port 8000
"""

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from api.queries import (
    activity_timeline,
    followers_history,
    github_api_usage,
    language_options,
    list_users,
    overview_stats,
    recent_actions,
    score_buckets,
    status_distribution,
    user_profile,
)
from config import DAILY_FOLLOW_LIMIT
from database import Database

ROOT = Path(__file__).resolve().parent.parent

# Bot CLI modes that may be started from the dashboard.
# Map: dashboard mode -> argv fragment.
JOB_MODES = {
    "collect": ["--collect"],
    "collect-users": ["--collect-users"],
    "collect-users-rep": ["--collect-users-rep"],
    "collect-self": ["--collect-self"],
    "score": ["--score"],
    "follow": ["--follow"],
    "silent": ["--silent"],
    "snapshot": ["--snapshot"],
}

app = FastAPI(title="GitHub Social Dashboard", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Job runner (subprocess + DB bookkeeping) ─────────────────────────────
# Lives at module scope so the whole app shares the same Popen handles.

_job_processes: dict[int, subprocess.Popen] = {}
# Job ids whose subprocess is still being started (row created but Popen
# not yet returned).  Cleanup must skip these to avoid racing a spawn.
_starting_jobs: set[int] = set()
_job_lock = threading.Lock()
_start_lock = threading.Lock()

# Orphaned jobs (API restarted) that we have already SIGTERM'd — the bot
# shuts down gracefully and self-reports its real outcome.  If a job is
# still RUNNING after this grace window, it is force-marked FAILED so the
# mode can be relaunched.  Generous because a SIGTERM'd bot may be partway
# through a long scoring/collection cycle before it can exit.
_orphan_sigterm_at: dict[int, float] = {}
_ORPHAN_GRACE_SECONDS = 900


def _db():
    """A fresh Database connection per call (SQLite is not thread-safe)."""
    db = Database()
    # WAL + busy_timeout make short API writes (mark/finish job) wait for
    # the bot's concurrent writes instead of raising "database is locked".
    db.conn.execute("PRAGMA journal_mode=WAL")
    db.conn.execute("PRAGMA busy_timeout=5000")
    return db


def _is_bot_process(pid):
    """True if *pid* looks like a running bot subprocess.

    Guards against PID reuse: before sending SIGTERM to an orphaned job's
    pid we verify the process command line points at this project's
    main.py.  On non-Linux (or when /proc is unavailable) we fall back to
    a plain liveness check.
    """
    cmdline = Path(f"/proc/{pid}/cmdline")
    if not cmdline.is_file():
        # Non-Linux fallback: only a liveness check, so PID reuse could in
        # theory match an unrelated process.  Acceptable for a local tool,
        # but keep it noted for anyone hardening this later.
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        args = cmdline.read_bytes().split(b"\0")
    except OSError:
        return False
    return any(b"main.py" in a for a in args if a)


def _spawn_job(mode):
    """Create a job_run row and start ``python main.py <args>``.

    Returns the new job id.
    """
    db = _db()
    try:
        job_id = db.create_job_run(mode)
    except Exception:
        db.conn.close()
        raise
    db.conn.close()

    # Register the id as "starting" before Popen so that a concurrent
    # _cleanup_stale_jobs() never races the spawn.  (In the tiny window
    # between the INSERT above and this add the row is PENDING with pid
    # NULL, so cleanup could at worst flag it FAILED — a harmless flicker
    # that mark_job_running + the watcher overwrite.)
    with _job_lock:
        _starting_jobs.add(job_id)

    # If the subprocess cannot start, finish the row we just created so it
    # never lingers as PENDING (which would block relaunching the mode).
    try:
        log_dir = ROOT / "logs" / "jobs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"job_{job_id}.log"
        argv = [sys.executable, str(ROOT / "main.py")] + JOB_MODES[mode]
        # Tell the bot which job_run row it belongs to so it can
        # self-report its real outcome on exit (survives API restarts).
        env = os.environ.copy()
        env["GITHUB_SOCIAL_JOB_ID"] = str(job_id)
        with open(log_path, "w", encoding="utf-8") as logf:
            proc = subprocess.Popen(
                argv,
                cwd=str(ROOT),
                stdout=logf,
                stderr=subprocess.STDOUT,
                env=env,
            )
    except Exception as exc:
        with _job_lock:
            _starting_jobs.discard(job_id)
        db = _db()
        try:
            db.finish_job(job_id, -1, error=f"Failed to start: {exc}")
        finally:
            db.conn.close()
        raise

    with _job_lock:
        _starting_jobs.discard(job_id)
        _job_processes[job_id] = proc

    # Mark RUNNING with the OS pid
    db = _db()
    try:
        db.mark_job_running(job_id, proc.pid)
    finally:
        db.conn.close()

    # Watcher thread: wait for exit, then persist the outcome.
    threading.Thread(
        target=_watch_job, args=(job_id, proc, log_path), daemon=True
    ).start()

    return job_id


def _watch_job(job_id, proc, log_path):
    exit_code = proc.wait()
    error = None
    if exit_code != 0:
        # Grab the tail of the log to surface the failure reason.
        try:
            lines = Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()
            tail = [l for l in lines if l.strip()][-25:]
            error = "\n".join(tail)[-4000:]
        except OSError:
            error = None
    db = _db()
    try:
        # The bot may already have self-reported its outcome (see
        # main.py) — respect that, don't overwrite it.
        current = db.get_job(job_id)
        if current and current["status"] in ("RUNNING", "PENDING"):
            db.finish_job(job_id, exit_code, error)
    finally:
        db.conn.close()
    with _job_lock:
        _job_processes.pop(job_id, None)


def _finish_if_still_running(db, job, exit_code, error=None):
    """Mark *job* finished only if it has not already resolved.

    The bot subprocess self-reports its real outcome (main.py), so if it
    committed SUCCESS/FAILED between our snapshot and this write, do not
    clobber its verdict with a cleanup guess.
    """
    current = db.get_job(job["id"])
    if current and current["status"] not in ("RUNNING", "PENDING"):
        return
    db.finish_job(job["id"], exit_code, error)


def _cleanup_stale_jobs():
    """Reconcile RUNNING/PENDING jobs with reality.

    Handles two cases:
      * the API server restarted while a job ran — the watcher thread is
        gone, so any job this process did not spawn is orphaned;
      * the child process died without the watcher persisting it.

    Jobs currently tracked by :data:`_job_processes` or still being
    started are skipped to avoid racing the watcher / spawn.

    For an orphaned job whose bot process is **still alive**, we send a
    graceful SIGTERM but do NOT mark it FAILED: the bot finishes the
    current unit of work and self-reports its real outcome (SUCCESS if it
    completed) via ``GITHUB_SOCIAL_JOB_ID`` — so a job that actually did
    its work is never shown as failed just because the API restarted.  If
    the job is still RUNNING after :data:`_ORPHAN_GRACE_SECONDS`, it is
    force-marked FAILED so the mode can be relaunched.

    Only jobs whose process is **dead** (container killed it, spawn never
    finished, or pid reused) are marked FAILED immediately — they cannot
    self-report.
    """
    db = _db()
    try:
        running = db.running_jobs()
        with _job_lock:
            tracked = set(_job_processes.keys()) | set(_starting_jobs)
            running_ids = {job["id"] for job in running}
            # Drop tracking for jobs that already resolved (self-reported
            # or force-marked) so the dict doesn't grow unboundedly.
            for stale_id in [i for i in _orphan_sigterm_at if i not in running_ids]:
                _orphan_sigterm_at.pop(stale_id, None)
        now = time.monotonic()
        for job in running:
            if job["id"] in tracked:
                continue
            pid = job.get("pid")
            if pid and _is_bot_process(pid):
                with _job_lock:
                    first_signal = job["id"] not in _orphan_sigterm_at
                    if first_signal:
                        _orphan_sigterm_at[job["id"]] = now
                    sig_time = _orphan_sigterm_at.get(job["id"])
                if first_signal:
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except OSError:
                        pass
                    continue
                if sig_time is not None and now - sig_time < _ORPHAN_GRACE_SECONDS:
                    continue  # give the bot time to self-report
                # Grace expired — the bot did not exit in time.
                with _job_lock:
                    _orphan_sigterm_at.pop(job["id"], None)
                _finish_if_still_running(
                    db, job, -1,
                    error="Interrupted (job did not stop after restart).",
                )
                continue
            # No live bot process — it cannot self-report.
            with _job_lock:
                _orphan_sigterm_at.pop(job["id"], None)
            _finish_if_still_running(
                db, job, -1,
                error="Interrupted (API restarted or process died).",
            )
    finally:
        db.conn.close()


# ── API routes ────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/stats")
def stats():
    db = _db()
    try:
        _cleanup_stale_jobs()
        # Keep the request log small — the per-hour meter only needs
        # recent history.
        db.prune_api_requests()
        return {
            **overview_stats(db, DAILY_FOLLOW_LIMIT),
            "github_usage": github_api_usage(db),
            "activity": activity_timeline(db, days=30),
            "followers_history": followers_history(db, days=30),
            "status_distribution": status_distribution(db),
            "score_buckets": score_buckets(db),
            "recent_actions": recent_actions(db),
            "jobs": db.list_jobs(limit=8),
        }
    finally:
        db.conn.close()


@app.get("/api/users")
def users(
    q: str | None = Query(None),
    status: str | None = Query(None),
    language: str | None = Query(None),
    ml: str | None = Query(None),  # "1" | "0" | "none"
    min_score: int | None = Query(None),
    sort: str = Query("score"),
    order: str = Query("desc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
):
    db = _db()
    try:
        ml_filter = None
        if ml in ("1", "0"):
            ml_filter = 1 if ml == "1" else 0
        elif ml == "none":
            ml_filter = "none"  # handled inside list_users (IS NULL)
        items, total = list_users(
            db,
            q=q,
            status=status,
            language=language,
            ml=ml_filter,
            min_score=min_score,
            sort=sort,
            order=order,
            page=page,
            per_page=per_page,
        )
        return {"items": items, "total": total, "page": page, "per_page": per_page}
    finally:
        db.conn.close()


@app.get("/api/users/{username}")
def user_detail(username: str):
    db = _db()
    try:
        profile = user_profile(db, username)
    finally:
        db.conn.close()
    if profile is None:
        raise HTTPException(status_code=404, detail="User not found")
    return profile


@app.get("/api/actions")
def actions(days: int = Query(30, ge=1, le=365)):
    db = _db()
    try:
        return {"activity": activity_timeline(db, days=days)}
    finally:
        db.conn.close()


@app.get("/api/languages")
def languages():
    db = _db()
    try:
        return {"items": language_options(db)}
    finally:
        db.conn.close()


@app.get("/api/config")
def config_view():
    import config as cfg

    return {
        "my_username": cfg.MY_USERNAME,
        "daily_follow_limit": cfg.DAILY_FOLLOW_LIMIT,
        "follow_delay": cfg.FOLLOW_DELAY,
        "score_threshold": cfg.SILENT_FOLLOW_SCORE_THRESHOLD,
        "current_score_version": cfg.CURRENT_SCORE_VERSION,
        "ml_enabled": cfg.ML_ENABLED,
        "ml_train_interval_hours": cfg.ML_TRAIN_INTERVAL_HOURS,
        "repo_freshness_days": cfg.REPO_FRESHNESS_DAYS,
        "owner_sync_days": cfg.OWNER_SYNC_DAYS,
        "score_freshness_days": cfg.SCORE_FRESHNESS_DAYS,
        "follower_scan_days": cfg.FOLLOWER_SCAN_DAYS,
        "prioritize_small": cfg.SILENT_PRIORITIZE_SMALL,
    }


# ── Job control ───────────────────────────────────────────────────────────

class JobStart(BaseModel):
    mode: str


@app.get("/api/jobs")
def jobs_list():
    db = _db()
    try:
        _cleanup_stale_jobs()
        return {"items": db.list_jobs(limit=50)}
    finally:
        db.conn.close()


@app.post("/api/jobs")
def jobs_start(payload: JobStart):
    if payload.mode not in JOB_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown mode '{payload.mode}'. Allowed: {sorted(JOB_MODES)}",
        )

    # Guard the check-then-spawn with a lock so two concurrent requests for
    # the same mode cannot both pass the "already running" check.
    with _start_lock:
        db = _db()
        try:
            _cleanup_stale_jobs()
            running = [j for j in db.running_jobs() if j["mode"] == payload.mode]
            if running:
                raise HTTPException(
                    status_code=409,
                    detail=f"Mode '{payload.mode}' is already running (job #{running[0]['id']}).",
                )
        finally:
            db.conn.close()

        try:
            job_id = _spawn_job(payload.mode)
        except Exception as exc:  # pragma: no cover - subprocess failure
            raise HTTPException(status_code=500, detail=f"Failed to start job: {exc}")

    db = _db()
    try:
        return db.get_job(job_id)
    finally:
        db.conn.close()


@app.get("/api/jobs/{job_id}")
def jobs_detail(job_id: int):
    db = _db()
    try:
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return job
    finally:
        db.conn.close()


@app.get("/api/jobs/{job_id}/log")
def jobs_log(job_id: int):
    log_path = ROOT / "logs" / "jobs" / f"job_{job_id}.log"
    if not log_path.is_file():
        raise HTTPException(status_code=404, detail="Log not found")
    return JSONResponse({"log": log_path.read_text(encoding="utf-8", errors="replace")[-20000:]})


# ── Static frontend (SPA) ─────────────────────────────────────────────────

_DIST = (ROOT / "frontend" / "dist").resolve()


def _register_spa_routes():
    """Serve the built SPA.

    The catch-all is always registered so the UI appears as soon as
    ``frontend/dist`` exists (even if the server started before the build).
    """
    assets = _DIST / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        # Unknown /api/* URLs should return JSON 404, not the SPA shell.
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        if not _DIST.is_dir():
            return JSONResponse(
                {
                    "detail": "Frontend not built. "
                    "Run: cd frontend && npm run build"
                },
                status_code=404,
            )
        candidate = (_DIST / full_path).resolve()
        # Only serve files that actually live inside dist/
        if full_path and candidate.is_file() and candidate.is_relative_to(_DIST):
            return FileResponse(candidate)
        index = _DIST / "index.html"
        if index.is_file():
            return FileResponse(index)
        return JSONResponse({"detail": "Not found"}, status_code=404)


_register_spa_routes()
