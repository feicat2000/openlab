"""
Tests for ExperimentLogger.
"""
from __future__ import annotations

import json
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.data.experiment_logger import ExperimentLogger, SensorReading


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmpdir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def logger_instance():
    el = ExperimentLogger("run_test_001", "DNA Extraction", "alice")
    el.start()
    return el


# ── Minimal RunRecord stub ────────────────────────────────────────────────────

@dataclass
class _RunRecord:
    run_id: str = "run_stub"
    protocol_name: str = "Test Protocol"
    started_at: float = 1000.0
    ended_at: float = 1060.0
    status: str = "completed"


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestLogReading:
    def test_log_reading_appends(self, logger_instance):
        logger_instance.log_reading("vortex", "rpm", 2000, "rpm", step_id=3)
        assert len(logger_instance.result.readings) == 1
        r = logger_instance.result.readings[0]
        assert r.equipment == "vortex"
        assert r.parameter == "rpm"
        assert r.value == 2000
        assert r.unit == "rpm"
        assert r.step_id == 3

    def test_multiple_readings(self, logger_instance):
        logger_instance.log_reading("vortex", "rpm", 2000, "rpm")
        logger_instance.log_reading("centrifuge", "rpm", 13000, "rpm")
        logger_instance.log_reading("incubator", "temperature_c", 37.5, "°C")
        assert len(logger_instance.result.readings) == 3

    def test_reading_without_step_id(self, logger_instance):
        logger_instance.log_reading("vortex", "rpm", 1500, "rpm")
        assert logger_instance.result.readings[0].step_id is None


class TestLifecycle:
    def test_start_sets_started_at(self):
        el = ExperimentLogger("r1", "P1")
        before = time.time()
        el.start()
        after = time.time()
        assert before <= el.result.started_at <= after

    def test_finish_sets_status_and_ended_at(self, logger_instance):
        before = time.time()
        logger_instance.finish("completed")
        assert logger_instance.result.status == "completed"
        assert logger_instance.result.ended_at >= before


class TestSaveLoad:
    def test_save_creates_json(self, logger_instance, tmpdir):
        logger_instance.log_reading("vortex", "rpm", 2000, "rpm")
        logger_instance.finish("completed")
        path = logger_instance.save(tmpdir)
        assert path.exists()
        assert path.suffix == ".json"

    def test_load_round_trip(self, logger_instance, tmpdir):
        logger_instance.log_reading("vortex", "rpm", 2000, "rpm", step_id=1)
        logger_instance.finish("completed")
        path = logger_instance.save(tmpdir)

        loaded = ExperimentLogger.load(path)
        assert loaded.result.run_id == "run_test_001"
        assert loaded.result.protocol_name == "DNA Extraction"
        assert loaded.result.operator == "alice"
        assert loaded.result.status == "completed"
        assert len(loaded.result.readings) == 1
        assert loaded.result.readings[0].value == 2000

    def test_load_preserves_readings(self, logger_instance, tmpdir):
        for i in range(5):
            logger_instance.log_reading("centrifuge", "rpm", 13000 + i, "rpm", step_id=i)
        logger_instance.finish("completed")
        path = logger_instance.save(tmpdir)
        loaded = ExperimentLogger.load(path)
        assert len(loaded.result.readings) == 5


class TestFromRunRecord:
    def test_from_run_record_populates_fields(self):
        rec = _RunRecord()
        el = ExperimentLogger.from_run_record(rec)
        assert el.result.run_id == "run_stub"
        assert el.result.protocol_name == "Test Protocol"
        assert el.result.started_at == 1000.0
        assert el.result.ended_at == 1060.0
        assert el.result.status == "completed"


class TestExportCSV:
    def test_export_csv_creates_file(self, logger_instance, tmpdir):
        logger_instance.log_reading("vortex", "rpm", 2000, "rpm")
        logger_instance.finish("completed")
        out = tmpdir / "out.csv"
        with patch("pandas.DataFrame.to_csv") as mock_csv:
            logger_instance.export_csv(out)
            mock_csv.assert_called_once()

    def test_export_csv_empty_readings(self, tmpdir):
        el = ExperimentLogger("empty_run", "P1")
        el.start()
        el.finish("completed")
        out = tmpdir / "empty.csv"
        with patch("pandas.DataFrame.to_csv"):
            el.export_csv(out)  # should not raise


class TestExportExcel:
    def test_export_excel_called(self, logger_instance, tmpdir):
        logger_instance.finish("completed")
        out = tmpdir / "out.xlsx"
        mock_writer = MagicMock()
        mock_writer.__enter__ = MagicMock(return_value=mock_writer)
        mock_writer.__exit__ = MagicMock(return_value=False)
        with patch("pandas.ExcelWriter", return_value=mock_writer):
            with patch("pandas.DataFrame.to_excel"):
                logger_instance.export_excel(out)


class TestPlot:
    def test_plot_returns_paths(self, logger_instance, tmpdir):
        logger_instance.log_reading("vortex", "rpm", 2000, "rpm")
        logger_instance.log_reading("vortex", "rpm", 2100, "rpm")
        logger_instance.finish("completed")

        mock_fig = MagicMock()
        mock_ax = MagicMock()
        with patch("matplotlib.pyplot.subplots", return_value=(mock_fig, mock_ax)):
            with patch("matplotlib.pyplot.close"):
                paths = logger_instance.plot(tmpdir)
        assert len(paths) == 1

    def test_plot_empty_no_files(self, tmpdir):
        el = ExperimentLogger("empty", "P")
        el.start()
        el.finish("completed")
        with patch("matplotlib.pyplot.subplots", return_value=(MagicMock(), MagicMock())):
            with patch("matplotlib.pyplot.close"):
                paths = el.plot(tmpdir)
        assert paths == []


class TestSummary:
    def test_summary_correct_counts(self, logger_instance):
        logger_instance.log_reading("vortex", "rpm", 2000, "rpm", step_id=1)
        logger_instance.log_reading("centrifuge", "rpm", 13000, "rpm", step_id=2)
        logger_instance.finish("completed")
        s = logger_instance.summary()
        assert s["readings_count"] == 2
        assert s["step_count"] == 2
        assert "vortex" in s["equipment_used"]
        assert "centrifuge" in s["equipment_used"]

    def test_summary_duration(self):
        el = ExperimentLogger("dur", "P")
        el._result.started_at = 0.0
        el._result.ended_at = 60.0
        s = el.summary()
        assert s["duration_s"] == 60.0
