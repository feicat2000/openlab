"""
OpenLab REST API – FastAPI application.

Endpoints
──────────
  POST /run                   – submit a protocol YAML, run asynchronously
  GET  /run/{run_id}          – poll run status
  GET  /runs                  – list all runs
  POST /run/{run_id}/abort    – emergency abort
  GET  /equipment/status      – status of all connected equipment
  POST /estop                 – software emergency stop
  POST /estop/reset           – reset e-stop (POST with confirmation body)
  GET  /protocol/validate     – validate a protocol YAML without running
  GET  /health                – liveness check

WebSocket
──────────
  WS /ws/run/{run_id}         – live step progress stream
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from fastapi import (
    BackgroundTasks,
    FastAPI,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from pydantic import BaseModel

from ..core.protocol_parser import ProtocolParser
from ..core.resource_manager import ResourceManager
from ..core.risk_assessment import RiskAssessor


# ── Request / response models (module-level for Pydantic v2 compatibility) ────

class RunOptions(BaseModel):
    skip_steps:          List[int] = []
    confirm_before_run:  bool      = False
    simulate_override:   Optional[bool] = None


class EStopResetBody(BaseModel):
    confirm: str   # must equal "I_CONFIRM_EQUIPMENT_IS_SAFE"


# ── App factory ───────────────────────────────────────────────────────────────

def create_app(config: dict, simulate: bool = True) -> FastAPI:
    app = FastAPI(
        title="OpenLab API",
        description="Opentrons + Robotic Arm automation platform",
        version="1.0.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Shared state
    app.state.config        = config
    app.state.simulate      = simulate
    app.state.active_runs:  Dict[str, dict]  = {}
    app.state.ws_clients:   Dict[str, list]  = {}   # run_id → [WebSocket]

    parser    = ProtocolParser()
    resource  = ResourceManager()
    assessor  = RiskAssessor()

    # ── Health ────────────────────────────────────────────────────────────────

    @app.get("/health")
    async def health():
        return {"status": "ok", "simulate": simulate, "time": time.time()}

    # ── Protocol validation ───────────────────────────────────────────────────

    @app.post("/protocol/validate")
    async def validate_protocol(file: UploadFile):
        content = await file.read()
        try:
            doc = parser.load_string(content.decode())
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        mat    = resource.generate_material_report(doc)
        risk   = assessor.assess(doc)

        return {
            "valid":        True,
            "protocol":     doc.metadata.model_dump(),
            "steps":        len(doc.steps),
            "reagents":     mat.reagents,
            "consumables":  mat.consumables,
            "missing":      mat.missing,
            "risk_level":   risk.overall_risk,
            "clearance":    risk.clearance,
            "risks":        [
                {"category": r.category, "source": r.source,
                 "severity": r.severity, "mitigation": r.mitigation}
                for r in risk.risks
            ],
            "ppe":          [p.item for p in risk.ppe],
            "waste":        [{"label": w.label, "route": w.disposal_route}
                             for w in risk.waste_streams],
        }

    # ── Run submission ────────────────────────────────────────────────────────

    @app.post("/run", status_code=status.HTTP_202_ACCEPTED)
    async def submit_run(
        file: UploadFile,
        background_tasks: BackgroundTasks,
        options: RunOptions = RunOptions(),
    ):
        content = await file.read()
        try:
            doc = parser.load_string(content.decode())
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Protocol parse error: {exc}")

        run_id  = f"run_{uuid.uuid4().hex[:8]}"
        use_sim = options.simulate_override if options.simulate_override is not None else simulate

        app.state.active_runs[run_id] = {
            "run_id":       run_id,
            "protocol":     doc.metadata.name,
            "status":       "queued",
            "started_at":   None,
            "ended_at":     None,
            "current_step": None,
            "steps_done":   0,
            "total_steps":  len(doc.steps),
            "errors":       [],
        }

        background_tasks.add_task(
            _run_protocol_bg,
            app=app,
            run_id=run_id,
            doc=doc,
            config=config,
            simulate=use_sim,
            skip_steps=options.skip_steps,
        )

        return {"run_id": run_id, "status": "queued"}

    # ── Run status ────────────────────────────────────────────────────────────

    @app.get("/run/{run_id}")
    async def get_run(run_id: str):
        run = app.state.active_runs.get(run_id)
        if not run:
            # Try loading from disk
            runs_dir = Path("runs")
            f = runs_dir / f"{run_id}.json"
            if f.exists():
                with open(f) as fh:
                    return json.load(fh)
            raise HTTPException(status_code=404, detail="Run not found")
        return run

    @app.get("/runs")
    async def list_runs():
        runs_dir = Path("runs")
        saved = []
        if runs_dir.exists():
            for f in sorted(runs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                saved.append(f.stem)
        return {
            "active": list(app.state.active_runs.keys()),
            "saved":  saved,
        }

    # ── Abort ─────────────────────────────────────────────────────────────────

    @app.post("/run/{run_id}/abort")
    async def abort_run(run_id: str):
        run = app.state.active_runs.get(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        run["status"]   = "aborting"
        run["abort_requested"] = True
        return {"run_id": run_id, "status": "abort_requested"}

    # ── Equipment status ──────────────────────────────────────────────────────

    @app.get("/equipment/status")
    async def equipment_status():
        """Returns mock status – real impl queries equipment drivers."""
        return {
            "opentrons":         {"status": "idle", "simulate": simulate},
            "robotic_arm":       {"status": "idle", "simulate": simulate},
            "vortex":            {"status": "idle", "simulate": simulate},
            "centrifuge":        {"status": "idle", "simulate": simulate},
            "incubator":         {"status": "idle", "simulate": simulate},
            "magnetic_separator":{"status": "idle", "simulate": simulate},
            "rocker":            {"status": "idle", "simulate": simulate},
        }

    # ── Emergency stop ────────────────────────────────────────────────────────

    @app.post("/estop")
    async def emergency_stop():
        logger.critical("API: Emergency stop triggered")
        # TODO: call safety_monitor.trigger_estop() from shared state
        return {"status": "estop_triggered", "time": time.time()}

    @app.post("/estop/reset")
    async def reset_estop(body: EStopResetBody):
        if body.confirm != "I_CONFIRM_EQUIPMENT_IS_SAFE":
            raise HTTPException(status_code=400, detail="Confirmation string incorrect")
        return {"status": "estop_reset"}

    # ── WebSocket live feed ───────────────────────────────────────────────────

    @app.websocket("/ws/run/{run_id}")
    async def ws_run(websocket: WebSocket, run_id: str):
        await websocket.accept()
        if run_id not in app.state.ws_clients:
            app.state.ws_clients[run_id] = []
        app.state.ws_clients[run_id].append(websocket)
        try:
            while True:
                run = app.state.active_runs.get(run_id, {})
                await websocket.send_json(run)
                if run.get("status") in ("completed", "failed", "aborted"):
                    break
                await asyncio.sleep(1.0)
        except WebSocketDisconnect:
            pass
        finally:
            if run_id in app.state.ws_clients:
                app.state.ws_clients[run_id].remove(websocket)

    # ── Dashboard ─────────────────────────────────────────────────────────────

    from ..ui.dashboard import DashboardRouter
    DashboardRouter(app, config, simulate).mount()

    return app


# ── Background run task ───────────────────────────────────────────────────────

async def _run_protocol_bg(
    app: FastAPI,
    run_id: str,
    doc: Any,
    config: dict,
    simulate: bool,
    skip_steps: List[int],
) -> None:
    from ..core.orchestrator import Orchestrator
    run = app.state.active_runs[run_id]
    run["status"]     = "running"
    run["started_at"] = time.time()

    try:
        orch = Orchestrator(config=config, simulate=simulate)
        # Wrap execute in thread to avoid blocking event loop
        loop = asyncio.get_running_loop()
        record = await loop.run_in_executor(
            None,
            lambda: orch.execute(doc, confirm_before_run=False, skip_steps=skip_steps),
        )
        run["status"]    = record.status
        run["ended_at"]  = record.ended_at
        run["steps_done"] = len([s for s in record.steps if s.status == "completed"])

    except Exception as exc:
        logger.error(f"Background run {run_id} failed: {exc}")
        run["status"] = "failed"
        run["errors"].append(str(exc))
        run["ended_at"] = time.time()
