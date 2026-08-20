"""
Tests for StockManager.
"""
from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from src.inventory.stock_manager import StockManager, StockItem, ConsumptionRecord


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmpdir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def sm():
    return StockManager()


# ── Minimal ProtocolDocument stub ─────────────────────────────────────────────

@dataclass
class _Reagent:
    name: str
    volume_ul: float = 500.0


@dataclass
class _Doc:
    reagents: List[_Reagent] = field(default_factory=list)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestAddStock:
    def test_add_new_item(self, sm):
        sm.add_stock("Ethanol", 10000, "uL")
        item = sm.get_item("Ethanol")
        assert item is not None
        assert item.quantity == 10000
        assert item.unit == "uL"

    def test_add_increments_existing(self, sm):
        sm.add_stock("Ethanol", 10000, "uL")
        sm.add_stock("Ethanol", 5000, "uL")
        assert sm.get_item("Ethanol").quantity == 15000

    def test_add_with_metadata(self, sm):
        sm.add_stock(
            "Tris-HCl", 5000, "uL",
            location="Fridge A", lot_number="LOT123",
            expiry_date="2027-01-01", reorder_threshold=1000,
            supplier="Sigma", catalog_number="T-7777",
            cost_per_unit=0.001,
        )
        item = sm.get_item("Tris-HCl")
        assert item.location == "Fridge A"
        assert item.lot_number == "LOT123"
        assert item.supplier == "Sigma"


class TestConsume:
    def test_consume_decreases_quantity(self, sm):
        sm.add_stock("Ethanol", 10000, "uL")
        sm.consume("Ethanol", 500, "uL", run_id="run_001")
        assert sm.get_item("Ethanol").quantity == 9500

    def test_consume_records_history(self, sm):
        sm.add_stock("Ethanol", 10000, "uL")
        sm.consume("Ethanol", 500, "uL", run_id="run_001")
        history = sm.consumption_history(name="Ethanol")
        assert len(history) == 1
        assert history[0].amount == 500
        assert history[0].run_id == "run_001"

    def test_consume_insufficient_stock_warns_not_crash(self, sm, caplog):
        sm.add_stock("Water", 100, "uL")
        sm.consume("Water", 500, "uL")  # should warn, not raise
        assert sm.get_item("Water").quantity == -400

    def test_consume_unknown_reagent_creates_entry(self, sm):
        sm.consume("Unknown", 100, "uL")  # should not raise
        assert sm.get_item("Unknown") is not None


class TestCheckLowStock:
    def test_returns_low_items(self, sm):
        sm.add_stock("Ethanol", 100, "uL", reorder_threshold=500)
        sm.add_stock("Water", 5000, "uL", reorder_threshold=500)
        low = sm.check_low_stock()
        names = [i.name for i in low]
        assert "Ethanol" in names
        assert "Water" not in names

    def test_returns_empty_if_all_ok(self, sm):
        sm.add_stock("Ethanol", 5000, "uL", reorder_threshold=100)
        assert sm.check_low_stock() == []

    def test_at_threshold_is_low(self, sm):
        sm.add_stock("Edge", 500, "uL", reorder_threshold=500)
        low = sm.check_low_stock()
        assert any(i.name == "Edge" for i in low)


class TestGenerateShoppingList:
    def test_returns_shortfall_items(self, sm):
        doc = _Doc(reagents=[
            _Reagent("Ethanol", 5000),
            _Reagent("Water", 500),
        ])
        sm.add_stock("Water", 1000, "uL")  # enough
        # Ethanol not in stock
        shopping = sm.generate_shopping_list(doc, safety_buffer=1.0)
        names = [i["name"] for i in shopping]
        assert "Ethanol" in names
        assert "Water" not in names

    def test_safety_buffer_applied(self, sm):
        doc = _Doc(reagents=[_Reagent("Ethanol", 1000)])
        sm.add_stock("Ethanol", 1050, "uL")  # only just enough without buffer
        shopping = sm.generate_shopping_list(doc, safety_buffer=1.15)
        # 1000 * 1.15 = 1150 > 1050 → should appear
        assert any(i["name"] == "Ethanol" for i in shopping)


class TestConsumptionHistory:
    def test_filter_by_name(self, sm):
        sm.add_stock("Ethanol", 10000, "uL")
        sm.add_stock("Water", 10000, "uL")
        sm.consume("Ethanol", 100, run_id="r1")
        sm.consume("Water", 200, run_id="r1")
        sm.consume("Ethanol", 150, run_id="r2")
        hist = sm.consumption_history(name="Ethanol")
        assert len(hist) == 2
        assert all(r.reagent == "Ethanol" for r in hist)

    def test_filter_by_run_id(self, sm):
        sm.add_stock("Ethanol", 10000, "uL")
        sm.consume("Ethanol", 100, run_id="r1")
        sm.consume("Ethanol", 150, run_id="r2")
        hist = sm.consumption_history(run_id="r1")
        assert len(hist) == 1
        assert hist[0].run_id == "r1"

    def test_no_filter_returns_all(self, sm):
        sm.add_stock("Ethanol", 10000, "uL")
        sm.consume("Ethanol", 100, run_id="r1")
        sm.consume("Ethanol", 100, run_id="r2")
        assert len(sm.consumption_history()) == 2


class TestSaveLoad:
    def test_json_save_load_round_trip(self, sm, tmpdir):
        sm.add_stock("Ethanol", 10000, "uL", location="Fridge A")
        sm.consume("Ethanol", 500, run_id="r1")
        path = tmpdir / "inventory.json"
        sm.save(path)

        sm2 = StockManager()
        sm2.load(path)
        assert sm2.get_item("Ethanol").quantity == 9500
        assert sm2.get_item("Ethanol").location == "Fridge A"
        assert len(sm2.consumption_history()) == 1

    def test_yaml_save_load_round_trip(self, sm, tmpdir):
        sm.add_stock("Water", 5000, "uL")
        path = tmpdir / "inventory.yaml"
        sm.save(path)
        sm2 = StockManager()
        sm2.load(path)
        assert sm2.get_item("Water").quantity == 5000
