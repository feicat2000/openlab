"""
Dashboard – server-side HTML routes added to the existing FastAPI application.

Routes:
  GET /dashboard              – overview (health, active runs, equipment status)
  GET /dashboard/runs         – run history table (last 50)
  GET /dashboard/equipment    – equipment status cards
  GET /dashboard/audit        – last 20 audit entries
"""
from __future__ import annotations

import html as _html
import json
import time
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from loguru import logger


# ── CSS shared across pages ───────────────────────────────────────────────────

_CSS = """
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
       background: #0d1117; color: #c9d1d9; min-height: 100vh; }
header { background: #161b22; padding: 1em 2em; border-bottom: 1px solid #30363d;
         display: flex; align-items: center; justify-content: space-between; }
header h1 { color: #58a6ff; font-size: 1.4em; }
nav a { color: #8b949e; text-decoration: none; margin-left: 1.5em; font-size: 0.9em; }
nav a:hover { color: #58a6ff; }
main { padding: 2em; max-width: 1400px; margin: 0 auto; }
.card { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
        padding: 1.2em; margin-bottom: 1em; }
.card h2 { color: #58a6ff; margin-bottom: 0.6em; font-size: 1em; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
        gap: 1em; margin-bottom: 1.5em; }
.stat { background: #21262d; border-radius: 6px; padding: 1em; text-align: center; }
.stat .val { font-size: 2em; font-weight: bold; color: #58a6ff; }
.stat .lbl { font-size: 0.8em; color: #8b949e; margin-top: 0.3em; }
table { width: 100%; border-collapse: collapse; font-size: 0.9em; }
th { background: #21262d; color: #8b949e; padding: 0.6em 0.8em; text-align: left;
     font-weight: 600; border-bottom: 1px solid #30363d; }
td { padding: 0.5em 0.8em; border-bottom: 1px solid #21262d; }
tr:hover td { background: #21262d; }
.badge { display: inline-block; padding: 0.2em 0.6em; border-radius: 12px;
         font-size: 0.78em; font-weight: 600; }
.badge-green  { background: #1a3a2a; color: #3fb950; }
.badge-red    { background: #3a1a1a; color: #f85149; }
.badge-yellow { background: #3a3a1a; color: #e3b341; }
.badge-blue   { background: #1a2a3a; color: #58a6ff; }
footer { text-align: center; color: #484f58; font-size: 0.8em; padding: 1em; }
</style>
"""

_NAV = """
<nav>
  <a href="/dashboard">Overview</a>
  <a href="/dashboard/runs">Runs</a>
  <a href="/dashboard/equipment">Equipment</a>
  <a href="/dashboard/audit">Audit</a>
  <a href="/docs">API Docs</a>
</nav>
"""


def _page(title: str, body: str, refresh: int = 10) -> HTMLResponse:
    """Wrap content in a full HTML page with auto-refresh."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="{refresh}">
  <title>{title} – OpenLab Dashboard</title>
  {_CSS}
</head>
<body>
<header>
  <h1>🔬 OpenLab Dashboard</h1>
  {_NAV}
</header>
<main>
{body}
</main>
<footer>Last updated: {ts} · Auto-refresh every {refresh}s</footer>
</body>
</html>"""
    return HTMLResponse(content=html)


def _status_badge(status: str) -> str:
    m = {
        "completed": "badge-green",
        "running": "badge-blue",
        "failed": "badge-red",
        "aborted": "badge-yellow",
        "queued": "badge-yellow",
    }
    cls = m.get(status.lower(), "badge-blue")
    return f'<span class="badge {cls}">{status}</span>'


# ── DashboardRouter ───────────────────────────────────────────────────────────

