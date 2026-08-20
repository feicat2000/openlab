"""
Risk Assessment Engine.

Generates a structured risk report from a ProtocolDocument covering:
  • Chemical hazards (GHS classification, flammability, toxicity)
  • Biological hazards (BSL level, containment requirements)
  • Mechanical hazards (centrifuge imbalance, arm collision zones)
  • Thermal hazards (high-temp incubation, cryo reagents)
  • PPE requirements derived from hazards
  • Waste disposal classification

Output formats: rich console table, JSON, or PDF (via Jinja2 + WeasyPrint).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from jinja2 import Environment, FileSystemLoader
from loguru import logger
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .protocol_parser import ProtocolDocument, SafetyLevel


# ── Risk severity ─────────────────────────────────────────────────────────────

class Severity(str, Enum):
    LOW      = "LOW"
    MEDIUM   = "MEDIUM"
    HIGH     = "HIGH"
    CRITICAL = "CRITICAL"


class HazardCategory(str, Enum):
    CHEMICAL    = "Chemical"
    BIOLOGICAL  = "Biological"
    MECHANICAL  = "Mechanical"
    THERMAL     = "Thermal"
    ELECTRICAL  = "Electrical"
    ERGONOMIC   = "Ergonomic"


@dataclass
class RiskItem:
    category:      HazardCategory
    source:        str              # reagent/equipment name
    description:   str
    severity:      Severity
    probability:   str              # "low" | "medium" | "high"
    mitigation:    str
    residual_risk: Severity


@dataclass
class PPERequirement:
    item: str
    required: bool
    reason: str


@dataclass
class WasteStream:
    label: str
    classification: str             # e.g. "biohazardous", "chemical", "regular"
    disposal_route: str


@dataclass
class RiskReport:
    protocol_name: str
    safety_level:  str
    risks:         List[RiskItem]       = field(default_factory=list)
    ppe:           List[PPERequirement] = field(default_factory=list)
    waste_streams: List[WasteStream]    = field(default_factory=list)
    overall_risk:  Severity             = Severity.LOW
    clearance:     bool                 = True   # False = must be reviewed before run
    notes:         List[str]            = field(default_factory=list)


# ── GHS hazard knowledge base ─────────────────────────────────────────────────

GHS_PROFILES: Dict[str, dict] = {
    "none": {
        "severity": Severity.LOW,
        "ppe": [],
        "waste": "regular",
    },
    "flammable": {
        "severity": Severity.MEDIUM,
        "ppe": ["Lab coat", "Chemical-resistant gloves", "Eye protection"],
        "waste": "chemical_flammable",
        "mitigation": "Keep away from ignition sources. Store in flammable cabinet.",
    },
    "toxic": {
        "severity": Severity.HIGH,
        "ppe": ["Lab coat", "Nitrile gloves (double)", "Eye protection", "Fume hood"],
        "waste": "chemical_toxic",
        "mitigation": "Work in certified fume hood. Neutralise before disposal.",
    },
    "corrosive": {
        "severity": Severity.HIGH,
        "ppe": ["Lab coat", "Acid-resistant gloves", "Face shield", "Apron"],
        "waste": "chemical_corrosive",
        "mitigation": "Neutralise spills with bicarbonate. Emergency shower nearby.",
    },
    "biohazard": {
        "severity": Severity.HIGH,
        "ppe": ["Lab coat", "Nitrile gloves", "Eye protection", "Face mask"],
        "waste": "biohazardous_autoclave",
        "mitigation": "Contain within BSL cabinet. Autoclave all liquid waste.",
    },
    "irritant": {
        "severity": Severity.LOW,
        "ppe": ["Lab coat", "Nitrile gloves", "Eye protection"],
        "waste": "chemical",
        "mitigation": "Avoid contact with skin/eyes. Ventilated area.",
    },
    "oxidiser": {
        "severity": Severity.MEDIUM,
        "ppe": ["Lab coat", "Gloves", "Eye protection"],
        "waste": "chemical_oxidiser",
        "mitigation": "Keep away from organic materials and reducing agents.",
    },
    "cryo": {
        "severity": Severity.MEDIUM,
        "ppe": ["Cryogenic gloves", "Face shield", "Lab coat"],
        "waste": "regular",
        "mitigation": "Work in ventilated area. Avoid skin contact. Secure Dewar.",
    },
}

BSL_REQUIREMENTS: Dict[SafetyLevel, dict] = {
    SafetyLevel.BSL1: {
        "severity": Severity.LOW,
        "containment": "Standard microbiological practices",
        "ppe_extra": [],
        "waste": "biohazardous_autoclave",
    },
    SafetyLevel.BSL2: {
        "severity": Severity.MEDIUM,
        "containment": "BSC Class II required for aerosol-generating procedures",
        "ppe_extra": ["Face shield for splash risk", "Biosafety cabinet use"],
        "waste": "biohazardous_autoclave",
    },
    SafetyLevel.BSL3: {
        "severity": Severity.CRITICAL,
        "containment": "Sealed, HEPA-filtered lab with negative pressure",
        "ppe_extra": ["Powered air-purifying respirator (PAPR)", "Double gloves", "Sealed suit"],
        "waste": "biohazardous_incineration",
        "clearance": False,
    },
    SafetyLevel.CHEM: {
        "severity": Severity.MEDIUM,
        "containment": "Chemical fume hood, spill kit",
        "ppe_extra": [],
        "waste": "chemical",
    },
}


# ── Risk Assessment Engine ────────────────────────────────────────────────────

class RiskAssessor:
    """
    Generates a RiskReport from a ProtocolDocument.

    Usage:
        assessor = RiskAssessor()
        report   = assessor.assess(doc)
        assessor.print_report(report)
    """

    def __init__(self) -> None:
        self._console = Console()

    def assess(self, doc: ProtocolDocument) -> RiskReport:
        logger.info(f"Running risk assessment for '{doc.metadata.name}'")
        report = RiskReport(
            protocol_name=doc.metadata.name,
            safety_level=doc.metadata.safety_level,
        )

        # ── Chemical hazards ──────────────────────────────────────────────────
        ppe_set: set[str] = {"Lab coat", "Nitrile gloves", "Eye protection"}
        waste_types: set[str] = set()
        overall_severities: list[Severity] = []

        for reagent in doc.reagents:
            haz = reagent.hazard_class.lower()
            profile = GHS_PROFILES.get(haz, GHS_PROFILES["none"])

            if haz != "none":
                risk = RiskItem(
                    category=HazardCategory.CHEMICAL,
                    source=reagent.name,
                    description=f"GHS hazard class: {haz}",
                    severity=profile["severity"],
                    probability="medium",
                    mitigation=profile.get("mitigation",
                        "Follow standard chemical handling procedures."),
                    residual_risk=Severity.LOW,
                )
                report.risks.append(risk)
                overall_severities.append(profile["severity"])
                ppe_set.update(profile.get("ppe", []))
                waste_types.add(profile.get("waste", "chemical"))

        # ── Biological hazard ─────────────────────────────────────────────────
        bsl_profile = BSL_REQUIREMENTS.get(doc.metadata.safety_level)
        if bsl_profile and doc.metadata.safety_level != SafetyLevel.CHEM:
            report.risks.append(RiskItem(
                category=HazardCategory.BIOLOGICAL,
                source=f"Sample/organism ({doc.metadata.safety_level})",
                description=bsl_profile["containment"],
                severity=bsl_profile["severity"],
                probability="medium",
                mitigation=bsl_profile["containment"],
                residual_risk=Severity.LOW,
            ))
            overall_severities.append(bsl_profile["severity"])
            ppe_set.update(bsl_profile.get("ppe_extra", []))
            waste_types.add(bsl_profile.get("waste", "biohazardous_autoclave"))
            if not bsl_profile.get("clearance", True):
                report.clearance = False
                report.notes.append(
                    "BSL3 protocol: BIOSAFETY OFFICER APPROVAL REQUIRED before run."
                )

        # ── Mechanical hazards ────────────────────────────────────────────────
        req_names = {e.name for e in doc.equipment}

        if "centrifuge" in req_names:
            report.risks.append(RiskItem(
                category=HazardCategory.MECHANICAL,
                source="Centrifuge",
                description="Rotor imbalance or lid failure at high RPM.",
                severity=Severity.HIGH,
                probability="low",
                mitigation=(
                    "Load tubes in balanced pairs/opposite positions. "
                    "Never open lid while spinning. "
                    "Robotic arm must not approach while centrifuge is running."
                ),
                residual_risk=Severity.LOW,
            ))
            overall_severities.append(Severity.MEDIUM)

        if "robotic_arm" in req_names or "ur_arm" in req_names:
            report.risks.append(RiskItem(
                category=HazardCategory.MECHANICAL,
                source="Collaborative Robot Arm",
                description="Collision with personnel or equipment during motion.",
                severity=Severity.MEDIUM,
                probability="low",
                mitigation=(
                    "Maintain safety zone (ISO/TS 15066). "
                    "Use safety-rated monitored stop. "
                    "Speed limit ≤ 250 mm/s in shared workspace."
                ),
                residual_risk=Severity.LOW,
            ))
            overall_severities.append(Severity.LOW)

        # ── Thermal hazards ───────────────────────────────────────────────────
        for step in doc.steps:
            temp = step.temperature_c
            if temp and temp > 60:
                report.risks.append(RiskItem(
                    category=HazardCategory.THERMAL,
                    source=f"Step {step.id}: {step.name}",
                    description=f"High temperature incubation at {temp}°C.",
                    severity=Severity.MEDIUM,
                    probability="low",
                    mitigation="Heat-resistant gloves when handling. Allow cooling before opening.",
                    residual_risk=Severity.LOW,
                ))
                overall_severities.append(Severity.LOW)
                ppe_set.add("Heat-resistant gloves (for post-incubation handling)")

        # ── PPE list ──────────────────────────────────────────────────────────
        report.ppe = [
            PPERequirement(item=p, required=True, reason="Derived from protocol hazards")
            for p in sorted(ppe_set)
        ]

        # ── Waste streams ─────────────────────────────────────────────────────
        waste_map = {
            "regular":                  ("Regular lab waste",    "Municipal waste bin"),
            "chemical":                 ("Chemical waste",       "Licensed chemical waste contractor"),
            "chemical_flammable":       ("Flammable waste",      "Flammable waste container → contractor"),
            "chemical_toxic":           ("Toxic chemical waste", "Licensed hazardous waste contractor"),
            "chemical_corrosive":       ("Corrosive waste",      "Neutralise → licensed contractor"),
            "chemical_oxidiser":        ("Oxidiser waste",       "Licensed chemical waste contractor"),
            "biohazardous_autoclave":   ("Biohazardous waste",   "Autoclave 121°C 20 min → biohaz bin"),
            "biohazardous_incineration":("BSL3 waste",           "Incineration – specialist contractor"),
        }
        for wt in waste_types:
            label, route = waste_map.get(wt, ("Unknown", "Consult EHS"))
            report.waste_streams.append(WasteStream(
                label=label, classification=wt, disposal_route=route
            ))

        # ── Overall risk ──────────────────────────────────────────────────────
        SEVERITY_ORDER = [Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
        if overall_severities:
            max_sev = max(overall_severities, key=lambda s: SEVERITY_ORDER.index(s))
            report.overall_risk = max_sev
        else:
            report.overall_risk = Severity.LOW

        logger.info(f"Risk assessment complete – overall: {report.overall_risk}")
        return report

    # ── Display ───────────────────────────────────────────────────────────────

    def print_report(self, report: RiskReport) -> None:
        COLOUR = {
            Severity.LOW: "green", Severity.MEDIUM: "yellow",
            Severity.HIGH: "red",  Severity.CRITICAL: "bold red",
        }
        c = self._console

        c.print(f"\n[bold cyan]═══ Risk Assessment: {report.protocol_name} ═══[/bold cyan]\n")
        c.print(f"  Safety Level  : [bold]{report.safety_level}[/bold]")
        c.print(
            f"  Overall Risk  : "
            f"[{COLOUR[report.overall_risk]}]{report.overall_risk}[/{COLOUR[report.overall_risk]}]"
        )
        c.print(
            f"  Run Clearance : "
            + ("[green]APPROVED[/green]" if report.clearance else "[bold red]BLOCKED – BSO APPROVAL NEEDED[/bold red]")
        )

        if report.risks:
            tbl = Table(title="\nHazards", show_lines=True)
            for col, style in [
                ("Category","cyan"),("Source","white"),("Description","white"),
                ("Severity","white"),("Probability","white"),("Mitigation","white"),
                ("Residual","white"),
            ]:
                tbl.add_column(col, style=style)
            for r in report.risks:
                tbl.add_row(
                    r.category, r.source, r.description,
                    f"[{COLOUR[r.severity]}]{r.severity}[/{COLOUR[r.severity]}]",
                    r.probability, r.mitigation,
                    f"[{COLOUR[r.residual_risk]}]{r.residual_risk}[/{COLOUR[r.residual_risk]}]",
                )
            c.print(tbl)

        if report.ppe:
            c.print("\n[bold]Required PPE:[/bold]")
            for p in report.ppe:
                c.print(f"  [green]✓[/green]  {p.item}")

        if report.waste_streams:
            c.print("\n[bold]Waste Disposal:[/bold]")
            for w in report.waste_streams:
                c.print(f"  • [yellow]{w.label}[/yellow] → {w.disposal_route}")

        for note in report.notes:
            c.print(Panel(f"[bold red]{note}[/bold red]", title="⚠ WARNING"))

    # ── JSON export ───────────────────────────────────────────────────────────

    def export_json(self, report: RiskReport, path: Path) -> None:
        import dataclasses
        data = dataclasses.asdict(report)
        with open(path, "w") as fh:
            json.dump(data, fh, indent=2, default=str)
        logger.info(f"Risk report exported to {path}")
