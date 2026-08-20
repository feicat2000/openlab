"""
Tests for ProtocolScheduler.
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.core.scheduler import ProtocolScheduler, ScheduledJob


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmpdir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def sched(tmpdir):
    return ProtocolScheduler(config={}, simulate=True, runs_dir=tmpdir / "runs")


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestSubmit:
    def test_submit_returns_job_id(self, sched):
        jid = sched.submit("protocols/test.yaml")
        assert isinstance(jid, str)
        assert len(jid) > 0

    def test_submitted_job_in_list(self, sched):
        jid = sched.submit("protocols/test.yaml")
        jobs = sched.list_jobs()
        assert any(j.job_id == jid for j in jobs)

    def test_submit_sets_priority(self, sched):
        jid = sched.submit("protocols/test.yaml", priority=3)
        job = sched.get_job(jid)
        assert job.priority == 3

    def test_submit_sets_scheduled_for(self, sched):
        future_ts = time.time() + 3600
        jid = sched.submit("protocols/test.yaml", scheduled_for=future_ts)
        job = sched.get_job(jid)
        assert job.scheduled_for == future_ts

    def test_submit_default_status_queued(self, sched):
        jid = sched.submit("protocols/test.yaml")
        assert sched.get_job(jid).status == "queued"


class TestCancel:
    def test_cancel_changes_status(self, sched):
        jid = sched.submit("protocols/test.yaml")
        result = sched.cancel(jid)
        assert result is True
        assert sched.get_job(jid).status == "cancelled"

    def test_cancel_nonexistent_returns_false(self, sched):
        assert sched.cancel("nonexistent_id") is False

    def test_cancel_running_job_returns_false(self, sched):
        jid = sched.submit("protocols/test.yaml")
        sched._jobs[jid].status = "running"
        result = sched.cancel(jid)
        assert result is False


class TestGetJob:
    def test_get_existing_job(self, sched):
        jid = sched.submit("protocols/test.yaml")
        job = sched.get_job(jid)
        assert isinstance(job, ScheduledJob)
        assert job.job_id == jid

    def test_get_nonexistent_returns_none(self, sched):
        assert sched.get_job("nope") is None


class TestListJobs:
    def test_list_all_jobs(self, sched):
        sched.submit("p1.yaml")
        sched.submit("p2.yaml")
        sched.submit("p3.yaml")
        assert len(sched.list_jobs()) == 3

    def test_list_with_status_filter(self, sched):
        jid1 = sched.submit("p1.yaml")
        jid2 = sched.submit("p2.yaml")
        sched.cancel(jid1)
        queued = sched.list_jobs(status_filter="queued")
        cancelled = sched.list_jobs(status_filter="cancelled")
        assert len(queued) == 1
        assert len(cancelled) == 1

    def test_list_sorted_by_priority(self, sched):
        sched.submit("p1.yaml", priority=5)
        sched.submit("p2.yaml", priority=1)
        sched.submit("p3.yaml", priority=3)
        jobs = sched.list_jobs()
        priorities = [j.priority for j in jobs]
        assert priorities == sorted(priorities)


class TestStatePersistence:
    def test_save_load_round_trip(self, sched, tmpdir):
        sched.submit("protocols/test.yaml", priority=2)
        state_path = tmpdir / "state.json"
        sched.save_state(state_path)

        sched2 = ProtocolScheduler(config={}, simulate=True)
        sched2.load_state(state_path)
        jobs = sched2.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].priority == 2

    def test_load_state_nonexistent_ok(self, sched, tmpdir):
        sched.load_state(tmpdir / "does_not_exist.json")  # should not raise


class TestRunJob:
    def test_run_job_calls_orchestrator(self, sched, tmpdir):
        protocol_yaml = tmpdir / "proto.yaml"
        protocol_yaml.write_text("""
metadata:
  name: Test
  safety_level: BSL1
steps:
  - id: 1
    name: Wait
    type: wait
    params:
      duration_s: 0
""")
        jid = sched.submit(protocol_yaml, simulate=True)

        mock_orch = MagicMock()
        mock_orch.execute.return_value = MagicMock(status="completed")
        mock_parser = MagicMock()
        mock_doc = MagicMock()
        mock_parser.return_value.load.return_value = mock_doc

        with patch("src.core.scheduler.ProtocolParser", mock_parser):
            with patch("src.core.scheduler.Orchestrator", return_value=mock_orch):
                sched._run_job(jid)

        mock_orch.execute.assert_called_once()
        assert sched.get_job(jid).status == "completed"

    def test_run_job_handles_failure(self, sched, tmpdir):
        protocol_yaml = tmpdir / "bad.yaml"
        protocol_yaml.write_text("metadata:\n  name: Bad\n")
        jid = sched.submit(protocol_yaml, simulate=True)

        mock_parser_cls = MagicMock()
        mock_parser_cls.return_value.load.side_effect = RuntimeError("parse error")
        with patch("src.core.scheduler.ProtocolParser", mock_parser_cls):
            sched._run_job(jid)

        job = sched.get_job(jid)
        assert job.status == "failed"
        assert "parse error" in job.error

    def test_run_cancelled_job_skips(self, sched):
        jid = sched.submit("protocols/test.yaml")
        sched.cancel(jid)
        # Should not raise or call orchestrator
        mock_orch_cls = MagicMock()
        with patch("src.core.scheduler.Orchestrator", mock_orch_cls):
            sched._run_job(jid)
            mock_orch_cls.assert_not_called()
