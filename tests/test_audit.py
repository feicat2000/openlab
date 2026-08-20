"""
Tests for AuditTrail.
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import pytest

from src.audit.audit_trail import AuditTrail, AuditEntry, _compute_hash


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmpdir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def trail():
    return AuditTrail()


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestLog:
    def test_log_creates_entry(self, trail):
        entry = trail.log("alice", "protocol_started", "run_001", {"protocol": "DNA"})
        assert isinstance(entry, AuditEntry)
        assert entry.operator == "alice"
        assert entry.action == "protocol_started"
        assert entry.resource == "run_001"
        assert entry.detail == {"protocol": "DNA"}

    def test_first_entry_previous_hash_is_genesis(self, trail):
        entry = trail.log("alice", "protocol_started", "run_001")
        assert entry.previous_hash == "GENESIS"

    def test_hash_chain_links_entries(self, trail):
        e1 = trail.log("alice", "protocol_started", "run_001")
        e2 = trail.log("alice", "step_completed", "run_001")
        assert e2.previous_hash == e1.hash

    def test_entry_hash_not_empty(self, trail):
        entry = trail.log("alice", "estop", "safety_monitor")
        assert len(entry.hash) == 64  # SHA-256 hex

    def test_entry_id_unique(self, trail):
        e1 = trail.log("a", "x", "r")
        e2 = trail.log("b", "y", "r")
        assert e1.entry_id != e2.entry_id

    def test_entry_count_increments(self, trail):
        assert trail.entry_count == 0
        trail.log("a", "x", "r")
        assert trail.entry_count == 1
        trail.log("b", "y", "r")
        assert trail.entry_count == 2


class TestVerifyIntegrity:
    def test_passes_for_untampered_trail(self, trail):
        trail.log("alice", "protocol_started", "run_001")
        trail.log("alice", "step_completed", "run_001")
        trail.log("alice", "protocol_ended", "run_001")
        ok, violations = trail.verify_integrity()
        assert ok is True
        assert violations == []

    def test_fails_after_hash_tampered(self, trail):
        trail.log("alice", "protocol_started", "run_001")
        trail.log("alice", "step_completed", "run_001")
        # Tamper with the first entry's hash
        trail._entries[0].hash = "deadbeef" * 8
        ok, violations = trail.verify_integrity()
        assert ok is False
        assert len(violations) > 0

    def test_fails_if_previous_hash_broken(self, trail):
        trail.log("alice", "e1", "r")
        trail.log("alice", "e2", "r")
        trail._entries[1].previous_hash = "tampered"
        ok, violations = trail.verify_integrity()
        assert ok is False

    def test_empty_trail_passes(self, trail):
        ok, violations = trail.verify_integrity()
        assert ok is True
        assert violations == []


class TestSearch:
    def test_search_by_action(self, trail):
        trail.log("alice", "protocol_started", "run_001")
        trail.log("alice", "estop", "safety_monitor")
        trail.log("bob", "protocol_started", "run_002")
        results = trail.search(action="protocol_started")
        assert len(results) == 2
        assert all(e.action == "protocol_started" for e in results)

    def test_search_by_operator(self, trail):
        trail.log("alice", "x", "r")
        trail.log("bob", "y", "r")
        results = trail.search(operator="alice")
        assert len(results) == 1
        assert results[0].operator == "alice"

    def test_search_by_resource(self, trail):
        trail.log("alice", "x", "run_001")
        trail.log("bob", "y", "run_002")
        results = trail.search(resource="run_001")
        assert len(results) == 1

    def test_search_by_time_range(self, trail):
        t0 = time.time()
        trail.log("alice", "x", "r")
        t1 = time.time()
        trail.log("bob", "y", "r")
        results = trail.search(start_ts=t0, end_ts=t1)
        # At least the first entry should be in range
        assert len(results) >= 1

    def test_search_returns_all_if_no_filter(self, trail):
        trail.log("a", "x", "r")
        trail.log("b", "y", "r")
        assert len(trail.search()) == 2


class TestGet:
    def test_get_by_entry_id(self, trail):
        e = trail.log("alice", "x", "r")
        found = trail.get(e.entry_id)
        assert found is e

    def test_get_nonexistent_returns_none(self, trail):
        assert trail.get("does-not-exist") is None


class TestSign:
    def test_sign_creates_signature_entry(self, trail):
        e = trail.log("alice", "protocol_started", "run_001")
        sig = trail.sign(e.entry_id, "supervisor", "I approve this run")
        assert sig.action == "signature"
        assert sig.detail["signed_entry_id"] == e.entry_id
        assert "I approve this run" in sig.detail["signature_text"]

    def test_sign_nonexistent_raises(self, trail):
        with pytest.raises(ValueError):
            trail.sign("bad-id", "alice", "sig")


class TestSaveLoad:
    def test_jsonl_round_trip(self, trail, tmpdir):
        trail.log("alice", "protocol_started", "run_001", {"x": 1})
        trail.log("bob", "estop", "safety_monitor")
        path = tmpdir / "audit.jsonl"
        trail.save(path)

        trail2 = AuditTrail()
        trail2.load(path)
        assert trail2.entry_count == 2
        assert trail2.entries[0].action == "protocol_started"
        assert trail2.entries[1].action == "estop"

    def test_integrity_after_load(self, trail, tmpdir):
        trail.log("alice", "x", "r")
        trail.log("bob", "y", "r")
        path = tmpdir / "audit.jsonl"
        trail.save(path)
        trail2 = AuditTrail()
        trail2.load(path)
        ok, _ = trail2.verify_integrity()
        assert ok is True


class TestExportReport:
    def test_export_html_creates_file(self, trail, tmpdir):
        trail.log("alice", "protocol_started", "run_001")
        path = tmpdir / "report.html"
        trail.export_report(path, fmt="html")
        assert path.exists()
        content = path.read_text()
        assert "<html" in content
        assert "protocol_started" in content

    def test_export_csv_creates_file(self, trail, tmpdir):
        trail.log("alice", "x", "r")
        path = tmpdir / "report.csv"
        trail.export_report(path, fmt="csv")
        assert path.exists()
        content = path.read_text()
        assert "action" in content  # header row
