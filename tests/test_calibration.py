"""
Tests for CalibrationManager.
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import pytest
import yaml

from src.calibration.calibration_manager import (
    CalibrationManager,
    CalibrationProfile,
    PoseCalibration,
    ArmCalibration,
    PipetteCalibration,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmpdir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def mgr(tmpdir):
    return CalibrationManager(profiles_dir=tmpdir / "calibration")


@pytest.fixture
def profile(mgr):
    return mgr.create_profile("test_profile", operator="alice")


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestProfileLifecycle:
    def test_create_profile_returns_profile(self, mgr):
        p = mgr.create_profile("prod", operator="bob")
        assert isinstance(p, CalibrationProfile)
        assert p.profile_name == "prod"
        assert p.created_at > 0

    def test_save_creates_json_file(self, mgr, profile):
        path = mgr.save(profile)
        assert path.exists()
        assert path.suffix == ".json"

    def test_load_round_trip(self, mgr, profile):
        mgr.set_pose(profile, "home", [0.3, -0.2, 0.5, 3.14, 0.0, 0.0], "alice")
        mgr.save(profile)
        loaded = mgr.load("test_profile")
        assert loaded.profile_name == "test_profile"
        assert "home" in loaded.arm.poses
        assert loaded.arm.poses["home"].pose == [0.3, -0.2, 0.5, 3.14, 0.0, 0.0]

    def test_list_profiles_returns_saved_names(self, mgr):
        p1 = mgr.create_profile("alpha", operator="alice")
        p2 = mgr.create_profile("beta", operator="bob")
        mgr.save(p1)
        mgr.save(p2)
        names = mgr.list_profiles()
        assert "alpha" in names
        assert "beta" in names

    def test_load_nonexistent_raises(self, mgr):
        with pytest.raises(FileNotFoundError):
            mgr.load("no_such_profile")


class TestSetPose:
    def test_set_pose_stores_correctly(self, mgr, profile):
        pose = [0.2, 0.3, 0.25, 3.14, 0.0, 0.0]
        mgr.set_pose(profile, "vortex", pose, "alice", notes="calibrated day 1")
        assert "vortex" in profile.arm.poses
        assert profile.arm.poses["vortex"].pose == pose
        assert profile.arm.poses["vortex"].notes == "calibrated day 1"

    def test_set_pose_creates_arm_if_absent(self, mgr, profile):
        assert profile.arm is None
        mgr.set_pose(profile, "home", [0.0] * 6, "alice")
        assert profile.arm is not None

    def test_set_pose_overwrites_existing(self, mgr, profile):
        mgr.set_pose(profile, "home", [0.1] * 6, "alice")
        mgr.set_pose(profile, "home", [0.9] * 6, "bob")
        assert profile.arm.poses["home"].pose == [0.9] * 6
        assert profile.arm.poses["home"].calibrated_by == "bob"


class TestRecordArm:
    def test_record_arm_populates_fields(self, mgr, profile):
        mgr.record_arm(
            profile,
            serial_number="UR3e-001",
            tcp_offset=[0.0, 0.0, 0.17, 0.0, 0.0, 0.0],
            payload_kg=0.85,
            payload_cog=[0.0, 0.0, 0.05],
            operator="alice",
        )
        assert profile.arm.serial_number == "UR3e-001"
        assert profile.arm.payload_kg == 0.85
        assert profile.arm.calibrated_by == "alice"

    def test_record_arm_preserves_existing_poses(self, mgr, profile):
        mgr.set_pose(profile, "home", [0.3, -0.2, 0.5, 3.14, 0.0, 0.0], "alice")
        mgr.record_arm(profile, "SN-002", [0.0]*6, 0.5, [0.0]*3, "bob")
        assert "home" in profile.arm.poses


class TestRecordPipette:
    def test_record_pipette_populates_fields(self, mgr, profile):
        mgr.record_pipette(
            profile,
            mount="left",
            model="P300 Single-GEN2",
            tip_length=59.3,
            offset=[0.0, 0.0, -0.5],
            operator="alice",
        )
        assert len(profile.pipettes) == 1
        p = profile.pipettes[0]
        assert p.mount == "left"
        assert p.model == "P300 Single-GEN2"
        assert p.tip_length_mm == 59.3

    def test_record_pipette_replaces_same_mount(self, mgr, profile):
        mgr.record_pipette(profile, "left", "P300", 59.3, [0]*3, "alice")
        mgr.record_pipette(profile, "left", "P1000", 95.6, [0]*3, "bob")
        lefts = [p for p in profile.pipettes if p.mount == "left"]
        assert len(lefts) == 1
        assert lefts[0].model == "P1000"


class TestCheckDrift:
    def test_zero_drift_for_identical_poses(self, mgr, profile):
        pose = [0.3, -0.2, 0.5, 3.14, 0.0, 0.0]
        mgr.set_pose(profile, "home", pose, "alice")
        drifted = mgr.check_drift(profile, {"home": pose}, threshold_mm=2.0)
        assert drifted == {}

    def test_detects_drift_above_threshold(self, mgr, profile):
        mgr.set_pose(profile, "home", [0.3, -0.2, 0.5, 3.14, 0.0, 0.0], "alice")
        current = {"home": [0.310, -0.2, 0.5, 3.14, 0.0, 0.0]}  # 10 mm shift
        drifted = mgr.check_drift(profile, current, threshold_mm=2.0)
        assert "home" in drifted
        assert drifted["home"] > 2.0

    def test_no_arm_returns_empty(self, mgr, profile):
        assert profile.arm is None
        result = mgr.check_drift(profile, {"home": [0]*6})
        assert result == {}


class TestApplyToConfig:
    def test_writes_arm_positions_to_yaml(self, mgr, profile, tmpdir):
        cfg_path = tmpdir / "hw.yaml"
        cfg_path.write_text("simulation_mode: true\n")
        mgr.set_pose(profile, "home", [0.3, -0.2, 0.5, 3.14, 0.0, 0.0], "alice")
        mgr.apply_to_config(profile, cfg_path)
        with open(cfg_path) as fh:
            cfg = yaml.safe_load(fh)
        assert "arm_positions" in cfg
        assert "home" in cfg["arm_positions"]


class TestExportReport:
    def test_creates_file(self, mgr, profile, tmpdir):
        mgr.set_pose(profile, "home", [0.3]*6, "alice")
        out = tmpdir / "report.txt"
        mgr.export_report(profile, out)
        assert out.exists()
        content = out.read_text()
        assert "test_profile" in content
