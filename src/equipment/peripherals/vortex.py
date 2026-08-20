"""
Vortex mixer driver – IKA Vortex 3 / MS 3 via RS-232 (NAMUR protocol).
Also works with Stuart SA8 / Fisherbrand vortexers in simulation.

IKA NAMUR command subset used:
  IN_PV_4     → read actual speed (rpm)
  OUT_SP_4 N  → set speed to N rpm
  START_4     → start motor
  STOP_4      → stop motor
"""
from __future__ import annotations

import time
from typing import Optional

import serial
from loguru import logger

from ..base_equipment import BaseEquipment, EquipmentConfig, EquipmentStatus


class Vortex(BaseEquipment):
    """IKA Vortex mixer serial driver."""

    MAX_RPM = 3000
    NAMUR_TERMINATOR = b"\r\n"

    def __init__(self, config: EquipmentConfig) -> None:
        super().__init__(config)
        self._serial: Optional[serial.Serial] = None
        self._current_rpm = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        if self.config.simulate:
            self._log.info("Vortex: simulation mode")
            self.status = EquipmentStatus.IDLE
            return True
        try:
            self._serial = serial.Serial(
                port=self.config.port,
                baudrate=self.config.baud_rate or 9600,
                bytesize=serial.SEVENBITS,
                parity=serial.PARITY_EVEN,
                stopbits=serial.STOPBITS_ONE,
                timeout=2,
            )
            resp = self._query("IN_NAME")
            self._log.info(f"Vortex connected: {resp.strip()}")
            self.status = EquipmentStatus.IDLE
            return True
        except Exception as exc:
            self._log.error(f"Vortex connect failed: {exc}")
            self.status = EquipmentStatus.ERROR
            return False

    def disconnect(self) -> None:
        self.stop()
        if self._serial and self._serial.is_open:
            self._serial.close()
        self.status = EquipmentStatus.DISCONNECTED

    def is_ready(self) -> bool:
        if self.config.simulate:
            return self.status in (EquipmentStatus.IDLE, EquipmentStatus.RUNNING)
        return self._serial is not None and self._serial.is_open

    # ── Operations ────────────────────────────────────────────────────────────

    def set_speed(self, rpm: int) -> None:
        rpm = max(0, min(rpm, self.MAX_RPM))
        if self.config.simulate:
            self._log.info(f"[SIMULATE] Vortex speed set to {rpm} rpm")
            self._current_rpm = rpm
            return
        self._send(f"OUT_SP_4 {rpm}")
        self._current_rpm = rpm

    def start(self) -> None:
        if self.config.simulate:
            self._log.info("[SIMULATE] Vortex started")
            self.status = EquipmentStatus.RUNNING
            return
        self._send("START_4")
        self.status = EquipmentStatus.RUNNING

    def stop(self) -> None:
        if self.config.simulate:
            self._log.info("[SIMULATE] Vortex stopped")
            if self.status != EquipmentStatus.ESTOP:
                self.status = EquipmentStatus.IDLE
            return
        if self._serial and self._serial.is_open:
            self._send("STOP_4")
        if self.status != EquipmentStatus.ESTOP:
            self.status = EquipmentStatus.IDLE

    def run_for(self, rpm: int, duration_s: float) -> None:
        """Convenience: set speed, run for duration, then stop."""
        self._log.info(f"Vortexing at {rpm} rpm for {duration_s:.1f}s")
        self.set_speed(rpm)
        self.start()
        if self.config.simulate:
            time.sleep(min(duration_s, 1.0))  # cap simulation wait
        else:
            time.sleep(duration_s)
        self.stop()

    def get_actual_rpm(self) -> int:
        if self.config.simulate:
            return self._current_rpm
        resp = self._query("IN_PV_4")
        try:
            return int(float(resp.strip()))
        except ValueError:
            return 0

    # ── Serial helpers ────────────────────────────────────────────────────────

    def _send(self, cmd: str) -> None:
        if self._serial:
            self._serial.write((cmd + "\r\n").encode())

    def _query(self, cmd: str) -> str:
        self._send(cmd)
        return (self._serial.readline() if self._serial else b"").decode(errors="replace")

    def _on_emergency_stop(self) -> None:
        self.stop()
