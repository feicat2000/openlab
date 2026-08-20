"""
Tube handler – cap tightening, cap loosening, tube inversion.

These operations are performed by the UR arm (robotic_arm.py).
This module provides:
  • The TubeHandler class as a logical layer that orchestrates arm motions.
  • It does NOT talk to hardware directly – it delegates to RoboticArm.
  • Cap tightening uses force-controlled motion (UR force mode).
  • Inversion uses wrist rotation.

Hardware accessories
─────────────────────
• Robotiq 2F-85 gripper (on UR arm)  – grasps tube body
• Robotiq Hand-E or custom cap gripper (optional 2nd arm / tool-changer)
• Cap torque fixture (passive) – holds tube while arm screws cap
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from loguru import logger

if TYPE_CHECKING:
    from ..robotic_arm import RoboticArm, Pose


@dataclass
class TubeSpec:
    """Physical parameters of the tube being handled."""
    label: str                    # e.g. "1.5mL_eppendorf"
    diameter_mm: float = 10.8    # outer diameter
    length_mm: float   = 40.5    # total length
    cap_torque_Nm: float = 0.3   # target cap tightening torque
    cap_turns: float   = 2.0     # full turns needed to open/close


class TubeHandler:
    """
    High-level tube manipulation interface.
    Delegates all motion to the provided RoboticArm instance.
    """

    def __init__(self, arm: "RoboticArm") -> None:
        self._arm = arm
        self._log = logger.bind(module="TubeHandler")

    # ── Cap operations ────────────────────────────────────────────────────────

    def tighten_cap(
        self,
        tube_position: "Pose",
        tube_spec: Optional[TubeSpec] = None,
    ) -> bool:
        """
        Pick up tube, tighten cap using force control, return to rack.
        Returns True if target torque was achieved.
        """
        spec = tube_spec or TubeSpec(label="default")
        self._log.info(f"Tightening cap on {spec.label} at {tube_position[:3]}")

        # Move to tube, grip body
        self._arm.pick_tube(tube_position)

        # Move to cap-tightening fixture position
        self._arm.move_to(self._arm._positions.cap_tightener)

        # Force-controlled screw
        success = self._arm.tighten_cap(
            target_torque_Nm=spec.cap_torque_Nm,
            max_turns=spec.cap_turns + 1.0,
        )

        # Return tube to rack
        self._arm.place_tube(tube_position)
        self._arm.home()
        return success

    def loosen_cap(
        self,
        tube_position: "Pose",
        tube_spec: Optional[TubeSpec] = None,
    ) -> None:
        """Pick up tube, loosen cap N turns, return to rack."""
        spec = tube_spec or TubeSpec(label="default")
        self._log.info(f"Loosening cap on {spec.label}")

        self._arm.pick_tube(tube_position)
        self._arm.move_to(self._arm._positions.cap_tightener)
        self._arm.loosen_cap(turns=spec.cap_turns)
        self._arm.place_tube(tube_position)
        self._arm.home()

    # ── Inversion ─────────────────────────────────────────────────────────────

    def invert_tube(
        self,
        tube_position: "Pose",
        n_times: int = 10,
        pause_s: float = 1.0,
    ) -> None:
        """
        Pick up tube, invert n_times (180° wrist rotation), return to rack.
        Used for gentle mixing without vortexing.
        """
        self._log.info(f"Inverting tube {n_times}x at {tube_position[:3]}")
        self._arm.pick_tube(tube_position)
        self._arm.invert_tube(n_times=n_times, pause_s=pause_s)
        self._arm.place_tube(tube_position)
        self._arm.home()

    # ── Transport ─────────────────────────────────────────────────────────────

    def transfer_tube(self, source: "Pose", destination: "Pose") -> None:
        """
        Move a tube from one station to another.
        Typically used to move tubes between Opentrons deck and peripherals.
        """
        self._log.info(f"Transferring tube {source[:3]} → {destination[:3]}")
        self._arm.pick_tube(source)
        self._arm.home()          # intermediate safe point
        self._arm.place_tube(destination)

    def transfer_to_vortex(self, tube_position: "Pose") -> None:
        self.transfer_tube(tube_position, self._arm._positions.vortex)

    def transfer_to_centrifuge(self, tube_position: "Pose") -> None:
        self.transfer_tube(tube_position, self._arm._positions.centrifuge_load)

    def retrieve_from_centrifuge(self, tube_position: "Pose") -> None:
        self.transfer_tube(self._arm._positions.centrifuge_unload, tube_position)

    def transfer_to_magnetic_separator(self, tube_position: "Pose") -> None:
        self.transfer_tube(tube_position, self._arm._positions.magnetic_separator)

    def transfer_to_rocker(self, tube_position: "Pose") -> None:
        self.transfer_tube(tube_position, self._arm._positions.rocker)

    def transfer_to_incubator(self, tube_position: "Pose") -> None:
        self.transfer_tube(tube_position, self._arm._positions.incubator)

    def retrieve_from_incubator(self, tube_position: "Pose") -> None:
        self.transfer_tube(self._arm._positions.incubator, tube_position)
