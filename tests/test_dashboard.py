"""
Tests for Dashboard routes.
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.ui.api import create_app
from src.ui.dashboard import DashboardRouter


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmpdir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def client(tmpdir):
    cfg = {
        "runs_dir": str(tmpdir / "runs"),
        "opentrons": {"simulate": True, "ip": "localhost"},
        "robotic_arm": {"simulate": True, "ip": "localhost"},
        "vortex": {"simulate": True},
        "centrifuge": {"simulate": True},
        "incubator": {"simulate": True},
        "magnetic_separator": {"simulate": True},
        "rocker": {"simulate": True},
    }
    app = create_app(cfg, simulate=True)
    router = DashboardRouter(app, cfg, simulate=True)
    router.mount()
    return TestClient(app)


@pytest.fixture
def client_with_runs(tmpdir, client):
    """Create a client that has some run JSON files in the runs directory."""
    runs_dir = tmpdir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    for i in range(3):
        run_data = {
            "run_id": f"run_{i:03d}",
            "protocol_name": f"Protocol {i}",
            "status": "completed" if i < 2 else "failed",
            "started_at": time.time() - (3600 * (i + 1)),
            "ended_at": time.time() - (3600 * i + 1800),
            "steps": [{"step_id": 1}, {"step_id": 2}],
        }
        (runs_dir / f"run_{i:03d}.json").write_text(json.dumps(run_data))
    return client


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestDashboardOverview:
    def test_returns_200(self, client):
        resp = client.get("/dashboard")
        assert resp.status_code == 200

    def test_content_type_html(self, client):
        resp = client.get("/dashboard")
        assert "text/html" in resp.headers["content-type"]

    def test_contains_openlab_title(self, client):
        resp = client.get("/dashboard")
        assert "OpenLab" in resp.text

    def test_auto_refresh_meta_tag(self, client):
        resp = client.get("/dashboard")
        assert 'http-equiv="refresh"' in resp.text

    def test_works_with_empty_runs_dir(self, client):
        resp = client.get("/dashboard")
        assert resp.status_code == 200


class TestDashboardRuns:
    def test_returns_200(self, client):
        resp = client.get("/dashboard/runs")
        assert resp.status_code == 200

    def test_content_type_html(self, client):
        resp = client.get("/dashboard/runs")
        assert "text/html" in resp.headers["content-type"]

    def test_contains_table(self, client):
        resp = client.get("/dashboard/runs")
        assert "<table" in resp.text

    def test_shows_run_data(self, client_with_runs):
        resp = client_with_runs.get("/dashboard/runs")
        assert "run_000" in resp.text or "run_001" in resp.text

    def test_empty_runs_no_crash(self, client):
        resp = client.get("/dashboard/runs")
        assert resp.status_code == 200


class TestDashboardEquipment:
    def test_returns_200(self, client):
        resp = client.get("/dashboard/equipment")
        assert resp.status_code == 200

    def test_shows_equipment_names(self, client):
        resp = client.get("/dashboard/equipment")
        # Should contain at least one known equipment name
        assert "Opentrons" in resp.text or "Centrifuge" in resp.text or "Vortex" in resp.text

    def test_content_type_html(self, client):
        resp = client.get("/dashboard/equipment")
        assert "text/html" in resp.headers["content-type"]
