"""
Resource Manager – tracks reagents, consumables, and inventory.

Responsibilities
─────────────────
• Compute the full material list from a protocol document.
• Check stock levels against inventory (optional DB or YAML inventory file).
• Track consumption during a run (decrement stock).
• Generate printable material and consumable lists as rich tables or Excel.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from loguru import logger
from rich.console import Console
from rich.table import Table

from .protocol_parser import ProtocolDocument, Reagent, Consumable


@dataclass
class StockItem:
    name: str
    quantity_available: float   # µL for liquids, count for items
    unit: str                   # "uL" | "mL" | "count"
    location: str = ""          # e.g. "Fridge A, shelf 2"


@dataclass
class MaterialReport:
    reagents:    List[Dict]
    consumables: List[Dict]
    missing:     List[str]      # items not found in inventory
    warnings:    List[str]


class ResourceManager:
    """
    Manages materials and consumables for experiment runs.

    inventory_path: optional path to a YAML/JSON file listing current stock.
    """

    def __init__(self, inventory_path: Optional[Path] = None) -> None:
        self._inventory: Dict[str, StockItem] = {}
        self._console = Console()
        if inventory_path and inventory_path.exists():
            self._load_inventory(inventory_path)

    # ── Inventory ─────────────────────────────────────────────────────────────

    def _load_inventory(self, path: Path) -> None:
        import yaml
        with open(path) as fh:
            raw = yaml.safe_load(fh) or {}
        for item in raw.get("inventory", []):
            si = StockItem(**item)
            self._inventory[si.name.lower()] = si
        logger.info(f"Inventory loaded: {len(self._inventory)} items from {path}")

    def add_stock(self, name: str, quantity: float, unit: str = "uL", location: str = "") -> None:
        self._inventory[name.lower()] = StockItem(name, quantity, unit, location)

    # ── Material list generation ──────────────────────────────────────────────

    def generate_material_report(
        self,
        doc: ProtocolDocument,
        safety_buffer: float = 1.15,   # 15% overage buffer
    ) -> MaterialReport:
        """
        Derive material requirements from a protocol document.
        Checks against inventory if available.
        """
        reagent_rows = []
        consumable_rows = []
        missing = []
        warnings = []

        # ── Reagents ──────────────────────────────────────────────────────────
        for r in doc.reagents:
            needed_ul = (r.volume_ul or 0) * safety_buffer
            in_stock  = self._inventory.get(r.name.lower())

            status = "OK"
            if in_stock is None:
                status = "NOT IN INVENTORY"
                missing.append(r.name)
            elif in_stock.quantity_available < needed_ul:
                status = f"LOW (have {in_stock.quantity_available:.0f} µL)"
                warnings.append(f"Low stock: {r.name}")

            reagent_rows.append({
                "Name":          r.name,
                "Needed (µL)":   f"{needed_ul:.1f}" if r.volume_ul else "–",
                "Concentration": r.concentration or "–",
                "Storage (°C)":  r.storage_temp_c,
                "Hazard":        r.hazard_class,
                "CAS":           r.cas_number or "–",
                "Stock Status":  status,
            })

        # ── Consumables ───────────────────────────────────────────────────────
        for c in doc.consumables:
            in_stock = self._inventory.get(c.name.lower())

            status = "OK"
            if in_stock is None:
                status = "NOT IN INVENTORY"
                missing.append(c.name)
            elif in_stock.quantity_available < c.quantity:
                status = f"LOW (have {int(in_stock.quantity_available)})"
                warnings.append(f"Low stock: {c.name}")

            consumable_rows.append({
                "Name":        c.name,
                "Qty Needed":  c.quantity,
                "Supplier":    c.supplier or "–",
                "Cat. No.":    c.catalog_number or "–",
                "Notes":       c.notes or "–",
                "Stock Status": status,
            })

        return MaterialReport(
            reagents=reagent_rows,
            consumables=consumable_rows,
            missing=list(set(missing)),
            warnings=warnings,
        )

    # ── Display ───────────────────────────────────────────────────────────────

    def print_material_report(self, doc: ProtocolDocument) -> MaterialReport:
        report = self.generate_material_report(doc)

        self._console.print(f"\n[bold cyan]═══ Material List: {doc.metadata.name} ═══[/bold cyan]\n")

        # Reagents table
        if report.reagents:
            rtable = Table(title="Reagents / Chemicals", show_lines=True)
            for col in report.reagents[0]:
                rtable.add_column(col, style="white")
            for row in report.reagents:
                style = "red" if "NOT IN" in row["Stock Status"] else (
                    "yellow" if "LOW" in row["Stock Status"] else "green"
                )
                rtable.add_row(*[str(v) for v in row.values()], style=style)
            self._console.print(rtable)

        # Consumables table
        if report.consumables:
            ctable = Table(title="Consumables", show_lines=True)
            for col in report.consumables[0]:
                ctable.add_column(col, style="white")
            for row in report.consumables:
                style = "red" if "NOT IN" in row["Stock Status"] else "default"
                ctable.add_row(*[str(v) for v in row.values()], style=style)
            self._console.print(ctable)

        if report.missing:
            self._console.print(f"\n[red bold]⚠ Missing from inventory:[/red bold] {', '.join(report.missing)}")
        if report.warnings:
            for w in report.warnings:
                self._console.print(f"[yellow]⚠ {w}[/yellow]")

        return report

    # ── Excel export ──────────────────────────────────────────────────────────

    def export_excel(
        self,
        doc: ProtocolDocument,
        output_path: Path,
    ) -> None:
        report = self.generate_material_report(doc)
        with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
            if report.reagents:
                pd.DataFrame(report.reagents).to_excel(
                    writer, sheet_name="Reagents", index=False
                )
            if report.consumables:
                pd.DataFrame(report.consumables).to_excel(
                    writer, sheet_name="Consumables", index=False
                )
            # Protocol steps
            steps_data = [
                {"ID": s.id, "Name": s.name, "Type": s.type, "Params": json.dumps(s.params)}
                for s in doc.steps
            ]
            pd.DataFrame(steps_data).to_excel(writer, sheet_name="Steps", index=False)

        logger.info(f"Material report exported to {output_path}")

    # ── Stock deduction (during run) ──────────────────────────────────────────

    def consume(self, reagent_name: str, volume_ul: float) -> None:
        key = reagent_name.lower()
        if key in self._inventory:
            self._inventory[key].quantity_available = max(
                0, self._inventory[key].quantity_available - volume_ul
            )
            logger.debug(f"Consumed {volume_ul}µL of {reagent_name}")
