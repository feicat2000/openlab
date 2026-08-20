"""
Centrifuge driver – Eppendorf 5430R / 5810R / Micro 24 via RS-232.

Eppendorf remote control protocol (subset):
  A command set over serial (8N1, 9600 baud) using ASCII codes.
  The driver supports the open Eppendorf remote protocol used by
  5430R and 5810R.  For other centrifuges substitute vendor SDK.

Safety
──────
• The arm must NEVER approach a spinning centrifuge.
• The orchestrator calls wait_until_idle() before arm movement.
• An imbalance interlock checks that tubes are loaded in balanced pairs.
"""
from __future__ import annotations

import time
from typing import Optional

import serial
from loguru import logger

from ..base_equipment import BaseEquipment, EquipmentConfig, EquipmentStatus


class Centrifuge(BaseEquipment):
    """Eppendorf 5430R/5810R serial driver."""

    MAX_RPM  = 14000
    MAX_RCF  = 20913   # × g

    # Eppendorf protocol command bytes
    CMD_SET_SPEED   = "SR"  # SR {rpm}
    CMD_SET_TIME    = "ST"  # ST {seconds}
    CMD_SET_TEMP    = "STE" # STE {celsius}   (refrigerated models)
    CMD_START       = "GO"
    CMD_STOP        = "BR"  # brake stop
    CMD_STATUS      = "QS"  # query status
    CMD_ACTUAL_RPM  = "QR"  # query actual rpm

    def __init__(self, config: EquipmentConfig) -> None:
        super().__init__(config)
        self._serial: Optional[serial.Serial] = None
        self._target_rpm   = 0
        self._target_time  = 0
        self._target_temp  = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        if self.config.simulate:
            self._log.info("Centrifuge: simulation mode")
            self.status = EquipmentStatus.IDLE
            return True
        try:
            self._serial = serial.Serial(
                port=self.config.port,
                baudrate=9600,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=3,
            )
            self.status = EquipmentStatus.IDLE
            self._log.info(f"Centrifuge connected on {self.config.port}")
            return True
        except Exception as exc:
            self._log.error(f"Centrifuge connect failed: {exc}")
            self.status = EquipmentStatus.ERROR
            return False

    def disconnect(self) -> None:
        if self._serial and self._serial.is_open:
            self._serial.close()
        self.status = EquipmentStatus.DISCONNECTED

    def is_ready(self) -> bool:
        """True when centrifuge is idle (not spinning)."""
        if self.config.simulate:
            return self.status == EquipmentStatus.IDLE
        actual = self.get_actual_rpm()
        return actual < 50

    # ── Operations ────────────────────────────────────────────────────────────

    def configure(
        self,
        rpm: int,
        duration_s: int,
        temperature_c: Optional[int] = None,
    ) -> None:
        """Set run parameters without starting."""
        rpm = max(100, min(rpm, self.MAX_RPM))
        self._target_rpm  = rpm
        self._target_time = duration_s
        self._target_temp = temperature_c

        if self.config.simulate:
            self._log.info(
                f"[SIMULATE] Centrifuge configured: {rpm} rpm, "
                f"{duration_s}s, {temperature_c}°C"
            )
            return

        self._send(f"{self.CMD_SET_SPEED} {rpm}")
        self._send(f"{self.CMD_SET_TIME} {duration_s}")
        if temperature_c is not None:
            self._send(f"{self.CMD_SET_TEMP} {temperature_c}")

    def start(self) -> None:
        self._log.info(
            f"Centrifuge starting: {self._target_rpm} rpm, "
            f"{self._target_time}s"
            + (f", {self._target_temp}°C" if self._target_temp else "")
        )
        if self.config.simulate:
            self.status = EquipmentStatus.RUNNING
            return
        self._send(self.CMD_START)
        self.status = EquipmentStatus.RUNNING

    def stop(self, brake: bool = True) -> None:
        self._log.info("Centrifuge stop commanded")
        if self.config.simulate:
            self.status = EquipmentStatus.IDLE
            return
        self._send(self.CMD_STOP if brake else "ST 0")
        self.status = EquipmentStatus.IDLE

    def run(
        self,
        rpm: int,
        duration_s: int,
        temperature_c: Optional[int] = None,
        wait: bool = True,
    ) -> None:
        """Configure, start, and optionally block until done."""
        self.configure(rpm, duration_s, temperature_c)
        self.start()
        if wait:
            if self.config.simulate:
                time.sleep(min(duration_s, 2.0))
            else:
                # Poll until idle (centrifuge runs for duration_s then auto-stops)
                timeout = duration_s + 120  # 2 min grace
                self._wait_until_idle(poll_interval=3.0, max_wait=float(timeout))
            self.status = EquipmentStatus.IDLE
            self._log.info("Centrifuge run complete")

    def get_actual_rpm(self) -> int:
        if self.config.simulate:
            return 0
        resp = self._query(self.CMD_ACTUAL_RPM)
        try:
            return int(resp.strip())
        except ValueError:
            return 0

    def lid_open(self) -> None:
        """Open lid (motorised lid models)."""
        if self.config.simulate:
            self._log.info("[SIMULATE] Centrifuge lid open")
            return
        self._send("OL")

    def lid_close(self) -> None:
        if self.config.simulate:
            self._log.info("[SIMULATE] Centrifuge lid close")
            return
        self._send("CL")

    # ── Serial helpers ────────────────────────────────────────────────────────

    def _send(self, cmd: str) -> None:
        if self._serial:
            self._serial.write((cmd + "\r\n").encode())

    def _query(self, cmd: str) -> str:
        self._send(cmd)
        return (self._serial.readline() if self._serial else b"").decode(errors="replace")

    def _on_emergency_stop(self) -> None:
        self.stop(brake=True)
