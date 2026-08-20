"""
Magnetic separator driver.

Hardware options
────────────────
A) DynaMag™ / Alpaqua Magnum FLX  →  motorised, controlled via relay or GPIO
B) Custom linear-actuator magnet   →  relay board or GPIO pin
C) On-deck Opentrons magnetic module → native OT API (handled in opentrons_controller)

This driver controls option A/B via a relay (USB relay board or RPi GPIO).
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Optional

from loguru import logger

from ..base_equipment import BaseEquipment, EquipmentConfig, EquipmentStatus


class MagnetState(str, Enum):
    ENGAGED    = "engaged"
    DISENGAGED = "disengaged"


class MagneticSeparator(BaseEquipment):
    """
    Relay-controlled magnetic separator.

    config.extra keys:
      relay_backend : "serial" | "gpio" | "simulation"
      relay_port    : /dev/ttyUSB5  (for USB relay boards)
      relay_channel : 1            (1-indexed)
      gpio_pin      : 22           (BCM pin, Raspberry Pi)
    """

    def __init__(self, config: EquipmentConfig) -> None:
        super().__init__(config)
        self._backend  = config.extra.get("relay_backend", "simulation")
        self._channel  = config.extra.get("relay_channel", 1)
        self._gpio_pin = config.extra.get("gpio_pin", 22)
        self._state    = MagnetState.DISENGAGED
        self._gpio     = None      # RPi.GPIO module, set during connect()
        self._relay_serial = None  # serial.Serial for USB relay board

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        if self.config.simulate or self._backend == "simulation":
            self._log.info("Magnetic separator: simulation mode")
            self.status = EquipmentStatus.IDLE
            return True

        if self._backend == "gpio":
            try:
                import RPi.GPIO as GPIO  # type: ignore
                GPIO.setmode(GPIO.BCM)
                GPIO.setup(self._gpio_pin, GPIO.OUT, initial=GPIO.LOW)
                self._gpio = GPIO
                self._log.info(f"Magnetic separator GPIO pin {self._gpio_pin} configured")
            except ImportError:
                self._log.error("RPi.GPIO not available")
                return False

        elif self._backend == "serial":
            import serial
            port = self.config.extra.get("relay_port", "/dev/ttyUSB5")
            try:
                self._relay_serial = serial.Serial(port, baudrate=9600, timeout=1)
                self._log.info(f"Magnetic separator relay on {port}")
            except Exception as exc:
                self._log.error(f"Relay serial open failed: {exc}")
                return False

        self.status = EquipmentStatus.IDLE
        return True

    def disconnect(self) -> None:
        self.disengage()
        if self._relay_serial:
            self._relay_serial.close()
        self.status = EquipmentStatus.DISCONNECTED

    def is_ready(self) -> bool:
        return self.status in (EquipmentStatus.IDLE, EquipmentStatus.RUNNING)

    # ── Operations ────────────────────────────────────────────────────────────

    def engage(self) -> None:
        """Move magnet into contact with tube rack."""
        self._log.info("Magnetic separator: ENGAGE")
        self._set_relay(True)
        self._state = MagnetState.ENGAGED
        self.status = EquipmentStatus.RUNNING

    def disengage(self) -> None:
        """Retract magnet."""
        self._log.info("Magnetic separator: DISENGAGE")
        self._set_relay(False)
        self._state = MagnetState.DISENGAGED
        self.status = EquipmentStatus.IDLE

    def separate(self, duration_s: float = 120.0, mix_before: bool = True) -> None:
        """
        Full magnetic separation cycle.
          1. (optional) Mix – arm should vortex before calling this
          2. Engage magnet
          3. Wait for beads to collect
          4. Leave magnet engaged (caller removes supernatant, then calls disengage())
        """
        self._log.info(f"Magnetic separation: {duration_s}s engagement")
        self.engage()
        if self.config.simulate:
            time.sleep(min(duration_s, 1.0))
        else:
            time.sleep(duration_s)
        self._log.info("Magnetic separation complete – magnet still engaged")

    @property
    def state(self) -> MagnetState:
        return self._state

    # ── Relay helpers ─────────────────────────────────────────────────────────

    def _set_relay(self, on: bool) -> None:
        if self.config.simulate or self._backend == "simulation":
            self._log.debug(f"[SIMULATE] Relay {'ON' if on else 'OFF'}")
            return

        if self._backend == "gpio":
            GPIO = self._gpio  # set during connect()
            GPIO.output(self._gpio_pin, GPIO.HIGH if on else GPIO.LOW)

        elif self._backend == "serial" and self._relay_serial:
            # USB-HID relay board (e.g. CH340 based) command bytes
            # Protocol: [0xA0, channel, state, checksum]
            ch  = self._channel
            st  = 0x01 if on else 0x00
            cs  = 0xA0 + ch + st
            self._relay_serial.write(bytes([0xA0, ch, st, cs & 0xFF]))

    def _on_emergency_stop(self) -> None:
        self.disengage()
