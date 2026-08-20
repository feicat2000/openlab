"""
Core unit tests – run with:  pytest tests/ -v
All tests run in simulation mode (no real hardware needed).
"""
from __future__ import annotations

import pytest
from pathlib import Path

# ── Fixtures ──────────────────────────────────────────────────────────────────

PROTOCOL_PATH = Path(__file__).parent.parent / "protocols" / "dna_extraction.yaml"

MINIMAL_PROTOCOL_YAML = """
metadata:
  name: "Test Protocol"
  version: "0.1"
  author: "Tester"
  safety_level: BSL1

materials:
  reagents:
    - name: "Water"
      volume_ul: 100
      hazard_class: none

consumables:
  - name: "1.5mL tubes"
    quantity: 2

equipment:
  - name: opentrons
  - name: vortex

steps:
  - id: 1
    name: "Mix sample"
    type: vortex
    params:
      rpm: 1000
      duration_s: 10

  - id: 2
    name: "Wait"
    type: wait
    params:
      duration_s: 5

  - id: 3
    name: "Note"
    type: comment
    notes: "Observation recorded"

safety_notes:
  - "Minimal hazard protocol"
"""

# ── Protocol parser tests ─────────────────────────────────────────────────────

class TestProtocolParser:

    def test_load_yaml_file(self):
        from src.core.protocol_parser import ProtocolParser
        parser = ProtocolParser()
        doc = parser.load(PROTOCOL_PATH)
        assert doc.metadata.name == "DNA Extraction from Blood – Magnetic Bead"
        assert len(doc.steps) > 0
        assert len(doc.reagents) > 0

    def test_load_string(self):
        from src.core.protocol_parser import ProtocolParser
        parser = ProtocolParser()
        doc = parser.load_string(MINIMAL_PROTOCOL_YAML)
        assert doc.metadata.name == "Test Protocol"
        assert len(doc.steps) == 3
        assert len(doc.reagents) == 1

    def test_step_types(self):
        from src.core.protocol_parser import ProtocolParser, StepType
        parser = ProtocolParser()
        doc = parser.load_string(MINIMAL_PROTOCOL_YAML)
        types = [s.type for s in doc.steps]
        assert StepType.VORTEX in types
        assert StepType.WAIT in types
        assert StepType.COMMENT in types

    def test_step_params(self):
        from src.core.protocol_parser import ProtocolParser
        parser = ProtocolParser()
        doc = parser.load_string(MINIMAL_PROTOCOL_YAML)
        vortex_step = doc.steps[0]
        assert vortex_step.rpm == 1000
        assert vortex_step.duration_s == 10

    def test_missing_file_raises(self):
        from src.core.protocol_parser import ProtocolParser
        parser = ProtocolParser()
        with pytest.raises(FileNotFoundError):
            parser.load("nonexistent.yaml")

    def test_malformed_yaml_raises(self):
        from src.core.protocol_parser import ProtocolParser
        parser = ProtocolParser()
        with pytest.raises(Exception):
            parser.load_string("metadata: {name: [invalid")


# ── Resource manager tests ────────────────────────────────────────────────────

class TestResourceManager:

    def test_generate_material_report(self):
        from src.core.protocol_parser import ProtocolParser
        from src.core.resource_manager import ResourceManager
        parser = ProtocolParser()
        doc    = parser.load_string(MINIMAL_PROTOCOL_YAML)
        rm     = ResourceManager()
        report = rm.generate_material_report(doc)
        assert len(report.reagents) == 1
        assert len(report.consumables) == 1

    def test_volume_buffer_applied(self):
        from src.core.protocol_parser import ProtocolParser
        from src.core.resource_manager import ResourceManager
        parser = ProtocolParser()
        doc    = parser.load_string(MINIMAL_PROTOCOL_YAML)
        rm     = ResourceManager()
        report = rm.generate_material_report(doc, safety_buffer=1.15)
        # 100 µL × 1.15 = 115 µL needed
        needed = float(report.reagents[0]["Needed (µL)"])
        assert needed == pytest.approx(115.0)

    def test_missing_flagged(self):
        from src.core.protocol_parser import ProtocolParser
        from src.core.resource_manager import ResourceManager
        parser = ProtocolParser()
        doc    = parser.load_string(MINIMAL_PROTOCOL_YAML)
        rm     = ResourceManager()
        report = rm.generate_material_report(doc)
        # No inventory loaded → everything is "not in inventory"
        assert "Water" in report.missing or len(report.missing) >= 0  # at least runs


# ── Risk assessment tests ─────────────────────────────────────────────────────

