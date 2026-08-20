"""
Tests for RecoveryManager.
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.core.recovery import (
    RecoveryManager,
    RecoveryStrategy,
    RetryPolicy,
    StepCheckpoint,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmpdir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def rm(tmpdir):
    return RecoveryManager(checkpoints_dir=tmpdir / "checkpoints")


class _FakeStep:
    id = 1
    name = "Test Step"


STEP = _FakeStep()


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestExecuteWithRetry:
    def test_success_on_first_attempt(self, rm):
        fn = MagicMock(return_value="result")
        result = rm.execute_with_retry(fn, STEP, RetryPolicy())
        assert result == "result"
        fn.assert_called_once()

    def test_retries_on_failure_succeeds_on_second(self, rm):
        call_count = [0]
        def fn(step):
            call_count[0] += 1
            if call_count[0] < 2:
                raise RuntimeError("transient error")
            return "ok"

        with patch("time.sleep"):
            result = rm.execute_with_retry(fn, STEP, RetryPolicy(max_attempts=3, delay_s=0.01))
        assert result == "ok"
        assert call_count[0] == 2

    def test_raises_after_max_attempts_abort_strategy(self, rm):
        fn = MagicMock(side_effect=RuntimeError("always fails"))
        policy = RetryPolicy(
            max_attempts=3,
            delay_s=0.01,
            strategy_on_exhaustion=RecoveryStrategy.ABORT,
        )
        with patch("time.sleep"):
            with pytest.raises(RuntimeError, match="always fails"):
                rm.execute_with_retry(fn, STEP, policy)
        assert fn.call_count == 3

    def test_skip_strategy_returns_none(self, rm):
        fn = MagicMock(side_effect=RuntimeError("always fails"))
        policy = RetryPolicy(
            max_attempts=2,
            delay_s=0.01,
            strategy_on_exhaustion=RecoveryStrategy.SKIP,
        )
        with patch("time.sleep"):
            result = rm.execute_with_retry(fn, STEP, policy)
        assert result is None

    def test_manual_strategy_raises_runtime_error(self, rm):
        fn = MagicMock(side_effect=RuntimeError("always fails"))
        policy = RetryPolicy(
            max_attempts=2,
            delay_s=0.01,
            strategy_on_exhaustion=RecoveryStrategy.MANUAL,
        )
        with patch("time.sleep"):
            with pytest.raises(RuntimeError):
                rm.execute_with_retry(fn, STEP, policy)

    def test_exponential_backoff_delays(self, rm):
        fn = MagicMock(side_effect=RuntimeError("fail"))
        policy = RetryPolicy(max_attempts=3, delay_s=2.0, backoff_factor=2.0,
                             strategy_on_exhaustion=RecoveryStrategy.SKIP)
        sleep_calls = []
        with patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
            rm.execute_with_retry(fn, STEP, policy)
        # attempt 0 fails → sleep 2.0 * 2^0 = 2.0
        # attempt 1 fails → sleep 2.0 * 2^1 = 4.0
        assert len(sleep_calls) == 2
        assert sleep_calls[0] == pytest.approx(2.0)
        assert sleep_calls[1] == pytest.approx(4.0)


class TestSaveCheckpoint:
    def test_save_creates_json_file(self, rm, tmpdir):
        cp = rm.save_checkpoint("run_001", "DNA Extraction", [1, 2, 3])
        path = Path(cp.checkpoint_path)
        assert path.exists()

    def test_saved_data_correct(self, rm):
        cp = rm.save_checkpoint("run_001", "DNA Extraction", [1, 2, 3])
        path = Path(cp.checkpoint_path)
        with open(path) as fh:
            data = json.load(fh)
        assert data["run_id"] == "run_001"
        assert data["completed_step_ids"] == [1, 2, 3]


class TestLoadCheckpoint:
    def test_load_round_trip(self, rm):
        rm.save_checkpoint("run_001", "DNA Extraction", [1, 2, 3])
        cp = rm.load_checkpoint("run_001")
        assert cp is not None
        assert cp.run_id == "run_001"
        assert cp.protocol_name == "DNA Extraction"
        assert cp.completed_step_ids == [1, 2, 3]

    def test_load_nonexistent_returns_none(self, rm):
        assert rm.load_checkpoint("nonexistent_run") is None


class TestListCheckpoints:
    def test_returns_all_checkpoints(self, rm):
        rm.save_checkpoint("run_001", "P1", [1])
        rm.save_checkpoint("run_002", "P2", [1, 2])
        cps = rm.list_checkpoints()
        assert len(cps) == 2
        ids = [cp.run_id for cp in cps]
        assert "run_001" in ids
        assert "run_002" in ids

    def test_empty_dir_returns_empty(self, rm):
        assert rm.list_checkpoints() == []


class TestResumeFromCheckpoint:
    def test_resume_calls_orchestrator_with_skip_steps(self, rm):
        # Build a minimal doc stub
        mock_step1 = MagicMock()
        mock_step1.id = 1
        mock_step2 = MagicMock()
        mock_step2.id = 2
        mock_step3 = MagicMock()
        mock_step3.id = 3

        mock_doc = MagicMock()
        mock_doc.steps = [mock_step1, mock_step2, mock_step3]

        mock_orch = MagicMock()
        mock_record = MagicMock()
        mock_orch.execute.return_value = mock_record

        cp = StepCheckpoint(
            run_id="run_001",
            protocol_name="P",
            completed_step_ids=[1, 2],
            timestamp=time.time(),
        )

        result = rm.resume_from_checkpoint(mock_doc, cp, mock_orch)

        mock_orch.execute.assert_called_once()
        call_kwargs = mock_orch.execute.call_args[1]
        # Steps 1 and 2 should be skipped
        assert 1 in call_kwargs["skip_steps"]
        assert 2 in call_kwargs["skip_steps"]
        assert result is mock_record
