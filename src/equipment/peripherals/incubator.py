"""
Incubator / Thermomixer driver.

Supported hardware (all RS-232):
  • Inheco ODTC (On-Deck Thermal Cycler / Thermoshaker)
  • Eppendorf ThermoMixer C / F
  • Benchmark Scientific Incu-Shaker

The Inheco ODTC protocol is implemented here; the others share a
similar ASCII command set with minor differences handled by subclassing.
"""
from __future__ import annotations

import time
from typing import Optional

import serial
from loguru import logger

from ..base_equipment import BaseEquipment, EquipmentConfig, EquipmentStatus


class Incubator(BaseEquipment):
    """
    Temperature-controlled incubator / thermomixer.

    Supports:
      - Temperature set-point and hold
      - Shaking / mixing at configurable RPM
      - Temperature ramp
    """

    MAX_TEMP_C  = 95.0
    MIN_TEMP_C  = 4.0   # with cooling option
    MAX_SHAKE   = 1400  # rpm

    def __init__(self, config: EquipmentConfig) -> None:
        super().__init__(config)
        self._serial: Optional[serial.Serial] = None
        self._set_temp: Optional[float] = None
        self._set_shake: Optional[int]  = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        if self.config.simulate:
            self._log.info("Incubator: simulation mode")
            self.status = EquipmentStatus.IDLE
            return True
        try:
            self._serial = serial.Serial(
                port=self.config.port,
                baudrate=self.config.baud_rate or 9600,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=3,
            )
            self.status = EquipmentStatus.IDLE
            self._log.info(f"Incubator connected on {self.config.port}")
            return True
        except Exception as exc:
            self._log.error(f"Incubator connect failed: {exc}")
            self.status = EquipmentStatus.ERROR
            return False

    def disconnect(self) -> None:
        self.stop_all()
        if self._serial and self._serial.is_open:
            self._serial.close()
        self.status = EquipmentStatus.DISCONNECTED

    def is_ready(self) -> bool:
        return self.status in (EquipmentStatus.IDLE, EquipmentStatus.RUNNING)

    # ── Temperature control ───────────────────────────────────────────────────

    def set_temperature(self, temp_c: float) -> None:
        temp_c = max(self.MIN_TEMP_C, min(temp_c, self.MAX_TEMP_C))
        self._set_temp = temp_c
        if self.config.simulate:
            self._log.info(f"[SIMULATE] Incubator set temp {temp_c}°C")
            return
        # Inheco ODTC: "SET TEMP {value}" (0.1°C resolution)
        self._send(f"SET TEMP {temp_c:.1f}")

    def get_actual_temperature(self) -> float:
        if self.config.simulate:
            return self._set_temp or 25.0
        resp = self._query("GET TEMP")
        try:
            return float(resp.strip().split()[-1])
        except (ValueError, IndexError):
            return 0.0

    def wait_for_temperature(self, tolerance_c: float = 0.5, timeout_s: float = 600) -> bool:
        """Block until actual temp is within tolerance of set-point."""
        if self._set_temp is None:
            return True
        if self.config.simulate:
            self._log.info(f"[SIMULATE] Temp reached {self._set_temp}°C")
            time.sleep(0.5)
            return True

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            actual = self.get_actual_temperature()
            if abs(actual - self._set_temp) <= tolerance_c:
                self._log.info(f"Temperature {actual:.1f}°C reached")
                return True
            time.sleep(5)
        self._log.error(f"Temperature not reached within {timeout_s}s")
        return False

    # ── Shaking ───────────────────────────────────────────────────────────────

    def set_shaking(self, rpm: int) -> None:
        rpm = max(0, min(rpm, self.MAX_SHAKE))
        self._set_shake = rpm
        if self.config.simulate:
            self._log.info(f"[SIMULATE] Incubator shake {rpm} rpm")
            return
        self._send(f"SET SHAKE {rpm}")

    def start_shaking(self) -> None:
        if self.config.simulate:
            self._log.info("[SIMULATE] Incubator shaking started")
            self.status = EquipmentStatus.RUNNING
            return
        self._send("START SHAKE")
        self.status = EquipmentStatus.RUNNING

    def stop_shaking(self) -> None:
        if self.config.simulate:
            self._log.info("[SIMULATE] Incubator shaking stopped")
            return
        self._send("STOP SHAKE")

    # ── Combined operations ───────────────────────────────────────────────────

    def incubate(
        self,
        temperature_c: float,
        duration_s: float,
        shake_rpm: int = 0,
        wait_for_temp: bool = True,
    ) -> None:
        """
        Full incubation sequence:
          set temp → (optionally shake) → wait duration → stop shake.
        Temperature control remains ON after this call.
        """
        self._log.info(
            f"Incubating: {temperature_c}°C for {duration_s}s"
            + (f" @ {shake_rpm} rpm" if shake_rpm else "")
        )
        self.set_temperature(temperature_c)
        if wait_for_temp:
            self.wait_for_temperature()

        if shake_rpm:
            self.set_shaking(shake_rpm)
            self.start_shaking()

        if self.config.simulate:
            time.sleep(min(duration_s, 2.0))
        else:
            time.sleep(duration_s)

        if shake_rpm:
            self.stop_shaking()
        self._log.info("Incubation complete")

    def stop_all(self) -> None:
        self.stop_shaking()
        if self.config.simulate:
            self.status = EquipmentStatus.IDLE
            return
        self._send("STOP ALL")
        self.status = EquipmentStatus.IDLE

    # ── Serial helpers ────────────────────────────────────────────────────────

    def _send(self, cmd: str) -> None:
        if self._serial:
            self._serial.write((cmd + "\r\n").encode())

    def _query(self, cmd: str) -> str:
        self._send(cmd)
        return (self._serial.readline() if self._serial else b"").decode(errors="replace")

    def _on_emergency_stop(self) -> None:
        self.stop_all()
