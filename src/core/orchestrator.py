"""
Orchestrator – the central execution engine of OpenLab.

Responsibilities
─────────────────
• Accept a validated ProtocolDocument.
• Instantiate and connect all required equipment.
• Execute each protocol step in order, dispatching to the correct driver.
• Coordinate Opentrons ↔ robotic arm (interlocks, handoff positions).
• Check safety gates before every arm movement.
• Log a complete run record (JSON) and print live progress.
• Support simulation mode (no real hardware).

Step dispatch table
────────────────────
  pipette         → OpentronsController (direct command or protocol upload)
  vortex          → Vortex.run_for()  + TubeHandler.transfer_to_vortex()
  centrifuge      → Centrifuge.run()  + TubeHandler.transfer_to_centrifuge()
  incubate        → Incubator.incubate() + TubeHandler.transfer_to_incubator()
  magnetic_sep    → MagneticSeparator.separate()
  rock            → Rocker.rock_for() + TubeHandler.transfer_to_rocker()
  tighten_cap     → TubeHandler.tighten_cap()
  loosen_cap      → TubeHandler.loosen_cap()
  invert_tube     → TubeHandler.invert_tube()
  transfer_tube   → TubeHandler.transfer_tube()
  temperature_set → Incubator.set_temperature()
  wait            → time.sleep()
  comment         → log only
  manual          → pause, wait for user confirmation
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from .protocol_parser import ProtocolDocument, ProtocolStep, StepType
from .resource_manager import ResourceManager
from .risk_assessment import RiskAssessor, RiskReport
from ..equipment.base_equipment import EquipmentConfig, EquipmentStatus
from ..equipment.opentrons_controller import OpentronsController
from ..equipment.robotic_arm import RoboticArm, ArmPositions
from ..equipment.peripherals.vortex import Vortex
from ..equipment.peripherals.centrifuge import Centrifuge
from ..equipment.peripherals.incubator import Incubator
from ..equipment.peripherals.magnetic_separator import MagneticSeparator
from ..equipment.peripherals.rocker import Rocker
from ..equipment.peripherals.tube_handler import TubeHandler
from ..safety.safety_monitor import SafetyMonitor


# ── Run record ────────────────────────────────────────────────────────────────

@dataclass
class StepRecord:
    step_id:    int
    step_name:  str
    step_type:  str
    started_at: float
    ended_at:   float = 0.0
    status:     str   = "pending"   # running | completed | failed | skipped
    error:      Optional[str] = None


@dataclass
class RunRecord:
    protocol_name: str
    run_id:        str
    started_at:    float
    ended_at:      float         = 0.0
    status:        str           = "running"
    steps:         List[StepRecord] = field(default_factory=list)
    errors:        List[str]        = field(default_factory=list)


# ── Orchestrator ──────────────────────────────────────────────────────────────

class Orchestrator:
    """
    Main execution engine.

    config: dict loaded from hardware_config.yaml
    simulate: override all equipment into simulation mode
    """

    def __init__(self, config: dict, simulate: bool = True) -> None:
        self._config   = config
        self._simulate = simulate
        self._console  = Console()

        # Equipment instances (populated in _setup_equipment)
        self._ot:    Optional[OpentronsController]  = None
        self._arm:   Optional[RoboticArm]           = None
        self._vortex: Optional[Vortex]              = None
        self._cent:  Optional[Centrifuge]           = None
        self._inc:   Optional[Incubator]            = None
        self._mag:   Optional[MagneticSeparator]    = None
        self._rock:  Optional[Rocker]               = None
        self._tube_handler: Optional[TubeHandler]   = None

        self._safety: Optional[SafetyMonitor] = None
        self._run_record: Optional[RunRecord] = None

    # ── Public entry point ────────────────────────────────────────────────────

    def execute(
        self,
        doc: ProtocolDocument,
        runs_dir: Path = Path("runs"),
        confirm_before_run: bool = True,
        skip_steps: Optional[List[int]] = None,
    ) -> RunRecord:
        """
        Full pipeline:
          1. Pre-flight reports (materials, risk)
          2. User confirmation
          3. Equipment setup
          4. Step execution
          5. Teardown and run record
        """
        runs_dir.mkdir(parents=True, exist_ok=True)

        # ── Pre-flight ────────────────────────────────────────────────────────
        self._print_header(doc)

        resource_mgr = ResourceManager()
        risk_assessor = RiskAssessor()

        mat_report  = resource_mgr.print_material_report(doc)
        risk_report = risk_assessor.assess(doc)
        risk_assessor.print_report(risk_report)

        if not risk_report.clearance:
            raise RuntimeError(
                "Protocol BLOCKED by risk assessment. "
                "Obtain Biosafety Officer approval before proceeding."
            )

        if mat_report.missing:
            self._console.print(
                f"\n[red bold]⚠ Missing materials: {mat_report.missing}[/red bold]"
            )

        if confirm_before_run:
            self._console.print("\n[bold yellow]Ready to run. Press Enter to start or Ctrl-C to abort...[/bold yellow]")
            try:
                input()
            except KeyboardInterrupt:
                logger.info("Run aborted by user")
                raise SystemExit(0)

        # ── Run ───────────────────────────────────────────────────────────────
        import uuid
        run_id = f"{doc.metadata.name.replace(' ', '_')}_{int(time.time())}_{uuid.uuid4().hex[:6]}"

        self._run_record = RunRecord(
            protocol_name=doc.metadata.name,
            run_id=run_id,
            started_at=time.time(),
        )

        try:
            self._setup_equipment(doc)
            self._safety.start()
            self._execute_steps(doc.steps, skip_steps or [])
            self._run_record.status = "completed"
            self._console.print("\n[bold green]✓ Protocol completed successfully[/bold green]")

        except KeyboardInterrupt:
            self._console.print("\n[yellow]Run interrupted by user[/yellow]")
            self._run_record.status = "aborted"
            if self._safety:
                self._safety.trigger_estop("user_interrupt")

        except Exception as exc:
            logger.exception(f"Run failed: {exc}")
            self._run_record.status = "failed"
            self._run_record.errors.append(str(exc))
            if self._safety:
                self._safety.trigger_estop("orchestrator_exception")
            raise

        finally:
            self._run_record.ended_at = time.time()
            self._teardown_equipment()
            self._save_run_record(runs_dir)

        return self._run_record

    # ── Equipment setup ───────────────────────────────────────────────────────

    def _setup_equipment(self, doc: ProtocolDocument) -> None:
        cfg   = self._config
        sim   = self._simulate
        req   = {e.name for e in doc.equipment}

        def make_cfg(key: str, **extra) -> EquipmentConfig:
            hw = cfg.get(key, {})
            return EquipmentConfig(
                name=key,
                port=hw.get("port"),
                ip_address=hw.get("ip"),
                baud_rate=hw.get("baud_rate", 9600),
                simulate=sim or hw.get("simulate", True),
                extra={**hw, **extra},
            )

        logger.info("Setting up equipment...")

        # Opentrons (always present)
        self._ot = OpentronsController(make_cfg("opentrons"))
        self._ot.connect()

        # Robotic arm (always present)
        arm_cfg   = make_cfg("robotic_arm")
        positions = ArmPositions.from_dict(cfg.get("arm_positions", {}))
        self._arm = RoboticArm(arm_cfg, positions=positions)
        self._arm.connect()
        self._tube_handler = TubeHandler(self._arm)

        # Peripherals – only connect what the protocol needs
        if "vortex" in req:
            self._vortex = Vortex(make_cfg("vortex"))
            self._vortex.connect()

        if "centrifuge" in req:
            self._cent = Centrifuge(make_cfg("centrifuge"))
            self._cent.connect()

        if "incubator" in req or "temperature_control" in req:
            self._inc = Incubator(make_cfg("incubator"))
            self._inc.connect()

        if "magnetic_separator" in req:
            self._mag = MagneticSeparator(make_cfg(
                "magnetic_separator",
                relay_backend=cfg.get("relay_backend", "simulation"),
            ))
            self._mag.connect()

        if "rocker" in req:
            self._rock = Rocker(make_cfg(
                "rocker",
                relay_backend=cfg.get("relay_backend", "simulation"),
            ))
            self._rock.connect()

        # Safety monitor
        self._safety = SafetyMonitor(cfg.get("safety", {}))
        if self._cent:
            self._safety.register("centrifuge", self._cent)
        if self._inc:
            self._safety.register("incubator", self._inc)
        if self._arm:
            self._safety.register("robotic_arm", self._arm)

        logger.info("All equipment connected")

    def _teardown_equipment(self) -> None:
        logger.info("Tearing down equipment...")
        if self._safety:
            self._safety.stop()
        for eq in [self._arm, self._ot, self._vortex, self._cent,
                   self._inc, self._mag, self._rock]:
            if eq:
                try:
                    eq.disconnect()
                except Exception as exc:
                    logger.error(f"Disconnect error: {exc}")

    # ── Step execution ────────────────────────────────────────────────────────

    def _execute_steps(self, steps: List[ProtocolStep], skip: List[int]) -> None:
        total = len(steps)
        for step in steps:
            if step.id in skip:
                logger.info(f"Skipping step {step.id}: {step.name}")
                continue

            rec = StepRecord(
                step_id=step.id,
                step_name=step.name,
                step_type=step.type,
                started_at=time.time(),
                status="running",
            )
            self._run_record.steps.append(rec)

            self._console.print(
                f"\n[cyan]▶ Step {step.id}/{total}:[/cyan] [bold]{step.name}[/bold] "
                f"[dim]({step.type})[/dim]"
            )

            try:
                self._dispatch(step)
                rec.status   = "completed"
                rec.ended_at = time.time()
                elapsed = rec.ended_at - rec.started_at
                self._console.print(f"  [green]✓[/green] Done in {elapsed:.1f}s")

            except Exception as exc:
                rec.status   = "failed"
                rec.error    = str(exc)
                rec.ended_at = time.time()
                logger.error(f"Step {step.id} failed: {exc}")
                raise

    def _dispatch(self, step: ProtocolStep) -> None:
        """Route a step to the correct handler method."""
        handlers = {
            StepType.PIPETTE:         self._step_pipette,
            StepType.VORTEX:          self._step_vortex,
            StepType.CENTRIFUGE:      self._step_centrifuge,
            StepType.INCUBATE:        self._step_incubate,
            StepType.MAGNETIC_SEP:    self._step_magnetic_sep,
            StepType.ROCK:            self._step_rock,
            StepType.TIGHTEN_CAP:     self._step_tighten_cap,
            StepType.LOOSEN_CAP:      self._step_loosen_cap,
            StepType.INVERT_TUBE:     self._step_invert_tube,
            StepType.TRANSFER_TUBE:   self._step_transfer_tube,
            StepType.TEMPERATURE_SET: self._step_temperature_set,
            StepType.WAIT:            self._step_wait,
            StepType.COMMENT:         self._step_comment,
            StepType.MANUAL:          self._step_manual,
        }
        fn = handlers.get(step.type)
        if fn is None:
            raise ValueError(f"Unknown step type: {step.type}")
        fn(step)

    # ── Step handlers ─────────────────────────────────────────────────────────

    def _step_pipette(self, step: ProtocolStep) -> None:
        """Delegate liquid handling to Opentrons."""
        if not self._ot:
            raise RuntimeError("Opentrons not connected")
        # Build an inline command (for live control mode)
        # For full protocol mode, the OT protocol is pre-uploaded.
        params = step.params
        logger.info(
            f"Pipette: {params.get('volume_ul', '?')}µL "
            f"{params.get('source', '?')} → {params.get('destination', '?')}"
        )
        # In simulation the OT controller handles the log
        if self._ot.config.simulate:
            self._ot._simulate_op(
                f"aspirate {params.get('volume_ul')}µL from {params.get('source')} "
                f"→ dispense to {params.get('destination')}",
                duration=2.0,
            )

    def _step_vortex(self, step: ProtocolStep) -> None:
        if not self._vortex:
            raise RuntimeError("Vortex not connected")
        self._arm_move_safe()
        tube_pos = self._resolve_tube_position(step)
        if tube_pos:
            self._tube_handler.transfer_to_vortex(tube_pos)
        self._vortex.run_for(
            rpm=step.rpm or 2000,
            duration_s=step.duration_s or 30,
        )
        if tube_pos:
            self._tube_handler.transfer_tube(self._arm._positions.vortex, tube_pos)
            self._arm.home()

    def _step_centrifuge(self, step: ProtocolStep) -> None:
        if not self._cent:
            raise RuntimeError("Centrifuge not connected")
        self._arm_move_safe()
        tube_pos = self._resolve_tube_position(step)
        if tube_pos:
            self._tube_handler.transfer_to_centrifuge(tube_pos)
            self._arm.home()   # CRITICAL: arm away before spin
        self._cent.run(
            rpm=step.rpm or 13000,
            duration_s=int(step.duration_s or 60),
            temperature_c=step.centrifuge_temp,
            wait=True,
        )
        if tube_pos:
            self._arm_move_safe()
            self._tube_handler.retrieve_from_centrifuge(tube_pos)

    def _step_incubate(self, step: ProtocolStep) -> None:
        if not self._inc:
            raise RuntimeError("Incubator not connected")
        self._arm_move_safe()
        tube_pos = self._resolve_tube_position(step)
        if tube_pos:
            self._tube_handler.transfer_to_incubator(tube_pos)
        self._inc.incubate(
            temperature_c=step.temperature_c or 37,
            duration_s=step.duration_s or 600,
            shake_rpm=int(step.params.get("shake_rpm", 0)),
        )
        if tube_pos:
            self._arm_move_safe()
            self._tube_handler.retrieve_from_incubator(tube_pos)

    def _step_magnetic_sep(self, step: ProtocolStep) -> None:
        if not self._mag:
            raise RuntimeError("Magnetic separator not connected")
        self._mag.separate(duration_s=step.separation_time_s)

    def _step_rock(self, step: ProtocolStep) -> None:
        if not self._rock:
            raise RuntimeError("Rocker not connected")
        self._arm_move_safe()
        tube_pos = self._resolve_tube_position(step)
        if tube_pos:
            self._tube_handler.transfer_to_rocker(tube_pos)
        self._rock.rock_for(
            duration_s=step.duration_s or 300,
            rpm=step.rpm,
        )
        if tube_pos:
            self._arm_move_safe()
            self._tube_handler.transfer_tube(self._arm._positions.rocker, tube_pos)

    def _step_tighten_cap(self, step: ProtocolStep) -> None:
        self._arm_move_safe()
        tube_pos = self._resolve_tube_position(step)
        if not tube_pos:
            logger.warning("tighten_cap: no tube_position defined, skipping")
            return
        self._tube_handler.tighten_cap(tube_pos)

    def _step_loosen_cap(self, step: ProtocolStep) -> None:
        self._arm_move_safe()
        tube_pos = self._resolve_tube_position(step)
        if not tube_pos:
            logger.warning("loosen_cap: no tube_position defined, skipping")
            return
        self._tube_handler.loosen_cap(tube_pos)

    def _step_invert_tube(self, step: ProtocolStep) -> None:
        self._arm_move_safe()
        tube_pos = self._resolve_tube_position(step)
        if not tube_pos:
            logger.warning("invert_tube: no tube_position defined, skipping")
            return
        self._tube_handler.invert_tube(tube_pos, n_times=step.n_inversions)

    def _step_transfer_tube(self, step: ProtocolStep) -> None:
        self._arm_move_safe()
        src  = self._parse_pose(step.params.get("source_pose"))
        dest = self._parse_pose(step.params.get("dest_pose"))
        if src and dest:
            self._tube_handler.transfer_tube(src, dest)
        else:
            logger.warning("transfer_tube: source_pose or dest_pose missing")

    def _step_temperature_set(self, step: ProtocolStep) -> None:
        if not self._inc:
            raise RuntimeError("Incubator not connected")
        self._inc.set_temperature(step.temperature_c or 37)
        if step.params.get("wait_for_temp", False):
            self._inc.wait_for_temperature()

    def _step_wait(self, step: ProtocolStep) -> None:
        duration = step.duration_s or 0
        logger.info(f"Waiting {duration}s")
        if self._simulate:
            time.sleep(min(duration, 1.0))
        else:
            time.sleep(duration)

    def _step_comment(self, step: ProtocolStep) -> None:
        self._console.print(f"  [dim]ℹ  {step.notes or step.name}[/dim]")

    def _step_manual(self, step: ProtocolStep) -> None:
        self._console.print(
            f"\n[bold yellow]⏸ MANUAL STEP:[/bold yellow] {step.name}\n"
            f"  {step.notes or 'Perform the required manual action.'}\n"
            "  Press Enter when done..."
        )
        if not self._simulate:
            input()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _arm_move_safe(self) -> None:
        """Block until safety monitor clears arm movement."""
        if not self._safety:
            return
        deadline = time.time() + 120
        while time.time() < deadline:
            if self._safety.can_arm_move():
                return
            faults = self._safety.get_active_faults()
            logger.debug(f"Waiting for arm clearance, active faults: {faults}")
            time.sleep(1.0)
        raise RuntimeError("Arm movement not cleared within 120s – active safety faults")

    def _resolve_tube_position(self, step: ProtocolStep):
        """Return a Pose if tube_position is specified in step params."""
        raw = step.params.get("tube_position") or step.params.get("tube_pose")
        return self._parse_pose(raw)

    def _parse_pose(self, raw) -> Optional[list]:
        if isinstance(raw, list) and len(raw) == 6:
            return [float(v) for v in raw]
        return None

    def _print_header(self, doc: ProtocolDocument) -> None:
        self._console.print(
            f"\n[bold cyan]╔══════════════════════════════════════════╗[/bold cyan]\n"
            f"[bold cyan]║  OpenLab – {doc.metadata.name:<30}║[/bold cyan]\n"
            f"[bold cyan]╚══════════════════════════════════════════╝[/bold cyan]\n"
            f"  Version  : {doc.metadata.version}\n"
            f"  Author   : {doc.metadata.author}\n"
            f"  Steps    : {len(doc.steps)}\n"
            f"  Simulate : {'[yellow]YES[/yellow]' if self._simulate else '[green]NO – REAL HARDWARE[/green]'}\n"
        )

    def _save_run_record(self, runs_dir: Path) -> None:
        if not self._run_record:
            return
        path = runs_dir / f"{self._run_record.run_id}.json"
        with open(path, "w") as fh:
            json.dump(asdict(self._run_record), fh, indent=2, default=str)
        logger.info(f"Run record saved: {path}")
