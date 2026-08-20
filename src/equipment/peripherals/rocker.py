"""
Tube rocker / nutator driver.

Hardware: Stuart SB3 / SRT9D / any relay-controlled rocker.
Control: On/Off via USB relay board or GPIO pin.
Speed control (if supported): RS-232 or PWM.

For simple on/off rockers, relay control is sufficient.
For variable-speed models (e.g. Stuart SRT9D), RS-232 is used.
"""
from __future__ import annotations

import time
from typing import Optional

from loguru import logger

from ..base_equipment import BaseEquipment, EquipmentConfig, EquipmentStatus


class Rocker(BaseEquipment):
    """
    Slow tube rocker / nutator driver.

    config.extra keys:
      relay_backend : "serial" | "gpio" | "simulation"
      relay_port    : /dev/ttyUSB4
      relay_channel : 2
      gpio_pin      : 24
      has_speed_control : false   (set true for variable-speed RS-232 models)
      speed_port        : /dev/ttyUSB4  (if speed control uses separate port)
    """

    def __init__(self, config: EquipmentConfig) -> None:
        super().__init__(config)
        self._backend       = config.extra.get("relay_backend", "simulation")
        self._channel       = config.extra.get("relay_channel", 2)
        self._gpio_pin      = config.extra.get("gpio_pin", 24)
        self._has_speed_ctl = config.extra.get("has_speed_control", False)
        self._relay_serial  = None
        self._running       = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        if self.config.simulate or self._backend == "simulation":
            self._log.info("Rocker: simulation mode")
            self.status = EquipmentStatus.IDLE
            return True

        if self._backend == "gpio":
            try:
                import RPi.GPIO as GPIO  # type: ignore
                GPIO.setmode(GPIO.BCM)
                GPIO.setup(self._gpio_pin, GPIO.OUT, initial=GPIO.LOW)
                self._gpio = GPIO
            except ImportError:
                self._log.error("RPi.GPIO not available")
                return False

        elif self._backend == "serial":
            import serial
            port = self.config.extra.get("relay_port", "/dev/ttyUSB4")
            try:
                self._relay_serial = serial.Serial(port, baudrate=9600, timeout=1)
            except Exception as exc:
                self._log.error(f"Rocker relay serial failed: {exc}")
                return False

        self.status = EquipmentStatus.IDLE
        return True

    def disconnect(self) -> None:
        self.stop()
        if self._relay_serial:
            self._relay_serial.close()
        self.status = EquipmentStatus.DISCONNECTED

    def is_ready(self) -> bool:
        return self.status in (EquipmentStatus.IDLE, EquipmentStatus.RUNNING)

    # ── Operations ────────────────────────────────────────────────────────────

    def start(self, rpm: Optional[int] = None) -> None:
        """Start rocking. rpm only used if has_speed_control=True."""
        self._log.info(f"Rocker started" + (f" @ {rpm} rpm" if rpm else ""))
        self._set_relay(True)
        self._running = True
        self.status   = EquipmentStatus.RUNNING
        if rpm and self._has_speed_ctl:
            self._set_speed(rpm)

    def stop(self) -> None:
        self._log.info("Rocker stopped")
        self._set_relay(False)
        self._running = False
        self.status   = EquipmentStatus.IDLE

    def rock_for(self, duration_s: float, rpm: Optional[int] = None) -> None:
        """Start, wait duration, stop."""
        self._log.info(f"Rocking for {duration_s:.1f}s")
        self.start(rpm=rpm)
        if self.config.simulate:
            time.sleep(min(duration_s, 1.0))
        else:
            time.sleep(duration_s)
        self.stop()

    # ── Relay / speed helpers ─────────────────────────────────────────────────

    def _set_relay(self, on: bool) -> None:
        if self.config.simulate or self._backend == "simulation":
            return
        if self._backend == "gpio":
            import RPi.GPIO as GPIO  # type: ignore
            GPIO.output(self._gpio_pin, GPIO.HIGH if on else GPIO.LOW)
        elif self._backend == "serial" and self._relay_serial:
            ch = self._channel
            st = 0x01 if on else 0x00
            cs = 0xA0 + ch + st
            self._relay_serial.write(bytes([0xA0, ch, st, cs & 0xFF]))

    def _set_speed(self, rpm: int) -> None:
        """Send speed command to variable-speed rocker via RS-232."""
        if self._relay_serial:
            self._relay_serial.write(f"SPEED {rpm}\r\n".encode())

    def _on_emergency_stop(self) -> None:
        self.stop()
