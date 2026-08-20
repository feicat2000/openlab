"""
Experiment Logger – records sensor readings and run metadata during a protocol
execution, then exports them to JSON / CSV / Excel / matplotlib plots.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class SensorReading:
    timestamp: float
    equipment: str        # "vortex", "centrifuge", …
    parameter: str        # "rpm", "temperature_c", "volume_ul"
    value: float
    unit: str
    step_id: Optional[int] = None


@dataclass
class ExperimentResult:
    run_id: str
    protocol_name: str
    operator: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    status: str = "unknown"
    readings: List[SensorReading] = field(default_factory=list)
    notes: str = ""


# ── ExperimentLogger ──────────────────────────────────────────────────────────

class ExperimentLogger:
    """
    Records sensor readings and run metadata during a protocol run.

    Usage::

        log = ExperimentLogger("run_001", "DNA Extraction", "alice")
        log.start()
        log.log_reading("vortex", "rpm", 2000, "rpm", step_id=3)
        log.finish("completed")
        log.save(Path("data/"))
    """

    def __init__(
        self,
        run_id: str,
        protocol_name: str,
        operator: str = "",
    ) -> None:
        self._result = ExperimentResult(
            run_id=run_id,
            protocol_name=protocol_name,
            operator=operator,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._result.started_at = time.time()
        logger.info(f"ExperimentLogger: run {self._result.run_id} started")

    def finish(self, status: str) -> None:
        self._result.ended_at = time.time()
        self._result.status = status
        logger.info(f"ExperimentLogger: run {self._result.run_id} finished – {status}")

    # ── Recording ─────────────────────────────────────────────────────────────

    def log_reading(
        self,
        equipment: str,
        parameter: str,
        value: float,
        unit: str,
        step_id: Optional[int] = None,
    ) -> None:
        reading = SensorReading(
            timestamp=time.time(),
            equipment=equipment,
            parameter=parameter,
            value=value,
            unit=unit,
            step_id=step_id,
        )
        self._result.readings.append(reading)

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_run_record(cls, run_record) -> "ExperimentLogger":
        """Create a logger pre-populated from an existing RunRecord dataclass."""
        inst = cls(
            run_id=run_record.run_id,
            protocol_name=run_record.protocol_name,
        )
        inst._result.started_at = run_record.started_at
        inst._result.ended_at = run_record.ended_at
        inst._result.status = run_record.status
        return inst

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(self, output_dir: Path) -> Path:
        """Save the experiment result as {run_id}_data.json."""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"{self._result.run_id}_data.json"
        payload = asdict(self._result)
        with open(path, "w") as fh:
            json.dump(payload, fh, indent=2)
        logger.info(f"Experiment data saved: {path}")
        return path

    @classmethod
    def load(cls, path: Path) -> "ExperimentLogger":
        """Load a previously saved logger state."""
        path = Path(path)
        with open(path) as fh:
            data = json.load(fh)
        readings = [SensorReading(**r) for r in data.get("readings", [])]
        result = ExperimentResult(
            run_id=data["run_id"],
            protocol_name=data["protocol_name"],
            operator=data.get("operator", ""),
            started_at=data.get("started_at", 0.0),
            ended_at=data.get("ended_at", 0.0),
            status=data.get("status", "unknown"),
            readings=readings,
            notes=data.get("notes", ""),
        )
        inst = cls(result.run_id, result.protocol_name, result.operator)
        inst._result = result
        return inst

    # ── Export ────────────────────────────────────────────────────────────────

    def export_csv(self, path: Path) -> None:
        """Export sensor readings to CSV."""
        import pandas as pd  # type: ignore
        path = Path(path)
        rows = [asdict(r) for r in self._result.readings]
        df = pd.DataFrame(rows) if rows else pd.DataFrame(
            columns=["timestamp", "equipment", "parameter", "value", "unit", "step_id"]
        )
        df.to_csv(path, index=False)
        logger.info(f"CSV exported: {path}")

    def export_excel(self, path: Path) -> None:
        """Export to multi-sheet Excel: summary + readings per equipment."""
        import pandas as pd  # type: ignore
        path = Path(path)
        rows = [asdict(r) for r in self._result.readings]
        readings_df = pd.DataFrame(rows) if rows else pd.DataFrame(
            columns=["timestamp", "equipment", "parameter", "value", "unit", "step_id"]
        )
        summary = {
            "run_id": [self._result.run_id],
            "protocol": [self._result.protocol_name],
            "operator": [self._result.operator],
            "status": [self._result.status],
            "started_at": [self._result.started_at],
            "ended_at": [self._result.ended_at],
            "duration_s": [self._result.ended_at - self._result.started_at],
            "reading_count": [len(self._result.readings)],
        }
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            pd.DataFrame(summary).to_excel(writer, sheet_name="Summary", index=False)
            readings_df.to_excel(writer, sheet_name="Readings", index=False)
        logger.info(f"Excel exported: {path}")

    def plot(self, output_dir: Path) -> List[Path]:
        """
        Generate one time-series plot per equipment type.
        Returns list of created PNG paths.
        """
        import matplotlib.pyplot as plt  # type: ignore
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Group by equipment
        by_equipment: Dict[str, list] = {}
        for r in self._result.readings:
            by_equipment.setdefault(r.equipment, []).append(r)

        paths: List[Path] = []
        for equip, readings in by_equipment.items():
            fig, ax = plt.subplots(figsize=(10, 4))
            by_param: Dict[str, list] = {}
            for r in readings:
                by_param.setdefault(r.parameter, []).append(r)
            for param, pts in by_param.items():
                xs = [p.timestamp - self._result.started_at for p in pts]
                ys = [p.value for p in pts]
                ax.plot(xs, ys, label=f"{param} ({pts[0].unit})")
            ax.set_title(f"{equip} – Run {self._result.run_id}")
            ax.set_xlabel("Time (s)")
            ax.legend()
            out = output_dir / f"{self._result.run_id}_{equip}.png"
            fig.savefig(out, dpi=100, bbox_inches="tight")
            plt.close(fig)
            paths.append(out)

        return paths

    # ── Summary ───────────────────────────────────────────────────────────────

    def summary(self) -> Dict[str, Any]:
        r = self._result
        duration = r.ended_at - r.started_at if r.ended_at else 0.0
        equipment_set = {rd.equipment for rd in r.readings}
        step_ids = {rd.step_id for rd in r.readings if rd.step_id is not None}
        return {
            "run_id": r.run_id,
            "protocol_name": r.protocol_name,
            "operator": r.operator,
            "status": r.status,
            "duration_s": round(duration, 2),
            "readings_count": len(r.readings),
            "step_count": len(step_ids),
            "equipment_used": sorted(equipment_set),
        }

    @property
    def result(self) -> ExperimentResult:
        return self._result