class DashboardRouter:
    """
    Registers /dashboard/* routes on an existing FastAPI application.

    Usage::

        from src.ui.dashboard import DashboardRouter
        router = DashboardRouter(app, config, simulate=True)
        router.mount()
    """

    def __init__(self, app: FastAPI, config: dict, simulate: bool) -> None:
        self._app = app
        self._config = config
        self._simulate = simulate
        self._runs_dir = Path(config.get("runs_dir", "runs"))

    def mount(self) -> None:
        """Register all /dashboard routes."""
        app = self._app

        @app.get("/dashboard", response_class=HTMLResponse)
        def dashboard_overview() -> HTMLResponse:
            return self._page_overview()

        @app.get("/dashboard/runs", response_class=HTMLResponse)
        def dashboard_runs() -> HTMLResponse:
            return self._page_runs()

        @app.get("/dashboard/equipment", response_class=HTMLResponse)
        def dashboard_equipment() -> HTMLResponse:
            return self._page_equipment()

        @app.get("/dashboard/audit", response_class=HTMLResponse)
        def dashboard_audit() -> HTMLResponse:
            return self._page_audit()

        logger.info("Dashboard routes mounted at /dashboard")

    # ── Page renderers ────────────────────────────────────────────────────────

    def _page_overview(self) -> HTMLResponse:
        runs = self._load_run_summaries(limit=50)
        total = len(runs)
        completed = sum(1 for r in runs if r.get("status") == "completed")
        failed = sum(1 for r in runs if r.get("status") == "failed")
        running = sum(1 for r in runs if r.get("status") == "running")

        recent_rows = ""
        for r in runs[:10]:
            ts = time.strftime("%m/%d %H:%M", time.localtime(r.get("started_at", 0)))
            dur = f"{r.get('duration_s', 0):.0f}s"
            recent_rows += (
                f"<tr><td>{_html.escape(str(r.get('run_id', '–')))[:24]}</td>"
                f"<td>{_html.escape(str(r.get('protocol_name', '–')))}</td>"
                f"<td>{_status_badge(_html.escape(str(r.get('status','unknown'))))}</td>"
                f"<td>{dur}</td><td>{ts}</td></tr>"
            )

        sim_badge = (
            '<span class="badge badge-yellow">SIMULATION</span>'
            if self._simulate else
            '<span class="badge badge-green">REAL HW</span>'
        )

        body = f"""
<div class="grid">
  <div class="stat"><div class="val">{total}</div><div class="lbl">Total Runs</div></div>
  <div class="stat"><div class="val" style="color:#3fb950">{completed}</div><div class="lbl">Completed</div></div>
  <div class="stat"><div class="val" style="color:#f85149">{failed}</div><div class="lbl">Failed</div></div>
  <div class="stat"><div class="val" style="color:#58a6ff">{running}</div><div class="lbl">Running</div></div>
</div>

<div class="card">
  <h2>System Status</h2>
  <p>Mode: {sim_badge} &nbsp;|&nbsp; Runs directory: <code>{self._runs_dir}</code></p>
</div>

<div class="card">
  <h2>Recent Runs (last 10)</h2>
  <table>
    <thead><tr><th>Run ID</th><th>Protocol</th><th>Status</th><th>Duration</th><th>Started</th></tr></thead>
    <tbody>{recent_rows or "<tr><td colspan='5'>No runs yet</td></tr>"}</tbody>
  </table>
</div>
"""
        return _page("Overview", body)

    def _page_runs(self) -> HTMLResponse:
        runs = self._load_run_summaries(limit=50)
        rows = ""
        for r in runs:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r.get("started_at", 0)))
            dur = f"{r.get('duration_s', 0):.1f}s"
            rows += (
                f"<tr><td><code>{_html.escape(str(r.get('run_id','–')))}</code></td>"
                f"<td>{_html.escape(str(r.get('protocol_name','–')))}</td>"
                f"<td>{_status_badge(_html.escape(str(r.get('status','unknown'))))}</td>"
                f"<td>{dur}</td><td>{r.get('step_count', '–')}</td>"
                f"<td>{ts}</td></tr>"
            )

        body = f"""
<div class="card">
  <h2>Run History (last 50)</h2>
  <table>
    <thead><tr><th>Run ID</th><th>Protocol</th><th>Status</th>
    <th>Duration</th><th>Steps</th><th>Started</th></tr></thead>
    <tbody>{rows or "<tr><td colspan='6'>No runs yet</td></tr>"}</tbody>
  </table>
</div>
"""
        return _page("Runs", body)

    def _page_equipment(self) -> HTMLResponse:
        equipment = [
            ("Opentrons Flex / OT-2", self._config.get("opentrons", {})),
            ("UR3e Robotic Arm", self._config.get("robotic_arm", {})),
            ("Vortex Mixer", self._config.get("vortex", {})),
            ("Centrifuge", self._config.get("centrifuge", {})),
            ("Incubator", self._config.get("incubator", {})),
            ("Magnetic Separator", self._config.get("magnetic_separator", {})),
            ("Rocker", self._config.get("rocker", {})),
        ]

        cards = ""
        for name, cfg in equipment:
            sim = cfg.get("simulate", True)
            status = "Simulation" if sim else "Connected"
            badge = _status_badge("running" if not sim else "queued")
            model = _html.escape(str(cfg.get("model", "–")))
            ip = _html.escape(str(cfg.get("ip", cfg.get("port", "–"))))
            cards += f"""
<div class="card">
  <h2>{name}</h2>
  <p>Status: {badge} &nbsp; Model: {model}</p>
  <p style="color:#8b949e;font-size:0.85em;margin-top:0.4em">Address: {ip}</p>
</div>
"""

        body = f"""
<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:1em">
{cards}
</div>
"""
        return _page("Equipment", body)

    def _page_audit(self) -> HTMLResponse:
        audit_path = Path("runs") / "audit.jsonl"
        rows = ""
        count = 0
        if audit_path.exists():
            from ..audit.audit_trail import AuditTrail
            trail = AuditTrail(audit_path)
            entries = trail.entries[-20:]
            count = trail.entry_count
            for e in reversed(entries):
                ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.timestamp))
                detail = _html.escape(json.dumps(e.detail)[:80])
                rows += (
                    f"<tr><td>{ts}</td><td>{_html.escape(e.operator)}</td>"
                    f"<td><code>{_html.escape(e.action)}</code></td><td>{_html.escape(e.resource)}</td>"
                    f"<td style='font-size:0.8em;color:#8b949e'>{detail}</td></tr>"
                )

        body = f"""
<div class="card">
  <h2>Audit Trail (last 20 of {count} entries)</h2>
  <table>
    <thead><tr><th>Timestamp</th><th>Operator</th><th>Action</th>
    <th>Resource</th><th>Detail</th></tr></thead>
    <tbody>{rows or "<tr><td colspan='5'>No audit entries</td></tr>"}</tbody>
  </table>
</div>
"""
        return _page("Audit", body, refresh=30)

    # ── Data helpers ──────────────────────────────────────────────────────────

    def _load_run_summaries(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Read run JSON files from runs/ directory and return summary dicts."""
        summaries: List[Dict[str, Any]] = []
        if not self._runs_dir.exists():
            return summaries

        run_files = sorted(
            self._runs_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:limit]

        for fpath in run_files:
            try:
                with open(fpath) as fh:
                    data = json.load(fh)
                duration = data.get("ended_at", 0) - data.get("started_at", 0)
                summaries.append({
                    "run_id": data.get("run_id", fpath.stem),
                    "protocol_name": data.get("protocol_name", "–"),
                    "status": data.get("status", "unknown"),
                    "started_at": data.get("started_at", 0),
                    "duration_s": max(0, duration),
                    "step_count": len(data.get("steps", [])),
                })
            except Exception as exc:
                logger.debug(f"Could not read run file {fpath}: {exc}")

        return summaries
