"""
Abstract base class for all OpenLab equipment drivers.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

from loguru import logger


class EquipmentStatus(str, Enum):
    DISCONNECTED = "disconnected"
    IDLE         = "idle"
    RUNNING      = "running"
    ERROR        = "error"
    ESTOP        = "emergency_stop"


@dataclass
class EquipmentConfig:
    name: str
    port: Optional[str]         = None   # serial port e.g. /dev/ttyUSB0
    ip_address: Optional[str]   = None   # network address
    baud_rate: int              = 9600
    timeout: float              = 30.0   # seconds
    simulate: bool              = True   # safe default
    extra: Dict[str, Any]       = field(default_factory=dict)


class BaseEquipment(ABC):
    """All equipment drivers inherit from this."""

    def __init__(self, config: EquipmentConfig) -> None:
        self.config  = config
        self.status  = EquipmentStatus.DISCONNECTED
        self._log    = logger.bind(equipment=config.name)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @abstractmethod
    def connect(self) -> bool:
        """Open connection to hardware. Return True on success."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close connection cleanly."""

    @abstractmethod
    def is_ready(self) -> bool:
        """Return True if the device is connected and idle."""

    def emergency_stop(self) -> None:
        self.status = EquipmentStatus.ESTOP
        self._log.critical("Emergency stop triggered")
        self._on_emergency_stop()

    def _on_emergency_stop(self) -> None:
        """Override in subclass to send hardware e-stop command."""

    # ── Context manager ───────────────────────────────────────────────────────

    def __enter__(self) -> "BaseEquipment":
        self.connect()
        return self

    def __exit__(self, *_: Any) -> None:
        self.disconnect()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _wait_until_idle(self, poll_interval: float = 0.5, max_wait: float = 600.0) -> bool:
        """Poll is_ready() until the device becomes idle or timeout."""
        deadline = time.time() + max_wait
        while time.time() < deadline:
            if self.is_ready():
                return True
            time.sleep(poll_interval)
        self._log.error("Timeout waiting for device to become idle")
        return False

    def _simulate_op(self, op_name: str, duration: float = 0.1) -> None:
        """Log a simulated operation."""
        self._log.info(f"[SIMULATE] {op_name} ({duration:.1f}s)")
        time.sleep(min(duration, 0.5))  # cap simulation wait

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.config.name!r}, status={self.status})"
