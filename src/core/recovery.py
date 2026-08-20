"""
Error Recovery – retry logic, checkpointing, and protocol resume.

Allows protocols to resume from the last completed step after a failure.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, List, Optional

from loguru import logger


# ── Data models ───────────────────────────────────────────────────────────────

class RecoveryStrategy(str, Enum):
    RETRY  = "retry"
    SKIP   = "skip"
    ABORT  = "abort"
    MANUAL = "manual"


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    delay_s: float = 2.0
    backoff_factor: float = 1.5
    strategy_on_exhaustion: RecoveryStrategy = RecoveryStrategy.ABORT


@dataclass
class StepCheckpoint:
    run_id: str
    protocol_name: str
    completed_step_ids: List[int]
    timestamp: float
    checkpoint_path: Optional[str] = None   # str for JSON serialisation


# ── RecoveryManager ───────────────────────────────────────────────────────────

class RecoveryManager:
    """
    Provides retry-with-backoff for individual steps and checkpoint-based
    protocol resume.

    Usage::

        rm = RecoveryManager()
        policy = RetryPolicy(max_attempts=3, delay_s=1.0, backoff_factor=2.0)
        result = rm.execute_with_retry(lambda s: do_step(s), step, policy)
    """

    def __init__(
        self,
        checkpoints_dir: Path = Path("runs/checkpoints"),
    ) -> None:
        self._dir = Path(checkpoints_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    # ── Retry logic ───────────────────────────────────────────────────────────

    def execute_with_retry(
        self,
        fn: Callable,
        step: Any,
        policy: RetryPolicy,
        logger_instance=None,
    ) -> Any:
        """
        Execute fn(step) with retry on failure.

        Delay between attempts: policy.delay_s * (policy.backoff_factor ** attempt)

        Raises the last exception if all attempts exhausted and strategy is ABORT.
        """
        log = logger_instance or logger
        last_exc: Optional[Exception] = None

        for attempt in range(policy.max_attempts):
            try:
                return fn(step)
            except Exception as exc:
                last_exc = exc
                remaining = policy.max_attempts - attempt - 1
                if remaining == 0:
                    break
                delay = policy.delay_s * (policy.backoff_factor ** attempt)
                log.warning(
                    f"Step {getattr(step, 'id', '?')} failed (attempt {attempt + 1}/"
                    f"{policy.max_attempts}): {exc}. Retrying in {delay:.1f}s…"
                )
                time.sleep(delay)

        # All attempts exhausted
        strategy = policy.strategy_on_exhaustion
        log.error(
            f"Step {getattr(step, 'id', '?')} exhausted {policy.max_attempts} attempts. "
            f"Strategy: {strategy.value}"
        )

        if strategy == RecoveryStrategy.ABORT:
            raise last_exc  # type: ignore[misc]
        elif strategy == RecoveryStrategy.SKIP:
            log.warning(f"Skipping step {getattr(step, 'id', '?')} after exhaustion")
            return None
        elif strategy == RecoveryStrategy.MANUAL:
            raise RuntimeError(
                f"Manual intervention required for step {getattr(step, 'id', '?')}: {last_exc}"
            )
        else:
            raise last_exc  # type: ignore[misc]

    # ── Checkpointing ─────────────────────────────────────────────────────────

    def save_checkpoint(
        self,
        run_id: str,
        protocol_name: str,
        completed_step_ids: List[int],
    ) -> StepCheckpoint:
        path = self._dir / f"{run_id}_checkpoint.json"
        cp = StepCheckpoint(
            run_id=run_id,
            protocol_name=protocol_name,
            completed_step_ids=list(completed_step_ids),
            timestamp=time.time(),
            checkpoint_path=str(path),
        )
        with open(path, "w") as fh:
            json.dump(asdict(cp), fh, indent=2)
        logger.debug(f"Checkpoint saved: {path} ({len(completed_step_ids)} steps done)")
        return cp

    def load_checkpoint(self, run_id: str) -> Optional[StepCheckpoint]:
        path = self._dir / f"{run_id}_checkpoint.json"
        if not path.exists():
            return None
        with open(path) as fh:
            data = json.load(fh)
        return StepCheckpoint(**data)

    def list_checkpoints(self) -> List[StepCheckpoint]:
        checkpoints: List[StepCheckpoint] = []
        for fpath in sorted(self._dir.glob("*_checkpoint.json")):
            try:
                with open(fpath) as fh:
                    data = json.load(fh)
                checkpoints.append(StepCheckpoint(**data))
            except Exception as exc:
                logger.warning(f"Could not read checkpoint {fpath}: {exc}")
        return checkpoints

    # ── Resume ────────────────────────────────────────────────────────────────

    def resume_from_checkpoint(
        self,
        doc,          # ProtocolDocument
        checkpoint: StepCheckpoint,
        orchestrator,
    ):
        """
        Filter out already-completed steps and run the remainder via the orchestrator.
        Returns a RunRecord.
        """
        done_ids = set(checkpoint.completed_step_ids)
        remaining_ids = [s.id for s in doc.steps if s.id not in done_ids]

        logger.info(
            f"Resuming run '{checkpoint.run_id}' from checkpoint: "
            f"{len(done_ids)} steps already done, {len(remaining_ids)} remaining"
        )

        return orchestrator.execute(
            doc,
            confirm_before_run=False,
            skip_steps=list(done_ids),
        )
