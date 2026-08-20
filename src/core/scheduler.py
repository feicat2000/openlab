"""
Run Scheduler – queue and schedule protocol runs using APScheduler.

Allows immediate (ASAP) or time-triggered execution, with priority ordering
and persistent queue state via JSON.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from src.core.protocol_parser import ProtocolParser
from src.core.orchestrator import Orchestrator


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class ScheduledJob:
    job_id: str
    protocol_path: str          # stored as string for JSON serialisation
    scheduled_for: Optional[float] = None   # Unix timestamp; None = ASAP
    priority: int = 5           # 1 = highest, 10 = lowest
    simulate: bool = True
    skip_steps: List[int] = field(default_factory=list)
    status: str = "queued"      # queued|running|completed|failed|cancelled
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    error: Optional[str] = None


# ── ProtocolScheduler ─────────────────────────────────────────────────────────

class ProtocolScheduler:
    """
    Schedule protocol runs via APScheduler.

    Usage::

        sched = ProtocolScheduler(config={}, simulate=True)
        sched.start()
        job_id = sched.submit(Path("protocols/dna.yaml"), priority=3)
        sched.stop()
    """

    def __init__(
        self,
        config: dict,
        simulate: bool = True,
        runs_dir: Path = Path("runs"),
    ) -> None:
        self._config = config
        self._simulate = simulate
        self._runs_dir = Path(runs_dir)
        self._jobs: Dict[str, ScheduledJob] = {}
        self._jobs_lock = threading.Lock()
        self._scheduler = None   # APScheduler instance, created lazily

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        from apscheduler.schedulers.background import BackgroundScheduler  # type: ignore
        self._scheduler = BackgroundScheduler()
        self._scheduler.start()
        logger.info("ProtocolScheduler started")
        # Re-schedule any queued jobs that were loaded from state
        for job in list(self._jobs.values()):
            if job.status == "queued":
                self._schedule_job(job)

    def stop(self) -> None:
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        logger.info("ProtocolScheduler stopped")

    # ── Job submission ────────────────────────────────────────────────────────

    def submit(
        self,
        protocol_path,
        scheduled_for: Optional[float] = None,
        priority: int = 5,
        simulate: Optional[bool] = None,
        skip_steps: Optional[List[int]] = None,
    ) -> str:
        job = ScheduledJob(
            job_id=uuid.uuid4().hex[:12],
            protocol_path=str(protocol_path),
            scheduled_for=scheduled_for,
            priority=priority,
            simulate=simulate if simulate is not None else self._simulate,
            skip_steps=skip_steps or [],
            status="queued",
            created_at=time.time(),
        )
        with self._jobs_lock:
            self._jobs[job.job_id] = job
        logger.info(f"Job {job.job_id} submitted: {protocol_path} (priority={priority})")

        if self._scheduler and self._scheduler.running:
            self._schedule_job(job)

        return job.job_id

    def _schedule_job(self, job: ScheduledJob) -> None:
        """Register the job with APScheduler."""
        from apscheduler.triggers.date import DateTrigger  # type: ignore
        import datetime

        if job.scheduled_for:
            run_time = datetime.datetime.fromtimestamp(job.scheduled_for)
            trigger = DateTrigger(run_date=run_time)
        else:
            # ASAP – use a date trigger 1 second from now
            trigger = DateTrigger(
                run_date=datetime.datetime.now() + datetime.timedelta(seconds=1)
            )

        self._scheduler.add_job(
            func=self._run_job,
            trigger=trigger,
            args=[job.job_id],
            id=job.job_id,
            replace_existing=True,
        )

    # ── Job execution ─────────────────────────────────────────────────────────

    def _run_job(self, job_id: str) -> None:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
        if job is None:
            logger.error(f"Job {job_id} not found")
            return
        if job.status == "cancelled":
            logger.info(f"Job {job_id} was cancelled, skipping")
            return

        with self._jobs_lock:
            job.status = "running"
            job.started_at = time.time()
        logger.info(f"Executing job {job_id}: {job.protocol_path}")

        try:
            parser = ProtocolParser()
            doc = parser.load(job.protocol_path)
            orch = Orchestrator(config=self._config, simulate=job.simulate)
            self._runs_dir.mkdir(parents=True, exist_ok=True)
            orch.execute(
                doc,
                runs_dir=self._runs_dir,
                confirm_before_run=False,
                skip_steps=job.skip_steps,
            )
            with self._jobs_lock:
                job.status = "completed"
        except Exception as exc:
            with self._jobs_lock:
                job.status = "failed"
                job.error = str(exc)
            logger.error(f"Job {job_id} failed: {exc}")
        finally:
            with self._jobs_lock:
                job.ended_at = time.time()

    # ── Job management ────────────────────────────────────────────────────────

    def cancel(self, job_id: str) -> bool:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.status not in ("queued",):
                logger.warning(f"Cannot cancel job {job_id} in state '{job.status}'")
                return False
            job.status = "cancelled"
        if self._scheduler:
            try:
                self._scheduler.remove_job(job_id)
            except Exception:
                pass
        logger.info(f"Job {job_id} cancelled")
        return True

    def list_jobs(
        self,
        status_filter: Optional[str] = None,
    ) -> List[ScheduledJob]:
        with self._jobs_lock:
            jobs = list(self._jobs.values())
        if status_filter:
            jobs = [j for j in jobs if j.status == status_filter]
        # Sort by priority then created_at
        return sorted(jobs, key=lambda j: (j.priority, j.created_at))

    def get_job(self, job_id: str) -> Optional[ScheduledJob]:
        with self._jobs_lock:
            return self._jobs.get(job_id)

    # ── Persistence ───────────────────────────────────────────────────────────

    def save_state(self, path: Path) -> None:
        path = Path(path)
        with self._jobs_lock:
            data = {jid: asdict(job) for jid, job in self._jobs.items()}
        with open(path, "w") as fh:
            json.dump(data, fh, indent=2)
        logger.info(f"Scheduler state saved: {path}")

    def load_state(self, path: Path) -> None:
        path = Path(path)
        if not path.exists():
            return
        with open(path) as fh:
            data = json.load(fh)
        with self._jobs_lock:
            self._jobs = {}
            for jid, jdata in data.items():
                self._jobs[jid] = ScheduledJob(**jdata)
        logger.info(f"Scheduler state loaded: {len(self._jobs)} jobs from {path}")
