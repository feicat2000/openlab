"""
Opentrons OT-2 / Flex controller.

Communication strategy
──────────────────────
• On-robot protocol execution  : Opentrons HTTP API (port 31950)
  - Upload protocol JSON/Python → create run → start run → poll status
• Direct pipette control (live) : Same HTTP API, /commands endpoint
• Simulation                    : Log-only, no network calls

Hardware supported
──────────────────
• Opentrons OT-2  (Python API v2, HTTP API v5)
• Opentrons Flex  (same HTTP API, gripper support)
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from loguru import logger

from .base_equipment import BaseEquipment, EquipmentConfig, EquipmentStatus


class OpentronsController(BaseEquipment):
    """
    Controls an Opentrons liquid-handling robot via its HTTP API.

    The preferred workflow for production:
      1. Build a protocol (YAML → Opentrons Python script via ProtocolCompiler)
      2. upload_protocol(path)   → returns protocol_id
      3. create_run(protocol_id) → returns run_id
      4. start_run(run_id)
      5. poll_run(run_id)        → blocks until complete / error

    For interactive / step-level control the direct_command() method
    can send individual pipetting commands without a full protocol upload.
    """

    BASE_URL_TMPL = "http://{host}:{port}"

    def __init__(self, config: EquipmentConfig) -> None:
        super().__init__(config)
        host = config.ip_address or "169.254.10.10"
        port = config.extra.get("port", 31950)
        self._base_url  = self.BASE_URL_TMPL.format(host=host, port=port)
        self._session   = requests.Session()
        self._session.headers.update({"Opentrons-Version": "5"})
        self._run_id: Optional[str] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        if self.config.simulate:
            self._log.info("Opentrons: simulation mode – no connection attempted")
            self.status = EquipmentStatus.IDLE
            return True
        try:
            r = self._session.get(f"{self._base_url}/health", timeout=5)
            r.raise_for_status()
            info = r.json()
            self._log.info(
                f"Connected to Opentrons {info.get('robot_model', '?')} "
                f"running OT-API {info.get('api_version', '?')}"
            )
            self.status = EquipmentStatus.IDLE
            return True
        except Exception as exc:
            self._log.error(f"Cannot connect to Opentrons: {exc}")
            self.status = EquipmentStatus.ERROR
            return False

    def disconnect(self) -> None:
        self._session.close()
        self.status = EquipmentStatus.DISCONNECTED
        self._log.info("Opentrons disconnected")

    def is_ready(self) -> bool:
        if self.config.simulate:
            return self.status == EquipmentStatus.IDLE
        try:
            r = self._session.get(f"{self._base_url}/health", timeout=3)
            return r.status_code == 200
        except Exception:
            return False

    # ── Protocol upload & run ─────────────────────────────────────────────────

    def upload_protocol(self, protocol_path: Path) -> str:
        """Upload a .py or .json protocol file; return protocol_id."""
        if self.config.simulate:
            pid = f"sim-protocol-{protocol_path.stem}"
            self._log.info(f"[SIMULATE] Protocol upload: {protocol_path.name} → {pid}")
            return pid

        with open(protocol_path, "rb") as fh:
            r = self._session.post(
                f"{self._base_url}/protocols",
                files={"files": (protocol_path.name, fh, "text/x-python")},
                timeout=30,
            )
        r.raise_for_status()
        pid = r.json()["data"]["id"]
        self._log.info(f"Protocol uploaded: {pid}")
        return pid

    def create_run(self, protocol_id: str) -> str:
        """Create a run from a protocol; return run_id."""
        if self.config.simulate:
            rid = f"sim-run-{protocol_id}"
            self._log.info(f"[SIMULATE] Run created: {rid}")
            self._run_id = rid
            return rid

        r = self._session.post(
            f"{self._base_url}/runs",
            json={"data": {"protocolId": protocol_id}},
            timeout=10,
        )
        r.raise_for_status()
        rid = r.json()["data"]["id"]
        self._run_id = rid
        self._log.info(f"Run created: {rid}")
        return rid

    def start_run(self, run_id: Optional[str] = None) -> None:
        rid = run_id or self._run_id
        if not rid:
            raise RuntimeError("No run_id – call create_run() first")
        if self.config.simulate:
            self._log.info(f"[SIMULATE] Run started: {rid}")
            self.status = EquipmentStatus.RUNNING
            return
        r = self._session.post(
            f"{self._base_url}/runs/{rid}/actions",
            json={"data": {"actionType": "play"}},
            timeout=10,
        )
        r.raise_for_status()
        self.status = EquipmentStatus.RUNNING
        self._log.info(f"Run started: {rid}")

    def poll_run(
        self,
        run_id: Optional[str] = None,
        poll_interval: float = 2.0,
        timeout: float = 7200.0,
    ) -> str:
        """Block until run reaches a terminal state; return final status string."""
        rid = run_id or self._run_id
        if not rid:
            raise RuntimeError("No run_id")
        if self.config.simulate:
            self._log.info(f"[SIMULATE] Run {rid} completed")
            self.status = EquipmentStatus.IDLE
            return "succeeded"

        deadline = time.time() + timeout
        TERMINAL = {"succeeded", "failed", "stopped"}
        while time.time() < deadline:
            r = self._session.get(f"{self._base_url}/runs/{rid}", timeout=5)
            r.raise_for_status()
            run_status = r.json()["data"]["status"]
            self._log.debug(f"Run {rid} status: {run_status}")
            if run_status in TERMINAL:
                self.status = EquipmentStatus.IDLE if run_status == "succeeded" else EquipmentStatus.ERROR
                self._log.info(f"Run {rid} finished with status: {run_status}")
                return run_status
            time.sleep(poll_interval)

        raise TimeoutError(f"Run {rid} did not complete within {timeout}s")

    # ── Direct (live) commands ────────────────────────────────────────────────

    def direct_command(self, run_id: str, command: Dict[str, Any], wait: bool = True) -> Dict:
        """
        POST a single command to an existing run (useful for interactive control).
        command example:
          {"commandType": "aspirate",
           "params": {"pipetteId": "...", "labwareId": "...",
                      "wellName": "A1", "volume": 100, "flowRate": 50}}
        """
        if self.config.simulate:
            self._log.info(f"[SIMULATE] Direct command: {command.get('commandType')}")
            return {"status": "simulated"}

        params = {"waitUntilComplete": str(wait).lower()}
        r = self._session.post(
            f"{self._base_url}/runs/{run_id}/commands",
            json={"data": command},
            params=params,
            timeout=self.config.timeout,
        )
        r.raise_for_status()
        return r.json()["data"]

    # ── Deck / labware helpers ────────────────────────────────────────────────

    def get_loaded_labware(self, run_id: Optional[str] = None) -> List[Dict]:
        rid = run_id or self._run_id
        if self.config.simulate or not rid:
            return []
        r = self._session.get(f"{self._base_url}/runs/{rid}/labware", timeout=5)
        r.raise_for_status()
        return r.json().get("data", [])

    def home_robot(self) -> None:
        """Send pipettes to home position."""
        if self.config.simulate:
            self._log.info("[SIMULATE] Robot homed")
            return
        r = self._session.post(f"{self._base_url}/robot/home", timeout=30)
        r.raise_for_status()
        self._log.info("Robot homed")

    def lights_on(self, on: bool = True) -> None:
        if self.config.simulate:
            return
        self._session.post(
            f"{self._base_url}/robot/lights",
            json={"on": on},
            timeout=5,
        )

    def _on_emergency_stop(self) -> None:
        if self.config.simulate:
            return
        try:
            if self._run_id:
                self._session.post(
                    f"{self._base_url}/runs/{self._run_id}/actions",
                    json={"data": {"actionType": "stop"}},
                    timeout=5,
                )
        except Exception as exc:
            self._log.error(f"E-stop HTTP call failed: {exc}")
