#!/usr/bin/env python3
"""
OpenLab CLI – entry point.

Usage examples
──────────────
  # Run a protocol (simulation)
  python main.py run protocols/dna_extraction.yaml

  # Run on real hardware
  python main.py run protocols/dna_extraction.yaml --no-simulate

  # Validate without running
  python main.py validate protocols/dna_extraction.yaml

  # Show material list only
  python main.py materials protocols/dna_extraction.yaml

  # Show risk assessment only
  python main.py risk protocols/dna_extraction.yaml

  # Start REST API server
  python main.py api

  # Start API in simulation mode on custom port
  python main.py api --port 9000 --simulate
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import click
import yaml
from dotenv import load_dotenv
from loguru import logger
from rich.console import Console

# Load .env before anything else
load_dotenv(Path(__file__).parent / ".env")

console = Console()


# ── Logging setup ─────────────────────────────────────────────────────────────

def configure_logging(level: str = "INFO", log_dir: str = "logs") -> None:
    Path(log_dir).mkdir(exist_ok=True)
    logger.remove()
    logger.add(sys.stderr, level=level, colorize=True,
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | <cyan>{extra[equipment]}</cyan> {message}"
               if False else
               "<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")
    logger.add(
        f"{log_dir}/openlab_{{time:YYYY-MM-DD}}.log",
        level="DEBUG",
        rotation="00:00",
        retention="30 days",
        compression="gz",
    )


# ── Config loading ────────────────────────────────────────────────────────────

def load_config(config_path: Path) -> dict:
    if not config_path.exists():
        logger.warning(f"Config not found at {config_path} – using defaults")
        return {}
    with open(config_path) as fh:
        cfg = yaml.safe_load(fh) or {}
    # Override with environment variables
    if os.getenv("OPENTRONS_HOST"):
        cfg.setdefault("opentrons", {})["ip"] = os.getenv("OPENTRONS_HOST")
    if os.getenv("UR_HOST"):
        cfg.setdefault("robotic_arm", {})["ip"] = os.getenv("UR_HOST")
    return cfg


# ── CLI ───────────────────────────────────────────────────────────────────────

@click.group()
@click.option("--config", default="config/hardware_config.yaml",
              show_default=True, help="Hardware config file")
@click.option("--log-level", default=os.getenv("LOG_LEVEL", "INFO"),
              show_default=True, help="Logging level")
@click.option("--log-dir", default=os.getenv("LOG_DIR", "logs"),
              show_default=True, help="Log directory")
@click.pass_context
def cli(ctx: click.Context, config: str, log_level: str, log_dir: str) -> None:
    """OpenLab – Automated In-Vitro Experiment Platform."""
    configure_logging(log_level, log_dir)
    ctx.ensure_object(dict)
    ctx.obj["config"]     = load_config(Path(config))
    ctx.obj["config_path"] = config


# ── run ───────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("protocol", type=click.Path(exists=True, path_type=Path))
@click.option("--simulate/--no-simulate",
              default=os.getenv("SIMULATION_MODE", "true").lower() == "true",
              show_default=True, help="Simulation mode (no real hardware)")
@click.option("--skip", "-s", multiple=True, type=int,
              help="Step IDs to skip (can repeat: -s 3 -s 5)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def run(ctx, protocol: Path, simulate: bool, skip: tuple, yes: bool) -> None:
    """Execute a protocol YAML file."""
    from src.core.protocol_parser import ProtocolParser
    from src.core.orchestrator import Orchestrator

    cfg    = ctx.obj["config"]
    parser = ProtocolParser()

    try:
        doc = parser.load(protocol)
    except Exception as exc:
        console.print(f"[red]Protocol load error: {exc}[/red]")
        sys.exit(1)

    if simulate:
        console.print("[yellow bold]⚠  SIMULATION MODE – no real hardware will be used[/yellow bold]")

    orch = Orchestrator(config=cfg, simulate=simulate)
    try:
        record = orch.execute(
            doc,
            confirm_before_run=not yes,
            skip_steps=list(skip),
        )
        if record.status == "completed":
            console.print(f"\n[green bold]Run {record.run_id} completed.[/green bold]")
        else:
            console.print(f"\n[red]Run {record.run_id} ended with status: {record.status}[/red]")
            sys.exit(1)
    except SystemExit:
        pass
    except Exception as exc:
        console.print(f"[red bold]Run failed: {exc}[/red bold]")
        sys.exit(1)


# ── validate ──────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("protocol", type=click.Path(exists=True, path_type=Path))
@click.pass_context
def validate(ctx, protocol: Path) -> None:
    """Validate a protocol file (no execution)."""
    from src.core.protocol_parser import ProtocolParser
    from src.core.resource_manager import ResourceManager
    from src.core.risk_assessment import RiskAssessor

    parser   = ProtocolParser()
    resource = ResourceManager()
    assessor = RiskAssessor()

    try:
        doc = parser.load(protocol)
    except Exception as exc:
        console.print(f"[red]Parse error: {exc}[/red]")
        sys.exit(1)

    resource.print_material_report(doc)
    risk = assessor.assess(doc)
    assessor.print_report(risk)

    console.print(
        f"\n[bold]Validation result:[/bold] "
        + ("[green]PASS[/green]" if risk.clearance else "[red bold]BLOCKED[/red bold]")
    )


# ── materials ─────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("protocol", type=click.Path(exists=True, path_type=Path))
@click.option("--export", type=click.Path(path_type=Path),
              help="Export to Excel file (.xlsx)")
@click.pass_context
def materials(ctx, protocol: Path, export: Path) -> None:
    """Print material and consumable list for a protocol."""
    from src.core.protocol_parser import ProtocolParser
    from src.core.resource_manager import ResourceManager

    doc    = ProtocolParser().load(protocol)
    rm     = ResourceManager()
    report = rm.print_material_report(doc)

    if export:
        rm.export_excel(doc, export)
        console.print(f"[green]Exported to {export}[/green]")


# ── risk ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("protocol", type=click.Path(exists=True, path_type=Path))
@click.option("--json-out", type=click.Path(path_type=Path),
              help="Export risk report as JSON")
@click.pass_context
def risk(ctx, protocol: Path, json_out: Path) -> None:
    """Print risk assessment for a protocol."""
    from src.core.protocol_parser import ProtocolParser
    from src.core.risk_assessment import RiskAssessor

    doc      = ProtocolParser().load(protocol)
    assessor = RiskAssessor()
    report   = assessor.assess(doc)
    assessor.print_report(report)

    if json_out:
        assessor.export_json(report, json_out)
        console.print(f"[green]Risk report exported to {json_out}[/green]")


# ── api ───────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--host", default=os.getenv("API_HOST", "0.0.0.0"), show_default=True)
@click.option("--port", default=int(os.getenv("API_PORT", "8000")), show_default=True, type=int)
@click.option("--simulate/--no-simulate",
              default=os.getenv("SIMULATION_MODE", "true").lower() == "true")
@click.option("--reload", is_flag=True, help="Auto-reload on code changes (dev mode)")
@click.pass_context
def api(ctx, host: str, port: int, simulate: bool, reload: bool) -> None:
    """Start the OpenLab REST API server."""
    import uvicorn
    from src.ui.api import create_app

    cfg        = ctx.obj["config"]
    app        = create_app(cfg, simulate=simulate)
    sim_notice = " [SIMULATION]" if simulate else " [REAL HARDWARE]"
    console.print(f"[cyan bold]Starting OpenLab API on http://{host}:{port}{sim_notice}[/cyan bold]")
    console.print(f"  Docs: http://{host}:{port}/docs")

    uvicorn.run(
        app,
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


# ── diagnose ──────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--simulate/--no-simulate",
              default=os.getenv("SIMULATION_MODE", "true").lower() == "true",
              help="Skip live hardware checks in simulation mode")
@click.option("--quick", is_flag=True,
              help="Skip slow tests (simulation run, etc.)")
@click.option("--category", "-c", multiple=True,
              help="Filter by category (repeat for multiple). "
                   "Options: Environment, System, Configuration, Permissions, "
                   "Serial Devices, Network, Opentrons, Robotic Arm, "
                   "Peripherals, GPIO, Protocols, Platform")
@click.option("--export", type=click.Path(path_type=Path),
              help="Export report (.json or .html)")
@click.pass_context
def diagnose(
    ctx,
    simulate: bool,
    quick: bool,
    category: tuple,
    export: Optional[Path],
) -> None:
    """Run full hardware and software diagnostic suite."""
    from src.diagnostics.diagnostic_runner import DiagnosticRunner

    cfg      = ctx.obj["config"]
    root     = Path(__file__).parent
    runner   = DiagnosticRunner(config=cfg, project_root=root, simulate=simulate)
    cats     = list(category) or None

    results  = runner.run(categories=cats, quick=quick)
    runner.print_report(results)

    if export:
        runner.export(results, export)

    fail_count = sum(1 for r in results if r.status.value == "FAIL")
    sys.exit(1 if fail_count > 0 else 0)


# ── calibrate ─────────────────────────────────────────────────────────────────

@cli.group()
@click.pass_context
def calibrate(ctx) -> None:
    """Manage calibration profiles for arm and pipettes."""


@calibrate.command("status")
@click.option("--profile", default="default", show_default=True)
@click.pass_context
def calibrate_status(ctx, profile: str) -> None:
    """Show current calibration profile status."""
    from src.calibration.calibration_manager import CalibrationManager
    mgr = CalibrationManager()
    try:
        p = mgr.load(profile)
        mgr.print_status(p)
    except FileNotFoundError:
        console.print(f"[yellow]Profile '{profile}' not found. Available: {mgr.list_profiles()}[/yellow]")


@calibrate.command("set-pose")
@click.argument("pose_name")
@click.argument("x", type=float)
@click.argument("y", type=float)
@click.argument("z", type=float)
@click.argument("rx", type=float)
@click.argument("ry", type=float)
@click.argument("rz", type=float)
@click.option("--profile", default="default", show_default=True)
@click.option("--operator", default="operator", show_default=True)
@click.pass_context
def calibrate_set_pose(ctx, pose_name, x, y, z, rx, ry, rz, profile, operator) -> None:
    """Set a named TCP pose in a calibration profile."""
    from src.calibration.calibration_manager import CalibrationManager
    mgr = CalibrationManager()
    try:
        p = mgr.load(profile)
    except FileNotFoundError:
        p = mgr.create_profile(profile, operator)
    mgr.set_pose(p, pose_name, [x, y, z, rx, ry, rz], operator)
    mgr.save(p)
    console.print(f"[green]Pose '{pose_name}' saved to profile '{profile}'.[/green]")


@calibrate.command("check-drift")
@click.option("--profile", default="default", show_default=True)
@click.option("--threshold", default=2.0, show_default=True, type=float)
@click.pass_context
def calibrate_check_drift(ctx, profile, threshold) -> None:
    """Compare live arm poses against calibrated profile (sim: uses stored poses)."""
    from src.calibration.calibration_manager import CalibrationManager
    mgr = CalibrationManager()
    try:
        p = mgr.load(profile)
    except FileNotFoundError:
        console.print(f"[red]Profile '{profile}' not found.[/red]")
        sys.exit(1)
    if p.arm:
        current = {name: pc.pose for name, pc in p.arm.poses.items()}
        drifted = mgr.check_drift(p, current, threshold_mm=threshold)
        if drifted:
            for pose_name, drift_mm in drifted.items():
                console.print(f"[red]DRIFT: {pose_name} – {drift_mm:.2f} mm[/red]")
        else:
            console.print("[green]No drift detected.[/green]")
    else:
        console.print("[dim]No arm calibration data in profile.[/dim]")


@calibrate.command("apply")
@click.option("--profile", default="default", show_default=True)
@click.option("--config", "config_path", default="config/hardware_config.yaml", show_default=True)
@click.pass_context
def calibrate_apply(ctx, profile, config_path) -> None:
    """Apply calibrated poses to hardware_config.yaml."""
    from src.calibration.calibration_manager import CalibrationManager
    mgr = CalibrationManager()
    try:
        p = mgr.load(profile)
    except FileNotFoundError:
        console.print(f"[red]Profile '{profile}' not found.[/red]")
        sys.exit(1)
    mgr.apply_to_config(p, Path(config_path))
    console.print(f"[green]Calibration profile '{profile}' applied to {config_path}.[/green]")


@calibrate.command("export")
@click.option("--profile", default="default", show_default=True)
@click.option("--out", default="calibration_report.txt", show_default=True, type=click.Path(path_type=Path))
@click.pass_context
def calibrate_export(ctx, profile, out) -> None:
    """Export calibration profile to a text report."""
    from src.calibration.calibration_manager import CalibrationManager
    mgr = CalibrationManager()
    try:
        p = mgr.load(profile)
    except FileNotFoundError:
        console.print(f"[red]Profile '{profile}' not found.[/red]")
        sys.exit(1)
    mgr.export_report(p, out)
    console.print(f"[green]Report written to {out}.[/green]")


# ── schedule ──────────────────────────────────────────────────────────────────

@cli.group()
@click.pass_context
def schedule(ctx) -> None:
    """Schedule protocol runs."""


@schedule.command("submit")
@click.argument("protocol", type=click.Path(exists=True, path_type=Path))
@click.option("--at", "scheduled_at", default=None, help='Scheduled time e.g. "2026-03-01 08:00"')
@click.option("--priority", default=5, show_default=True, type=int)
@click.option("--simulate/--no-simulate",
              default=os.getenv("SIMULATION_MODE", "true").lower() == "true")
@click.pass_context
def schedule_submit(ctx, protocol, scheduled_at, priority, simulate) -> None:
    """Submit a protocol to the run queue."""
    from src.core.scheduler import ProtocolScheduler
    from datetime import datetime
    ts = None
    if scheduled_at:
        ts = datetime.strptime(scheduled_at, "%Y-%m-%d %H:%M").timestamp()
    sched = ProtocolScheduler(config=ctx.obj["config"], simulate=simulate)
    job_id = sched.submit(protocol, scheduled_for=ts, priority=priority)
    console.print(f"[green]Job submitted: {job_id}[/green]")


@schedule.command("list")
@click.option("--status", "status_filter", default=None)
@click.pass_context
def schedule_list(ctx, status_filter) -> None:
    """List scheduled jobs."""
    from src.core.scheduler import ProtocolScheduler
    sched = ProtocolScheduler(config=ctx.obj["config"])
    jobs = sched.list_jobs(status_filter=status_filter)
    if not jobs:
        console.print("[dim]No jobs.[/dim]")
        return
    for j in jobs:
        console.print(f"  {j.job_id}  priority={j.priority}  status={j.status}  protocol={j.protocol_path}")


@schedule.command("cancel")
@click.argument("job_id")
@click.pass_context
def schedule_cancel(ctx, job_id) -> None:
    """Cancel a queued job."""
    from src.core.scheduler import ProtocolScheduler
    sched = ProtocolScheduler(config=ctx.obj["config"])
    ok = sched.cancel(job_id)
    console.print("[green]Cancelled.[/green]" if ok else "[red]Could not cancel.[/red]")


# ── stock ─────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--low-only", is_flag=True, help="Show only low-stock items")
@click.pass_context
def stock(ctx, low_only: bool) -> None:
    """Show inventory stock report."""
    from src.inventory.stock_manager import StockManager
    sm = StockManager()
    inventory_path = Path("inventory.yaml")
    if inventory_path.exists():
        sm.load(inventory_path)
    sm.print_stock_report(low_only=low_only)


# ── generate ──────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("description")
@click.option("--equipment", default=None, help="Comma-separated equipment list")
@click.option("--safety-level", default="BSL1", show_default=True)
@click.option("--model", default="claude-opus-4-6", show_default=True)
@click.option("--out", type=click.Path(path_type=Path), default=None,
              help="Save generated protocol YAML to file")
@click.pass_context
def generate(ctx, description, equipment, safety_level, model, out) -> None:
    """Generate a protocol YAML from a natural-language description (requires ANTHROPIC_API_KEY)."""
    from src.nlp.protocol_generator import ProtocolGenerator
    api_key = os.getenv("ANTHROPIC_API_KEY")
    equip_list = [e.strip() for e in equipment.split(",")] if equipment else None
    gen = ProtocolGenerator(api_key=api_key, model=model)
    console.print(f"[cyan]Generating protocol…[/cyan]")
    result = gen.generate(description, equipment_available=equip_list, safety_level=safety_level)
    if result.success:
        console.print(f"[green]Generated {len(result.yaml_string)} chars of YAML "
                      f"({result.tokens_used} tokens).[/green]")
        if out:
            out.write_text(result.yaml_string)
            console.print(f"[green]Saved to {out}[/green]")
        else:
            console.print(result.yaml_string)
    else:
        console.print(f"[red]Generation failed: {result.error}[/red]")
        sys.exit(1)


# ── notify ────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--severity", default="info", show_default=True,
              type=click.Choice(["info", "warning", "critical"]))
@click.pass_context
def notify(ctx, severity) -> None:
    """Send a test notification to all configured backends."""
    from src.notify.notifier import Notifier
    n = Notifier.from_config(ctx.obj["config"])
    results = n.notify("test", "OpenLab Test Notification",
                       "This is a test notification from the CLI.", severity=severity)
    console.print(f"[green]Notification sent to {len(results)} backend(s): {results}[/green]")


# ── audit ─────────────────────────────────────────────────────────────────────

@cli.group()
@click.pass_context
def audit(ctx) -> None:
    """View and verify the audit trail."""


@audit.command("show")
@click.option("--last", default=20, show_default=True, type=int)
@click.option("--action", default=None)
@click.option("--resource", default=None)
@click.pass_context
def audit_show(ctx, last, action, resource) -> None:
    """Show recent audit entries."""
    from src.audit.audit_trail import AuditTrail
    import time as _time
    trail_path = Path("runs/audit.jsonl")
    trail = AuditTrail(trail_path if trail_path.exists() else None)
    entries = trail.search(action=action, resource=resource)[-last:]
    if not entries:
        console.print("[dim]No audit entries.[/dim]")
        return
    for e in entries:
        ts = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(e.timestamp))
        console.print(f"  [{ts}] {e.operator:12s} {e.action:25s} {e.resource}")


@audit.command("verify")
@click.pass_context
def audit_verify(ctx) -> None:
    """Verify the integrity of the audit trail."""
    from src.audit.audit_trail import AuditTrail
    trail_path = Path("runs/audit.jsonl")
    if not trail_path.exists():
        console.print("[dim]No audit trail found.[/dim]")
        return
    trail = AuditTrail(trail_path)
    ok, violations = trail.verify_integrity()
    if ok:
        console.print(f"[green]Audit trail OK – {trail.entry_count} entries verified.[/green]")
    else:
        console.print(f"[red bold]INTEGRITY VIOLATIONS DETECTED:[/red bold]")
        for v in violations:
            console.print(f"  [red]• {v}[/red]")
        sys.exit(1)


@audit.command("export")
@click.argument("output", type=click.Path(path_type=Path))
@click.option("--format", "fmt", default="html", type=click.Choice(["html", "csv"]))
@click.pass_context
def audit_export(ctx, output, fmt) -> None:
    """Export audit trail to HTML or CSV."""
    from src.audit.audit_trail import AuditTrail
    trail_path = Path("runs/audit.jsonl")
    trail = AuditTrail(trail_path if trail_path.exists() else None)
    trail.export_report(Path(output), fmt=fmt)
    console.print(f"[green]Audit report exported to {output}.[/green]")


# ── resume ────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("run_id", required=False)
@click.argument("protocol", required=False, type=click.Path(path_type=Path))
@click.option("--list", "list_only", is_flag=True, help="List resumable runs")
@click.option("--simulate/--no-simulate",
              default=os.getenv("SIMULATION_MODE", "true").lower() == "true")
@click.pass_context
def resume(ctx, run_id, protocol, list_only, simulate) -> None:
    """Resume a protocol run from the last checkpoint."""
    from src.core.recovery import RecoveryManager
    rm = RecoveryManager()

    if list_only:
        cps = rm.list_checkpoints()
        if not cps:
            console.print("[dim]No resumable checkpoints.[/dim]")
            return
        for cp in cps:
            console.print(f"  {cp.run_id:40s} steps done: {len(cp.completed_step_ids)}")
        return

    if not run_id or not protocol:
        console.print("[red]Provide RUN_ID and PROTOCOL, or use --list.[/red]")
        sys.exit(1)

    cp = rm.load_checkpoint(run_id)
    if not cp:
        console.print(f"[red]No checkpoint found for run '{run_id}'.[/red]")
        sys.exit(1)

    from src.core.protocol_parser import ProtocolParser
    from src.core.orchestrator import Orchestrator
    doc = ProtocolParser().load(protocol)
    orch = Orchestrator(config=ctx.obj["config"], simulate=simulate)
    record = rm.resume_from_checkpoint(doc, cp, orch)
    console.print(f"[green]Resumed run finished: {record.status}[/green]")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli(obj={})