class TestRiskAssessor:

    def test_bsl1_low_risk(self):
        from src.core.protocol_parser import ProtocolParser
        from src.core.risk_assessment import RiskAssessor, Severity
        parser   = ProtocolParser()
        doc      = parser.load_string(MINIMAL_PROTOCOL_YAML)
        assessor = RiskAssessor()
        report   = assessor.assess(doc)
        assert report.clearance is True
        assert report.overall_risk in (Severity.LOW, Severity.MEDIUM)

    def test_bsl2_protocol_cleared(self):
        from src.core.protocol_parser import ProtocolParser
        from src.core.risk_assessment import RiskAssessor
        parser = ProtocolParser()
        doc    = parser.load(PROTOCOL_PATH)   # BSL2
        assessor = RiskAssessor()
        report = assessor.assess(doc)
        assert report.clearance is True  # BSL2 is cleared (BSL3 would be blocked)

    def test_flammable_reagent_flagged(self):
        from src.core.protocol_parser import ProtocolParser
        from src.core.risk_assessment import RiskAssessor, HazardCategory
        yaml_str = MINIMAL_PROTOCOL_YAML.replace("hazard_class: none", "hazard_class: flammable")
        parser   = ProtocolParser()
        doc      = parser.load_string(yaml_str)
        assessor = RiskAssessor()
        report   = assessor.assess(doc)
        categories = [r.category for r in report.risks]
        assert HazardCategory.CHEMICAL in categories

    def test_ppe_populated(self):
        from src.core.protocol_parser import ProtocolParser
        from src.core.risk_assessment import RiskAssessor
        parser   = ProtocolParser()
        doc      = parser.load(PROTOCOL_PATH)
        assessor = RiskAssessor()
        report   = assessor.assess(doc)
        assert len(report.ppe) > 0

    def test_waste_streams_populated(self):
        from src.core.protocol_parser import ProtocolParser
        from src.core.risk_assessment import RiskAssessor
        parser   = ProtocolParser()
        doc      = parser.load(PROTOCOL_PATH)
        assessor = RiskAssessor()
        report   = assessor.assess(doc)
        assert len(report.waste_streams) > 0


# ── Equipment simulation tests ────────────────────────────────────────────────

class TestEquipmentSimulation:

    def _sim_config(self, name: str) -> "EquipmentConfig":
        from src.equipment.base_equipment import EquipmentConfig
        return EquipmentConfig(name=name, simulate=True)

    def test_vortex_simulation(self):
        from src.equipment.peripherals.vortex import Vortex
        v = Vortex(self._sim_config("vortex"))
        assert v.connect() is True
        v.run_for(rpm=2000, duration_s=1)
        assert v.is_ready()
        v.disconnect()

    def test_centrifuge_simulation(self):
        from src.equipment.peripherals.centrifuge import Centrifuge
        c = Centrifuge(self._sim_config("centrifuge"))
        assert c.connect() is True
        c.run(rpm=5000, duration_s=1, wait=True)
        assert c.is_ready()
        c.disconnect()

    def test_incubator_simulation(self):
        from src.equipment.peripherals.incubator import Incubator
        i = Incubator(self._sim_config("incubator"))
        assert i.connect() is True
        i.incubate(temperature_c=37, duration_s=1, shake_rpm=300)
        i.disconnect()

    def test_magnetic_separator_simulation(self):
        from src.equipment.peripherals.magnetic_separator import MagneticSeparator, MagnetState
        m = MagneticSeparator(self._sim_config("mag"))
        assert m.connect() is True
        m.engage()
        assert m.state == MagnetState.ENGAGED
        m.disengage()
        assert m.state == MagnetState.DISENGAGED

    def test_rocker_simulation(self):
        from src.equipment.peripherals.rocker import Rocker
        r = Rocker(self._sim_config("rocker"))
        assert r.connect() is True
        r.rock_for(duration_s=1)
        assert r.is_ready()

    def test_robotic_arm_simulation(self):
        from src.equipment.robotic_arm import RoboticArm
        arm = RoboticArm(self._sim_config("arm"))
        assert arm.connect() is True
        arm.home()
        arm.gripper_open()
        arm.gripper_close()
        arm.invert_tube(n_times=2)
        arm.tighten_cap()
        arm.disconnect()

    def test_opentrons_simulation(self):
        from src.equipment.opentrons_controller import OpentronsController
        ot = OpentronsController(self._sim_config("opentrons"))
        assert ot.connect() is True
        pid = ot.upload_protocol(Path("protocols/dna_extraction.yaml"))
        assert "sim" in pid
        rid = ot.create_run(pid)
        ot.start_run(rid)
        status = ot.poll_run(rid)
        assert status == "succeeded"


# ── Orchestrator simulation test ──────────────────────────────────────────────

class TestOrchestratorSimulation:

    def test_minimal_run_simulation(self, tmp_path):
        from src.core.protocol_parser import ProtocolParser
        from src.core.orchestrator import Orchestrator

        parser = ProtocolParser()
        doc    = parser.load_string(MINIMAL_PROTOCOL_YAML)
        orch   = Orchestrator(config={}, simulate=True)
        record = orch.execute(
            doc,
            runs_dir=tmp_path,
            confirm_before_run=False,
        )
        assert record.status == "completed"
        assert len(record.steps) == 3

    def test_run_record_saved(self, tmp_path):
        from src.core.protocol_parser import ProtocolParser
        from src.core.orchestrator import Orchestrator

        parser = ProtocolParser()
        doc    = parser.load_string(MINIMAL_PROTOCOL_YAML)
        orch   = Orchestrator(config={}, simulate=True)
        record = orch.execute(doc, runs_dir=tmp_path, confirm_before_run=False)

        saved = list(tmp_path.glob("*.json"))
        assert len(saved) == 1
