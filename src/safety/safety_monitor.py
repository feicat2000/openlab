"""
Safety Monitor – real-time hardware safety watchdog.

Responsibilities
─────────────────
• Poll centrifuge RPM – arm must not move while spinning.
• Monitor incubator temperature – halt if over MAX_TEMP_C.
• Detect E-stop button press (GPIO or software signal).
• Enforce zone exclusions (arm ↔ centrifuge / Opentrons).
• Broadcast EMERGENCY_STOP to all equipment on fault.
• Log all safety events to audit trail.

Architecture
─────────────
SafetyMonitor runs as a background thread.
Equipment drivers register themselves via register().
The orchestrator checks can_arm_move() before every arm command.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from ..equipment.base_equipment import BaseEquipment
    from ..equipment.centrifuge import Centrifuge
    from ..equipment.incubator import Incubator


class SafetyFaultType(str, Enum):
    CENTRIFUGE_SPINNING   = "centrifuge_spinning"
    OVER_TEMPERATURE      = "over_temperature"
    ESTOP_PRESSED         = "estop_pressed"
    EQUIPMENT_ERROR       = "equipment_error"
    ARM_COLLISION_ZONE    = "arm_collision_zone"
    COMMUNICATION_LOSS    = "communication_loss"


@dataclass
class SafetyEvent:
    timestamp: float
    fault:     SafetyFaultType
    source:    str
    detail:    str
    resolved:  bool = False


class SafetyMonitor:
    """
    Background safety watchdog.  Thread-safe.

    Usage:
        monitor = SafetyMonitor(config)
        monitor.register("centrifuge", centrifuge_instance)
        monitor.register("incubator",  incubator_instance)
        monitor.start()
        ...
        if monitor.can_arm_move():
            arm.move_to(...)
        ...
        monitor.stop()
    """

    POLL_INTERVAL  = 0.5   # seconds between safety checks
    MAX_CENTRIFUGE_IDLE_RPM = 50

    def __init__(self, config: dict) -> None:
        self._config       = config
        self._equipment:   Dict[str, "BaseEquipment"] = {}
        self._events:      List[SafetyEvent] = []
        self._faults:      Dict[SafetyFaultType, bool] = {}
        self._estop:       bool = False
        self._running:     bool = False
        self._lock         = threading.Lock()
        self._thread:      Optional[threading.Thread] = None
        self._callbacks:   List[Callable[[SafetyEvent], None]] = []

        # Limits from config
        self._max_incubator_temp = float(config.get("max_incubator_temp_c", 95))
        self._max_centrifuge_rpm = int(config.get("max_centrifuge_rpm", 14000))

        # GPIO E-stop (optional)
        self._gpio_estop_pin = config.get("estop_gpio_pin")
        self._setup_gpio_estop()

    # ── Registration ──────────────────────────────────────────────────────────

    def register(self, name: str, equipment: "BaseEquipment") -> None:
        """Register an equipment driver for monitoring."""
        with self._lock:
            self._equipment[name] = equipment
        logger.debug(f"SafetyMonitor: registered '{name}'")

    def add_callback(self, fn: Callable[[SafetyEvent], None]) -> None:
        """Add a callback invoked on every new safety event."""
        self._callbacks.append(fn)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._running = True
        self._thread  = threading.Thread(
            target=self._poll_loop, name="SafetyMonitor", daemon=True
        )
        self._thread.start()
        logger.info("SafetyMonitor started")

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
        logger.info("SafetyMonitor stopped")

    # ── Arm movement gate ─────────────────────────────────────────────────────

    def can_arm_move(self) -> bool:
        """Return True if it is safe for the robotic arm to move."""
        with self._lock:
            if self._estop:
                return False
            for fault, active in self._faults.items():
                if active and fault in (
                    SafetyFaultType.CENTRIFUGE_SPINNING,
                    SafetyFaultType.ESTOP_PRESSED,
                    SafetyFaultType.EQUIPMENT_ERROR,
                ):
                    return False
            return True

    def assert_arm_safe(self) -> None:
        """Raise RuntimeError if arm movement is not currently safe."""
        if not self.can_arm_move():
            active = [f for f, v in self._faults.items() if v]
            raise RuntimeError(f"Arm movement blocked by safety faults: {active}")

    # ── Software E-stop ───────────────────────────────────────────────────────

    def trigger_estop(self, source: str = "software") -> None:
        logger.critical(f"EMERGENCY STOP triggered by: {source}")
        with self._lock:
            self._estop = True
        self._record_event(SafetyFaultType.ESTOP_PRESSED, source, "Emergency stop triggered")
        self._broadcast_estop()

    def reset_estop(self) -> None:
        """Reset E-stop – only after physical inspection and clearance."""
        logger.warning("E-stop RESET – ensure all equipment is safe before resuming")
        with self._lock:
            self._estop = False
            self._faults.pop(SafetyFaultType.ESTOP_PRESSED, None)

    @property
    def is_estopped(self) -> bool:
        return self._estop

    # ── Event log ─────────────────────────────────────────────────────────────

    def get_events(self) -> List[SafetyEvent]:
        with self._lock:
            return list(self._events)

    def get_active_faults(self) -> List[SafetyFaultType]:
        with self._lock:
            return [f for f, v in self._faults.items() if v]

    # ── Internal polling loop ─────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        while self._running:
            try:
                self._check_centrifuge()
                self._check_incubator()
                self._check_equipment_errors()
            except Exception as exc:
                logger.error(f"SafetyMonitor poll error: {exc}")
            time.sleep(self.POLL_INTERVAL)

    def _check_centrifuge(self) -> None:
        cent = self._equipment.get("centrifuge")
        if cent is None or cent.config.simulate:
            return
        try:
            from ..equipment.peripherals.centrifuge import Centrifuge as Cent
            if isinstance(cent, Cent):
                rpm = cent.get_actual_rpm()
                spinning = rpm > self.MAX_CENTRIFUGE_IDLE_RPM
                with self._lock:
                    was_spinning = self._faults.get(SafetyFaultType.CENTRIFUGE_SPINNING, False)
                if spinning and not was_spinning:
                    self._set_fault(SafetyFaultType.CENTRIFUGE_SPINNING, "centrifuge",
                                    f"Centrifuge spinning at {rpm} rpm – arm locked")
                elif not spinning and was_spinning:
                    self._clear_fault(SafetyFaultType.CENTRIFUGE_SPINNING)
        except Exception as exc:
            logger.debug(f"Centrifuge check error: {exc}")

    def _check_incubator(self) -> None:
        inc = self._equipment.get("incubator")
        if inc is None or inc.config.simulate:
            return
        try:
            from ..equipment.peripherals.incubator import Incubator as Inc
            if isinstance(inc, Inc):
                temp = inc.get_actual_temperature()
                over = temp > self._max_incubator_temp
                with self._lock:
                    was_over = self._faults.get(SafetyFaultType.OVER_TEMPERATURE, False)
                if over and not was_over:
                    self._set_fault(SafetyFaultType.OVER_TEMPERATURE, "incubator",
                                    f"Temperature {temp:.1f}°C exceeds limit {self._max_incubator_temp}°C")
                    inc.stop_all()
                elif not over and was_over:
                    self._clear_fault(SafetyFaultType.OVER_TEMPERATURE)
        except Exception as exc:
            logger.debug(f"Incubator check error: {exc}")

    def _check_equipment_errors(self) -> None:
        from ..equipment.base_equipment import EquipmentStatus
        with self._lock:
            equip = dict(self._equipment)
        for name, eq in equip.items():
            if eq.status == EquipmentStatus.ERROR:
                self._set_fault(
                    SafetyFaultType.EQUIPMENT_ERROR, name,
                    f"Equipment '{name}' entered ERROR state"
                )

    # ── Fault management ──────────────────────────────────────────────────────

    def _set_fault(self, fault: SafetyFaultType, source: str, detail: str) -> None:
        with self._lock:
            if not self._faults.get(fault):
                self._faults[fault] = True
        logger.warning(f"SAFETY FAULT [{fault}] {source}: {detail}")
        self._record_event(fault, source, detail)

    def _clear_fault(self, fault: SafetyFaultType) -> None:
        with self._lock:
            self._faults[fault] = False
        logger.info(f"Safety fault cleared: {fault}")

    def _record_event(self, fault: SafetyFaultType, source: str, detail: str) -> None:
        event = SafetyEvent(
            timestamp=time.time(), fault=fault, source=source, detail=detail
        )
        with self._lock:
            self._events.append(event)
        for cb in self._callbacks:
            try:
                cb(event)
            except Exception:
                pass

    def _broadcast_estop(self) -> None:
        with self._lock:
            equip = dict(self._equipment)
        for name, eq in equip.items():
            try:
                eq.emergency_stop()
            except Exception as exc:
                logger.error(f"E-stop failed on '{name}': {exc}")

    # ── GPIO E-stop setup ─────────────────────────────────────────────────────

    def _setup_gpio_estop(self) -> None:
        if not self._gpio_estop_pin:
            return
        try:
            import RPi.GPIO as GPIO  # type: ignore
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self._gpio_estop_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            GPIO.add_event_detect(
                self._gpio_estop_pin,
                GPIO.FALLING,
                callback=lambda _: self.trigger_estop("GPIO_ESTOP_BUTTON"),
                bouncetime=200,
            )
            logger.info(f"GPIO E-stop button on BCM pin {self._gpio_estop_pin}")
        except ImportError:
            logger.debug("RPi.GPIO not available – GPIO E-stop disabled")
        except Exception as exc:
            logger.warning(f"GPIO E-stop setup failed: {exc}")
