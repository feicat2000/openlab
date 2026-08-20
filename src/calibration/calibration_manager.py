"""
Calibration Manager – stores and manages arm/pipette calibration profiles.

Profiles are saved as JSON in a ``calibration/`` directory.
Each profile contains named TCP poses, arm TCP offset, payload info,
and pipette calibration data.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from loguru import logger
from rich.console import Console
from rich.table import Table


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class PoseCalibration:
    name: str
    pose: List[float]          # [x, y, z, rx, ry, rz] metres/radians
    calibrated_at: float
    calibrated_by: str
    notes: str = ""


@dataclass
class ArmCalibration:
    serial_number: str
    tcp_offset: List[float]    # [x,y,z,rx,ry,rz] tool centre offset
    payload_kg: float
    payload_cog: List[float]   # [x,y,z] centre of gravity
    poses: Dict[str, PoseCalibration]
    calibrated_at: float
    calibrated_by: str


@dataclass
class PipetteCalibration:
    mount: str                 # "left" | "right"
    model: str                 # e.g. "P300 Single-GEN2"
    tip_length_mm: float
    offset: List[float]        # [x, y, z] correction vs factory
    calibrated_at: float
    calibrated_by: str


@dataclass
class CalibrationProfile:
    profile_name: str
    created_at: float
    arm: Optional[ArmCalibration] = None
    pipettes: List[PipetteCalibration] = field(default_factory=list)


# ── Serialisation helpers ─────────────────────────────────────────────────────

def _profile_to_dict(profile: CalibrationProfile) -> dict:
    d = {
        "profile_name": profile.profile_name,
        "created_at": profile.created_at,
        "arm": None,
        "pipettes": [],
    }
    if profile.arm:
        arm = profile.arm
        d["arm"] = {
            "serial_number": arm.serial_number,
            "tcp_offset": arm.tcp_offset,
            "payload_kg": arm.payload_kg,
            "payload_cog": arm.payload_cog,
            "calibrated_at": arm.calibrated_at,
            "calibrated_by": arm.calibrated_by,
            "poses": {
                k: {
                    "name": v.name,
                    "pose": v.pose,
                    "calibrated_at": v.calibrated_at,
                    "calibrated_by": v.calibrated_by,
                    "notes": v.notes,
                }
                for k, v in arm.poses.items()
            },
        }
    d["pipettes"] = [
        {
            "mount": p.mount,
            "model": p.model,
            "tip_length_mm": p.tip_length_mm,
            "offset": p.offset,
            "calibrated_at": p.calibrated_at,
            "calibrated_by": p.calibrated_by,
        }
        for p in profile.pipettes
    ]
    return d


def _profile_from_dict(d: dict) -> CalibrationProfile:
    arm = None
    if d.get("arm"):
        a = d["arm"]
        poses = {
            k: PoseCalibration(**v) for k, v in a.get("poses", {}).items()
        }
        arm = ArmCalibration(
            serial_number=a["serial_number"],
            tcp_offset=a["tcp_offset"],
            payload_kg=a["payload_kg"],
            payload_cog=a["payload_cog"],
            poses=poses,
            calibrated_at=a["calibrated_at"],
            calibrated_by=a["calibrated_by"],
        )
    pipettes = [PipetteCalibration(**p) for p in d.get("pipettes", [])]
    return CalibrationProfile(
        profile_name=d["profile_name"],
        created_at=d["created_at"],
        arm=arm,
        pipettes=pipettes,
    )


# ── CalibrationManager ────────────────────────────────────────────────────────

class CalibrationManager:
    """
    Manage robot and pipette calibration profiles.

    Usage::

        mgr = CalibrationManager()
        profile = mgr.create_profile("production", operator="alice")
        mgr.set_pose(profile, "vortex", [0.2, 0.3, 0.25, 3.14, 0, 0], "alice")
        mgr.save(profile)
    """

    def __init__(self, profiles_dir: Path = Path("calibration")) -> None:
        self._dir = Path(profiles_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._console = Console()

    # ── Profile lifecycle ─────────────────────────────────────────────────────

    def list_profiles(self) -> List[str]:
        """Return names of all saved profiles (without .json extension)."""
        return sorted(p.stem for p in self._dir.glob("*.json"))

    def load(self, profile_name: str) -> CalibrationProfile:
        path = self._dir / f"{profile_name}.json"
        if not path.exists():
            raise FileNotFoundError(f"Profile not found: {path}")
        with open(path) as fh:
            return _profile_from_dict(json.load(fh))

    def save(self, profile: CalibrationProfile) -> Path:
        path = self._dir / f"{profile.profile_name}.json"
        with open(path, "w") as fh:
            json.dump(_profile_to_dict(profile), fh, indent=2)
        logger.info(f"Calibration profile saved: {path}")
        return path

    def create_profile(self, name: str, operator: str) -> CalibrationProfile:
        profile = CalibrationProfile(
            profile_name=name,
            created_at=time.time(),
        )
        logger.info(f"Created calibration profile '{name}' by {operator}")
        return profile

    # ── Pose management ───────────────────────────────────────────────────────

    def set_pose(
        self,
        profile: CalibrationProfile,
        pose_name: str,
        pose: List[float],
        operator: str,
        notes: str = "",
    ) -> None:
        """Record or update a named TCP pose on the profile."""
        if profile.arm is None:
            profile.arm = ArmCalibration(
                serial_number="unknown",
                tcp_offset=[0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                payload_kg=0.0,
                payload_cog=[0.0, 0.0, 0.0],
                poses={},
                calibrated_at=time.time(),
                calibrated_by=operator,
            )
        pc = PoseCalibration(
            name=pose_name,
            pose=list(pose),
            calibrated_at=time.time(),
            calibrated_by=operator,
            notes=notes,
        )
        profile.arm.poses[pose_name] = pc
        logger.info(f"Pose '{pose_name}' set by {operator}: {pose}")

    def record_arm(
        self,
        profile: CalibrationProfile,
        serial_number: str,
        tcp_offset: List[float],
        payload_kg: float,
        payload_cog: List[float],
        operator: str,
    ) -> None:
        """Record or update arm-level calibration data."""
        existing_poses = profile.arm.poses if profile.arm else {}
        profile.arm = ArmCalibration(
            serial_number=serial_number,
            tcp_offset=list(tcp_offset),
            payload_kg=payload_kg,
            payload_cog=list(payload_cog),
            poses=existing_poses,
            calibrated_at=time.time(),
            calibrated_by=operator,
        )
        logger.info(f"Arm calibration recorded by {operator} (SN: {serial_number})")

    def record_pipette(
        self,
        profile: CalibrationProfile,
        mount: str,
        model: str,
        tip_length: float,
        offset: List[float],
        operator: str,
    ) -> None:
        """Add or replace pipette calibration for a given mount."""
        # Remove existing entry for this mount
        profile.pipettes = [p for p in profile.pipettes if p.mount != mount]
        pc = PipetteCalibration(
            mount=mount,
            model=model,
            tip_length_mm=tip_length,
            offset=list(offset),
            calibrated_at=time.time(),
            calibrated_by=operator,
        )
        profile.pipettes.append(pc)
        logger.info(f"Pipette '{model}' on {mount} calibrated by {operator}")

    # ── Drift detection ───────────────────────────────────────────────────────

    def check_drift(
        self,
        profile: CalibrationProfile,
        current_poses: Dict[str, List[float]],
        threshold_mm: float = 2.0,
    ) -> Dict[str, float]:
        """
        Compare current robot poses against calibrated poses.

        Returns {pose_name: drift_mm} only for poses that exceed the threshold.
        """
        if profile.arm is None:
            return {}

        drifted: Dict[str, float] = {}
        for name, current in current_poses.items():
            if name not in profile.arm.poses:
                continue
            ref = profile.arm.poses[name].pose
            # Euclidean distance on XYZ (first 3 components), convert m → mm
            drift_m = math.sqrt(
                sum((a - b) ** 2 for a, b in zip(current[:3], ref[:3]))
            )
            drift_mm = drift_m * 1000.0
            if drift_mm > threshold_mm:
                drifted[name] = round(drift_mm, 3)
        return drifted

    # ── Config application ────────────────────────────────────────────────────

    def apply_to_config(
        self,
        profile: CalibrationProfile,
        config_path: Path,
    ) -> None:
        """Overwrite arm_positions section in hardware_config.yaml with calibrated poses."""
        config_path = Path(config_path)
        with open(config_path) as fh:
            cfg = yaml.safe_load(fh) or {}

        if profile.arm:
            cfg["arm_positions"] = {
                name: pc.pose for name, pc in profile.arm.poses.items()
            }

        with open(config_path, "w") as fh:
            yaml.dump(cfg, fh, default_flow_style=False, sort_keys=False)

        logger.info(f"Calibration applied to {config_path}")

    # ── Report export ─────────────────────────────────────────────────────────

    def export_report(
        self,
        profile: CalibrationProfile,
        output_path: Path,
    ) -> None:
        """Write a human-readable calibration report to a text file."""
        output_path = Path(output_path)
        lines = [
            f"Calibration Profile: {profile.profile_name}",
            f"Created: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(profile.created_at))}",
            "",
        ]

        if profile.arm:
            arm = profile.arm
            lines += [
                "=== ARM CALIBRATION ===",
                f"Serial Number : {arm.serial_number}",
                f"Calibrated by : {arm.calibrated_by}",
                f"TCP Offset    : {arm.tcp_offset}",
                f"Payload (kg)  : {arm.payload_kg}",
                f"CoG           : {arm.payload_cog}",
                "",
                "Named Poses:",
            ]
            for pname, pc in arm.poses.items():
                lines.append(f"  {pname:<24} {[round(v,4) for v in pc.pose]}  (by {pc.calibrated_by})")

        if profile.pipettes:
            lines += ["", "=== PIPETTE CALIBRATIONS ==="]
            for p in profile.pipettes:
                lines += [
                    f"  Mount  : {p.mount}",
                    f"  Model  : {p.model}",
                    f"  Tip len: {p.tip_length_mm} mm",
                    f"  Offset : {p.offset}",
                    f"  By     : {p.calibrated_by}",
                    "",
                ]

        output_path.write_text("\n".join(lines))
        logger.info(f"Calibration report written: {output_path}")

    # ── Rich status print ─────────────────────────────────────────────────────

    def print_status(self, profile: CalibrationProfile) -> None:
        self._console.print(
            f"\n[bold cyan]Calibration Profile:[/bold cyan] {profile.profile_name}"
        )
        self._console.print(
            f"Created: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(profile.created_at))}"
        )

        if profile.arm:
            tbl = Table(title="Named Poses", show_header=True)
            tbl.add_column("Pose", style="cyan")
            tbl.add_column("XYZ (m)")
            tbl.add_column("RPY (rad)")
            tbl.add_column("By")
            for pname, pc in profile.arm.poses.items():
                xyz = [round(v, 4) for v in pc.pose[:3]]
                rpy = [round(v, 4) for v in pc.pose[3:]]
                tbl.add_row(pname, str(xyz), str(rpy), pc.calibrated_by)
            self._console.print(tbl)
        else:
            self._console.print("[dim]No arm calibration data.[/dim]")

        if profile.pipettes:
            for p in profile.pipettes:
                self._console.print(
                    f"  Pipette [{p.mount}]: {p.model}, tip={p.tip_length_mm}mm"
                )
