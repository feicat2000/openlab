"""
Universal Robots UR3e / UR5e / UR10e controller.

Communication
─────────────
• RTDE (Real-Time Data Exchange) via `ur_rtde` Python library
  - Port 30004 (RTDE)  – real-time state & control
  - Port 29999 (Dashboard) – run/stop/load programs
  - Port 30002 (Secondary) – URScript streaming

Coordinate system
─────────────────
All positions are stored as [x, y, z, rx, ry, rz] in metres / radians
(tool pose in robot base frame).  Named positions are loaded from
config/hardware_config.yaml  →  robotic_arm.positions.

Gripper
───────
Supports Robotiq 2F-85 / 2F-140 via Modbus-TCP or RS-232.
Set GRIPPER_TYPE=robotiq_2f in hardware_config.yaml.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from loguru import logger

from .base_equipment import BaseEquipment, EquipmentConfig, EquipmentStatus

# Optional imports – graceful if library not installed
try:
    import rtde_control  # type: ignore
    import rtde_receive  # type: ignore
    import dashboard_client  # type: ignore
    HAS_RTDE = True
except ImportError:
    HAS_RTDE = False
    logger.warning("ur_rtde not installed – UR arm will run in simulation only")


Pose = List[float]   # [x, y, z, rx, ry, rz]


@dataclass
class ArmPositions:
    """Named Cartesian poses loaded from config."""
    home:               Pose = field(default_factory=lambda: [0.3, -0.2, 0.5, 3.14, 0.0, 0.0])
    opentrons_handoff:  Pose = field(default_factory=lambda: [0.4, -0.1, 0.3, 3.14, 0.0, 0.0])
    vortex:             Pose = field(default_factory=lambda: [0.2, 0.3, 0.25, 3.14, 0.0, 0.0])
    centrifuge_load:    Pose = field(default_factory=lambda: [0.1, 0.4, 0.2, 3.14, 0.0, 0.0])
    centrifuge_unload:  Pose = field(default_factory=lambda: [0.1, 0.4, 0.3, 3.14, 0.0, 0.0])
    magnetic_separator: Pose = field(default_factory=lambda: [0.35, 0.25, 0.2, 3.14, 0.0, 0.0])
    rocker:             Pose = field(default_factory=lambda: [0.25, 0.35, 0.2, 3.14, 0.0, 0.0])
    incubator:          Pose = field(default_factory=lambda: [0.0, 0.45, 0.2, 3.14, 0.0, 0.0])
    tube_rack:          Pose = field(default_factory=lambda: [0.3, 0.0, 0.25, 3.14, 0.0, 0.0])
    cap_tightener:      Pose = field(default_factory=lambda: [0.15, 0.35, 0.2, 3.14, 0.0, 0.0])

    @classmethod
    def from_dict(cls, d: dict) -> "ArmPositions":
        obj = cls()
        for key, val in d.items():
            if hasattr(obj, key):
                setattr(obj, key, val)
        return obj


class RoboticArm(BaseEquipment):
    """
    UR3e/UR5e collaborative arm driver with Robotiq gripper support.

    Capabilities used in OpenLab:
      - move_to(pose)            : Cartesian MoveL
      - pick_tube(position)      : approach → grip → retract
      - place_tube(position)     : approach → release → retract
      - vortex_tube(duration)    : oscillate on vortex pad
      - invert_tube(n)           : rotate wrist 180° × n times
      - tighten_cap(torque_Nm)   : force-controlled screw motion
      - loosen_cap(turns)        : reverse screw
      - gripper_open/close()     : direct gripper control
    """

    # Typical motion parameters
    SPEED_FAST   = 0.5   # m/s
    SPEED_SLOW   = 0.1   # m/s
    ACCEL        = 0.5   # m/s²
    BLEND        = 0.01  # m

    def __init__(self, config: EquipmentConfig, positions: Optional[ArmPositions] = None) -> None:
        super().__init__(config)
        self._positions  = positions or ArmPositions()
        self._rtde_c     = None   # rtde_control.RTDEControlInterface
        self._rtde_r     = None   # rtde_receive.RTDEReceiveInterface
        self._dashboard  = None   # dashboard_client.DashboardClient
        self._gripper_open_mm  = config.extra.get("gripper_open_mm", 85)
        self._gripper_close_mm = config.extra.get("gripper_close_mm", 20)
        self._gripper_force    = config.extra.get("gripper_force", 40)   # %

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        if self.config.simulate or not HAS_RTDE:
            self._log.info("UR arm: simulation mode")
            self.status = EquipmentStatus.IDLE
            return True

        ip = self.config.ip_address or "192.168.1.10"
        try:
            self._rtde_c    = rtde_control.RTDEControlInterface(ip)
            self._rtde_r    = rtde_receive.RTDEReceiveInterface(ip)
            self._dashboard = dashboard_client.DashboardClient(ip)
            self._dashboard.connect()
            robot_mode = self._rtde_r.getRobotMode()
            self._log.info(f"UR arm connected at {ip} – mode {robot_mode}")
            self.status = EquipmentStatus.IDLE
            return True
        except Exception as exc:
            self._log.error(f"UR arm connect failed: {exc}")
            self.status = EquipmentStatus.ERROR
            return False

    def disconnect(self) -> None:
        try:
            if self._rtde_c:
                self._rtde_c.stopScript()
                self._rtde_c.disconnect()
            if self._dashboard:
                self._dashboard.disconnect()
        except Exception:
            pass
        self.status = EquipmentStatus.DISCONNECTED
        self._log.info("UR arm disconnected")

    def is_ready(self) -> bool:
        if self.config.simulate:
            return self.status == EquipmentStatus.IDLE
        if not self._rtde_r:
            return False
        try:
            mode = self._rtde_r.getRobotMode()
            return mode == 7  # RUNNING
        except Exception:
            return False

    # ── Motion primitives ─────────────────────────────────────────────────────

    def move_to(self, pose: Pose, speed: float = SPEED_FAST, accel: float = ACCEL) -> None:
        """Move TCP to Cartesian pose (MoveL – linear in Cartesian space)."""
        if self.config.simulate:
            self._simulate_op(f"move_to {[f'{v:.3f}' for v in pose]}", duration=1.5)
            return
        success = self._rtde_c.moveL(pose, speed, accel)
        if not success:
            raise RuntimeError(f"moveL failed to pose {pose}")

    def move_joint(self, joints: List[float], speed: float = 0.5, accel: float = 0.5) -> None:
        """Move to joint configuration (MoveJ – faster for large moves)."""
        if self.config.simulate:
            self._simulate_op(f"moveJ {joints}", duration=2.0)
            return
        self._rtde_c.moveJ(joints, speed, accel)

    def home(self) -> None:
        """Move to home position (safe, away from deck)."""
        self._log.info("Moving to home position")
        self.move_to(self._positions.home, speed=self.SPEED_FAST)

    # ── Gripper ───────────────────────────────────────────────────────────────

    def gripper_open(self) -> None:
        if self.config.simulate:
            self._simulate_op("gripper open", 0.5)
            return
        # Robotiq gripper URScript command
        self._send_urscript(
            f"rq_move_and_wait_mm({self._gripper_open_mm})"
        )

    def gripper_close(self, force_pct: Optional[int] = None) -> None:
        force = force_pct or self._gripper_force
        if self.config.simulate:
            self._simulate_op(f"gripper close (force {force}%)", 0.5)
            return
        self._send_urscript(
            f"rq_move_and_wait_mm({self._gripper_close_mm})\n"
            f"rq_set_force({force})"
        )

    # ── High-level lab operations ─────────────────────────────────────────────

    def pick_tube(self, position: Pose, approach_offset_z: float = 0.05) -> None:
        """
        Pick up a tube:  approach above → descend → grip → retract.
        approach_offset_z: metres above tube before descent.
        """
        self._log.info(f"Picking tube at {position[:3]}")
        approach = position.copy()
        approach[2] += approach_offset_z

        self.gripper_open()
        self.move_to(approach, speed=self.SPEED_FAST)
        self.move_to(position, speed=self.SPEED_SLOW)
        self.gripper_close()
        time.sleep(0.3)
        self.move_to(approach, speed=self.SPEED_SLOW)

    def place_tube(self, position: Pose, approach_offset_z: float = 0.05) -> None:
        """
        Place a tube: approach above → descend → release → retract.
        """
        self._log.info(f"Placing tube at {position[:3]}")
        approach = position.copy()
        approach[2] += approach_offset_z

        self.move_to(approach, speed=self.SPEED_FAST)
        self.move_to(position, speed=self.SPEED_SLOW)
        self.gripper_open()
        time.sleep(0.3)
        self.move_to(approach, speed=self.SPEED_SLOW)

    def invert_tube(self, n_times: int = 1, pause_s: float = 1.0) -> None:
        """
        Invert tube n times by rotating wrist 180° and back.
        Tube must already be gripped.
        """
        self._log.info(f"Inverting tube {n_times}x")
        for i in range(n_times):
            if self.config.simulate:
                self._simulate_op(f"invert tube #{i+1}", 2.0)
                continue
            # Rotate tool by 180° around Z (wrist 3)
            current = self._rtde_r.getActualTCPPose()
            inverted = current.copy()
            inverted[5] += 3.1416  # +π rad
            self._rtde_c.moveL(inverted, self.SPEED_SLOW, self.ACCEL)
            time.sleep(pause_s)
            self._rtde_c.moveL(current, self.SPEED_SLOW, self.ACCEL)
            time.sleep(pause_s)

    def tighten_cap(
        self,
        target_torque_Nm: float = 0.3,
        max_turns: float = 3.0,
    ) -> bool:
        """
        Force-controlled cap tightening.
        Screw clockwise until target torque is reached or max_turns exceeded.
        Returns True if target torque was reached.
        """
        self._log.info(f"Tightening cap (target {target_torque_Nm} Nm, max {max_turns} turns)")
        if self.config.simulate:
            self._simulate_op(f"tighten cap @ {target_torque_Nm} Nm", 3.0)
            return True

        # Force mode: free in X/Y translation, compliant in Z rotation
        task_frame     = [0, 0, 0, 0, 0, 0]
        selection_vec  = [0, 0, 0, 0, 0, 1]   # control rotation only
        wrench         = [0, 0, 0, 0, 0, target_torque_Nm]
        f_type         = 2   # wrench type
        limits         = [0.1, 0.1, 0.1, 0.1, 0.1, 0.3]  # m/s, rad/s limits

        self._rtde_c.forceMode(task_frame, selection_vec, wrench, f_type, limits)

        deg_per_step = 10
        step_rad     = deg_per_step * 3.1416 / 180
        total_turns  = 0.0
        step_per_turn = 360 / deg_per_step
        reached = False

        while total_turns < max_turns:
            current = self._rtde_r.getActualTCPPose()
            target  = current.copy()
            target[5] += step_rad
            self._rtde_c.moveL(target, 0.05, 0.1)

            # Check actual torque via force sensor
            tcp_force = self._rtde_r.getActualTCPForce()
            actual_torque = abs(tcp_force[5])
            total_turns += deg_per_step / 360.0

            if actual_torque >= target_torque_Nm:
                self._log.info(f"Target torque {actual_torque:.2f} Nm reached")
                reached = True
                break

        self._rtde_c.forceModeStop()
        return reached

    def loosen_cap(self, turns: float = 2.5) -> None:
        """Unscrew cap by specified number of turns (no force control needed)."""
        self._log.info(f"Loosening cap ({turns} turns)")
        if self.config.simulate:
            self._simulate_op(f"loosen cap {turns} turns", 2.0)
            return
        current = self._rtde_r.getActualTCPPose()
        target  = current.copy()
        target[5] -= turns * 2 * 3.1416   # clockwise = positive → unscrew = negative
        self._rtde_c.moveL(target, 0.05, 0.1)

    def vortex_motion(self, duration_s: float = 30.0, amplitude_m: float = 0.003) -> None:
        """
        Hold tube on vortex pad.  The vortex itself shakes; the arm just
        applies gentle downward pressure while holding position.
        """
        self._log.info(f"Holding tube on vortex for {duration_s}s")
        if self.config.simulate:
            self._simulate_op(f"vortex hold {duration_s}s", duration_s)
            return
        # Apply slight downward force while vortex runs
        task_frame    = [0, 0, 0, 0, 0, 0]
        selection_vec = [0, 0, 1, 0, 0, 0]   # compliant in Z
        wrench        = [0, 0, -2.0, 0, 0, 0] # 2 N downward
        self._rtde_c.forceMode(task_frame, selection_vec, wrench, 2, [0.1]*6)
        time.sleep(duration_s)
        self._rtde_c.forceModeStop()

    # ── Named-position wrappers ───────────────────────────────────────────────

    def go_to_opentrons_handoff(self) -> None:
        self.move_to(self._positions.opentrons_handoff)

    def go_to_vortex(self) -> None:
        self.move_to(self._positions.vortex)

    def go_to_centrifuge_load(self) -> None:
        self.move_to(self._positions.centrifuge_load)

    def go_to_magnetic_separator(self) -> None:
        self.move_to(self._positions.magnetic_separator)

    def go_to_rocker(self) -> None:
        self.move_to(self._positions.rocker)

    def go_to_incubator(self) -> None:
        self.move_to(self._positions.incubator)

    def go_to_tube_rack(self) -> None:
        self.move_to(self._positions.tube_rack)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _send_urscript(self, script: str) -> None:
        """Stream URScript to port 30002."""
        import socket
        ip   = self.config.ip_address or "192.168.1.10"
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.connect((ip, 30002))
            sock.sendall((script + "\n").encode())
        finally:
            sock.close()

    def _on_emergency_stop(self) -> None:
        if self.config.simulate:
            return
        try:
            if self._rtde_c:
                self._rtde_c.stopScript()
                self._rtde_c.triggerProtectiveStop()
        except Exception as exc:
            self._log.error(f"UR E-stop failed: {exc}")
