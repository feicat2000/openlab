"""
Stock Manager – tracks reagent and consumable inventory, records consumption,
and generates shopping lists from protocol requirements.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from loguru import logger
from rich.console import Console
from rich.table import Table


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class StockItem:
    name: str
    quantity: float              # µL for liquids, count for consumables
    unit: str                    # "uL" | "count"
    location: str = ""
    lot_number: str = ""
    expiry_date: Optional[str] = None   # ISO format "YYYY-MM-DD"
    reorder_threshold: float = 0.0
    supplier: str = ""
    catalog_number: str = ""
    cost_per_unit: float = 0.0   # per µL or per unit


@dataclass
class ConsumptionRecord:
    reagent: str
    amount: float
    unit: str
    run_id: str
    timestamp: float


# ── StockManager ──────────────────────────────────────────────────────────────

class StockManager:
    """
    Inventory management for reagents and consumables.

    Usage::

        sm = StockManager()
        sm.load(Path("inventory.yaml"))
        sm.consume("Ethanol", 500, run_id="run_001")
        sm.check_low_stock()
        sm.save(Path("inventory.yaml"))
    """

    def __init__(self, inventory_path: Optional[Path] = None) -> None:
        self._stock: Dict[str, StockItem] = {}
        self._history: List[ConsumptionRecord] = []
        self._console = Console()
        if inventory_path:
            self.load(Path(inventory_path))

    # ── Persistence ───────────────────────────────────────────────────────────

    def load(self, path: Path) -> None:
        path = Path(path)
        if not path.exists():
            logger.warning(f"Inventory file not found: {path}")
            return
        suffix = path.suffix.lower()
        with open(path) as fh:
            if suffix in (".yaml", ".yml"):
                data = yaml.safe_load(fh) or {}
            else:
                data = json.load(fh)

        self._stock.clear()
        self._history.clear()

        for item_data in data.get("stock", []):
            item = StockItem(**item_data)
            self._stock[item.name] = item

        for rec_data in data.get("history", []):
            self._history.append(ConsumptionRecord(**rec_data))

        logger.info(f"Inventory loaded: {len(self._stock)} items from {path}")

    def save(self, path: Path) -> None:
        path = Path(path)
        data = {
            "stock": [asdict(item) for item in self._stock.values()],
            "history": [asdict(r) for r in self._history],
        }
        suffix = path.suffix.lower()
        with open(path, "w") as fh:
            if suffix in (".yaml", ".yml"):
                yaml.dump(data, fh, default_flow_style=False)
            else:
                json.dump(data, fh, indent=2)
        logger.info(f"Inventory saved: {path}")

    # ── Stock operations ──────────────────────────────────────────────────────

    def add_stock(
        self,
        name: str,
        quantity: float,
        unit: str,
        location: str = "",
        lot_number: str = "",
        expiry_date: Optional[str] = None,
        reorder_threshold: float = 0.0,
        supplier: str = "",
        catalog_number: str = "",
        cost_per_unit: float = 0.0,
    ) -> None:
        if name in self._stock:
            self._stock[name].quantity += quantity
            logger.info(f"Stock added: {name} +{quantity}{unit} → total {self._stock[name].quantity}{unit}")
        else:
            self._stock[name] = StockItem(
                name=name,
                quantity=quantity,
                unit=unit,
                location=location,
                lot_number=lot_number,
                expiry_date=expiry_date,
                reorder_threshold=reorder_threshold,
                supplier=supplier,
                catalog_number=catalog_number,
                cost_per_unit=cost_per_unit,
            )
            logger.info(f"New stock item: {name} ({quantity}{unit})")

    def consume(
        self,
        name: str,
        amount: float,
        unit: str = "",
        run_id: str = "manual",
    ) -> None:
        if name not in self._stock:
            logger.warning(f"consume(): '{name}' not found in inventory – adding with 0 quantity")
            self._stock[name] = StockItem(name=name, quantity=0.0, unit=unit or "uL")

        item = self._stock[name]
        if item.quantity < amount:
            logger.warning(
                f"Insufficient stock for '{name}': need {amount}, have {item.quantity} – "
                "proceeding with negative balance"
            )
        item.quantity -= amount

        rec = ConsumptionRecord(
            reagent=name,
            amount=amount,
            unit=unit or item.unit,
            run_id=run_id,
            timestamp=time.time(),
        )
        self._history.append(rec)
        logger.info(f"Consumed {amount} {unit or item.unit} of '{name}' (run={run_id})")

    # ── Queries ───────────────────────────────────────────────────────────────

    def check_low_stock(self) -> List[StockItem]:
        """Return items whose quantity is at or below the reorder threshold."""
        return [
            item for item in self._stock.values()
            if item.quantity <= item.reorder_threshold
        ]

    def generate_shopping_list(
        self,
        doc,   # ProtocolDocument
        safety_buffer: float = 1.15,
    ) -> List[Dict[str, Any]]:
        """
        Compare protocol material requirements against current stock.
        Returns a list of dicts for items that need ordering.
        """
        shopping: List[Dict[str, Any]] = []
        for reagent in doc.reagents:
            needed = (reagent.volume_ul or 0.0) * safety_buffer
            in_stock = self._stock.get(reagent.name)
            available = in_stock.quantity if in_stock else 0.0
            shortfall = needed - available
            if shortfall > 0:
                shopping.append({
                    "name": reagent.name,
                    "needed_ul": round(needed, 1),
                    "in_stock_ul": round(available, 1),
                    "shortfall_ul": round(shortfall, 1),
                    "supplier": in_stock.supplier if in_stock else "",
                    "catalog_number": in_stock.catalog_number if in_stock else "",
                })
        return shopping

    def consumption_history(
        self,
        name: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> List[ConsumptionRecord]:
        records = self._history
        if name:
            records = [r for r in records if r.reagent == name]
        if run_id:
            records = [r for r in records if r.run_id == run_id]
        return records

    # ── Reporting ─────────────────────────────────────────────────────────────

    def print_stock_report(self, low_only: bool = False) -> None:
        items = list(self._stock.values())
        if low_only:
            items = self.check_low_stock()

        tbl = Table(title="Stock Inventory" + (" (Low Stock)" if low_only else ""))
        tbl.add_column("Reagent", style="cyan")
        tbl.add_column("Quantity", justify="right")
        tbl.add_column("Unit")
        tbl.add_column("Location")
        tbl.add_column("Expires")
        tbl.add_column("Reorder At", justify="right")

        for item in sorted(items, key=lambda i: i.name):
            qty_style = "red" if item.quantity <= item.reorder_threshold else "green"
            tbl.add_row(
                item.name,
                f"[{qty_style}]{item.quantity:.1f}[/{qty_style}]",
                item.unit,
                item.location or "-",
                item.expiry_date or "-",
                str(item.reorder_threshold),
            )
        self._console.print(tbl)

    def export_excel(self, path: Path) -> None:
        import pandas as pd  # type: ignore
        path = Path(path)
        stock_rows = [asdict(item) for item in self._stock.values()]
        history_rows = [asdict(r) for r in self._history]
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            pd.DataFrame(stock_rows).to_excel(writer, sheet_name="Stock", index=False)
            pd.DataFrame(history_rows).to_excel(writer, sheet_name="History", index=False)
        logger.info(f"Stock Excel exported: {path}")

    # ── Convenience ───────────────────────────────────────────────────────────

    def get_item(self, name: str) -> Optional[StockItem]:
        return self._stock.get(name)

    @property
    def all_items(self) -> List[StockItem]:
        return list(self._stock.values())
