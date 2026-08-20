"""
Protocol parser – reads YAML protocol files and returns a validated
ProtocolDocument with typed steps, materials, and consumables.

Protocol YAML format
─────────────────────
See protocols/dna_extraction.yaml for a full annotated example.

Top-level keys
  metadata     : name, version, author, description, safety_level
  materials    : list of reagent / labware definitions
  consumables  : tubes, tips, plates, etc.
  equipment    : required instruments
  steps        : ordered list of experiment steps
  safety_notes : free-text safety warnings

Step types (step.type)
  pipette           – Opentrons liquid handling
  vortex            – vortex mixer
  centrifuge        – centrifugation
  incubate          – temperature incubation (with optional shaking)
  magnetic_sep      – magnetic bead separation
  rock              – slow tube rocker
  tighten_cap       – cap tightening (robotic arm, force-controlled)
  loosen_cap        – cap removal
  invert_tube       – tube inversion mixing
  transfer_tube     – arm moves tube between stations
  temperature_set   – set temperature on incubator/thermocycler
  wait              – timed pause
  comment           – annotation only (no hardware action)
  manual            – pause for human intervention
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from loguru import logger


# ── Step type enum ────────────────────────────────────────────────────────────

class StepType(str, Enum):
    PIPETTE         = "pipette"
    VORTEX          = "vortex"
    CENTRIFUGE      = "centrifuge"
    INCUBATE        = "incubate"
    MAGNETIC_SEP    = "magnetic_sep"
    ROCK            = "rock"
    TIGHTEN_CAP     = "tighten_cap"
    LOOSEN_CAP      = "loosen_cap"
    INVERT_TUBE     = "invert_tube"
    TRANSFER_TUBE   = "transfer_tube"
    TEMPERATURE_SET = "temperature_set"
    WAIT            = "wait"
    COMMENT         = "comment"
    MANUAL          = "manual"


class SafetyLevel(str, Enum):
    BSL1 = "BSL1"
    BSL2 = "BSL2"
    BSL3 = "BSL3"
    CHEM = "CHEM"   # Chemical hazard (no biological)


# ── Material models ───────────────────────────────────────────────────────────

class Reagent(BaseModel):
    name: str
    volume_ul: Optional[float]  = None   # total volume needed (µL)
    concentration: Optional[str] = None  # e.g. "1x", "10 mM"
    storage_temp_c: float       = 4.0
    hazard_class: str           = "none"  # GHS: none | flammable | toxic | corrosive | biohazard
    cas_number: Optional[str]   = None
    sds_url: Optional[str]      = None
    notes: Optional[str]        = None


class Consumable(BaseModel):
    name: str
    quantity: int               = 1
    supplier: Optional[str]     = None
    catalog_number: Optional[str] = None
    notes: Optional[str]        = None


class EquipmentRequirement(BaseModel):
    name: str                   # e.g. "opentrons", "centrifuge"
    model: Optional[str]        = None
    required: bool              = True


# ── Step models ───────────────────────────────────────────────────────────────

class ProtocolStep(BaseModel):
    id: int
    name: str
    type: StepType
    params: Dict[str, Any]      = Field(default_factory=dict)
    notes: Optional[str]        = None

    # --- Validated parameter accessors (type-specific) -----------------------

    # pipette
    @property
    def source(self) -> Optional[str]:
        return self.params.get("source")

    @property
    def destination(self) -> Optional[str]:
        return self.params.get("destination")

    @property
    def volume_ul(self) -> Optional[float]:
        return self.params.get("volume_ul") or self.params.get("volume")

    @property
    def mix_reps(self) -> int:
        return int(self.params.get("mix_repetitions", 0))

    # vortex / centrifuge / rocker
    @property
    def rpm(self) -> Optional[int]:
        return self.params.get("rpm") or self.params.get("speed_rpm")

    @property
    def duration_s(self) -> Optional[float]:
        return self.params.get("duration_s") or self.params.get("duration")

    # incubate / temperature
    @property
    def temperature_c(self) -> Optional[float]:
        return self.params.get("temperature_c") or self.params.get("temperature")

    # centrifuge
    @property
    def centrifuge_temp(self) -> Optional[int]:
        return self.params.get("temp_c") or self.params.get("temperature_c")

    # tube ops
    @property
    def tube_label(self) -> Optional[str]:
        return self.params.get("tube_label")

    @property
    def n_inversions(self) -> int:
        return int(self.params.get("n_times", 10))

    # magnetic sep
    @property
    def separation_time_s(self) -> float:
        return float(self.params.get("duration_s", 120))


# ── Top-level protocol document ───────────────────────────────────────────────

class ProtocolMetadata(BaseModel):
    name: str
    version: str                = "1.0"
    author: str                 = "Unknown"
    description: str            = ""
    safety_level: SafetyLevel   = SafetyLevel.BSL1
    created_at: Optional[str]   = None


class ProtocolDocument(BaseModel):
    metadata:     ProtocolMetadata
    reagents:     List[Reagent]             = Field(default_factory=list)
    consumables:  List[Consumable]          = Field(default_factory=list)
    equipment:    List[EquipmentRequirement] = Field(default_factory=list)
    steps:        List[ProtocolStep]        = Field(default_factory=list)
    safety_notes: List[str]                 = Field(default_factory=list)

    @property
    def required_equipment_names(self) -> List[str]:
        return [e.name for e in self.equipment if e.required]


# ── Parser ────────────────────────────────────────────────────────────────────

class ProtocolParser:
    """
    Loads and validates a YAML protocol file into a ProtocolDocument.

    Usage:
        parser = ProtocolParser()
        doc = parser.load("protocols/dna_extraction.yaml")
    """

    def load(self, path: Union[str, Path]) -> ProtocolDocument:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Protocol not found: {path}")

        logger.info(f"Loading protocol: {path.name}")
        with open(path, "r") as fh:
            raw = yaml.safe_load(fh)

        return self._parse(raw, source=str(path))

    def load_string(self, yaml_str: str) -> ProtocolDocument:
        """Parse a YAML string directly (useful for LLM-generated protocols)."""
        raw = yaml.safe_load(yaml_str)
        return self._parse(raw, source="<string>")

    def _parse(self, raw: dict, source: str) -> ProtocolDocument:
        try:
            if not isinstance(raw, dict):
                raise ValueError(
                    f"Expected a YAML mapping at the top level, got {type(raw).__name__}. "
                    "Is the protocol file empty or malformed?"
                )
            meta_raw = raw.get("metadata") or raw.get("protocol", {})
            if not isinstance(meta_raw, dict):
                meta_raw = {"name": str(meta_raw)}

            metadata = ProtocolMetadata(**meta_raw)

            # Materials – support both flat list and nested {reagents: [...]} dict
            mat_section = raw.get("materials", {})
            if isinstance(mat_section, list):
                reagents_raw = mat_section
            elif isinstance(mat_section, dict):
                reagents_raw = mat_section.get("reagents") or []
            else:
                reagents_raw = []
            reagents = [Reagent(**r) for r in reagents_raw]

            # Consumables
            consumables = [
                Consumable(**c)
                for c in raw.get("consumables", [])
            ]

            # Equipment
            equipment = []
            for e in raw.get("equipment", []):
                if isinstance(e, str):
                    equipment.append(EquipmentRequirement(name=e))
                elif isinstance(e, dict):
                    equipment.append(EquipmentRequirement(**e))

            # Steps
            steps = []
            for s in raw.get("steps", []):
                steps.append(ProtocolStep(**s))

            step_ids = [s.id for s in steps]
            if len(step_ids) != len(set(step_ids)):
                from collections import Counter
                dupes = sorted({sid for sid, cnt in Counter(step_ids).items() if cnt > 1})
                raise ValueError(f"Protocol has duplicate step IDs: {dupes}")

            safety_notes = raw.get("safety_notes", [])

            doc = ProtocolDocument(
                metadata=metadata,
                reagents=reagents,
                consumables=consumables,
                equipment=equipment,
                steps=steps,
                safety_notes=safety_notes,
            )
            logger.info(
                f"Protocol '{metadata.name}' loaded: "
                f"{len(steps)} steps, {len(reagents)} reagents, "
                f"{len(consumables)} consumables"
            )
            return doc

        except Exception as exc:
            raise ValueError(f"Protocol parse error in {source}: {exc}") from exc

    def validate_against_hardware(
        self,
        doc: ProtocolDocument,
        available_equipment: List[str],
    ) -> List[str]:
        """
        Return a list of missing equipment names (empty = all present).
        """
        required  = set(doc.required_equipment_names)
        available = set(available_equipment)
        missing   = required - available
        if missing:
            logger.warning(f"Missing equipment: {missing}")
        return sorted(missing)
