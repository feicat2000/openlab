"""
Diagnostic Runner – orchestrates all checks, renders results, and exports reports.

Usage (CLI via main.py):
    python main.py diagnose                           # full suite
    python main.py diagnose --category Network        # filter by category
    python main.py diagnose --quick                   # skip slow tests
    python main.py diagnose --export report.json      # JSON export
    python main.py diagnose --export report.html      # HTML report

Usage (programmatic):
    runner  = DiagnosticRunner(config, project_root)
    results = runner.run()
    runner.print_report(results)
    runner.export(results, Path("report.json"))
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from jinja2 import Environment
from loguru import logger
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .checks import (
    CheckResult, CheckStatus,
    check_config_files, check_dialout_group, check_dot_env,
    check_example_protocol, check_gpio_available,
    check_hardware_config, check_opentrons_http, check_opentrons_pipettes,
    check_os_compatibility, check_python_packages, check_python_version,
    check_relay_board, check_serial_device, check_serial_ports_available,
    check_simulation_run, check_system_resources, check_tcp_port,
    check_ur_dashboard, check_ur_rtde, check_venv_active,
)


REQUIRED_PACKAGES = [
    "yaml", "pydantic", "rich", "click", "dotenv",
    "loguru", "fastapi", "uvicorn", "serial",
    "pandas", "jinja2", "anthropic", "apscheduler", "psutil",
]
OPTIONAL_PACKAGES = [
    "rtde_control", "rtde_receive", "dashboard_client",
]


class DiagnosticRunner:
    """
    Runs all diagnostic checks in category order and reports results.

    Parameters
    ──────────
    config       : dict loaded from hardware_config.yaml
    project_root : Path to project root directory
    simulate     : if True, skip live hardware tests
    """

    def __init__(
        self,
        config: dict,
        project_root: Path,
        simulate: bool = True,
    ) -> None:
        self._cfg      = config
        self._root     = project_root
        self._simulate = simulate
        self._console  = Console()

    # ── Run ───────────────────────────────────────────────────────────────────

    def run(
        self,
        categories: Optional[List[str]] = None,
        quick: bool = False,
    ) -> List[CheckResult]:
        """
        Execute all checks (or filtered by category list).
        Returns list of CheckResult in execution order.
        """
        checks_to_run = self._build_check_list(quick=quick)
        results: List[CheckResult] = []

        self._console.print(
            "\n[bold cyan]OpenLab Diagnostic Suite[/bold cyan] – "
            f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        )

        for check_fn, args, kwargs in checks_to_run:
            try:
                result: CheckResult = check_fn(*args, **kwargs)
            except Exception as exc:
                result = CheckResult(
                    name=getattr(check_fn, "__name__", "unknown"),
                    category="Internal",
                    status=CheckStatus.FAIL,
                    message=f"Check raised exception: {exc}",
                    detail={"traceback": str(exc)},
                )

            # Category filter
            if categories and result.category not in categories:
                continue

            results.append(result)
            self._print_live_result(result)

        return results

    # ── Display ───────────────────────────────────────────────────────────────

    def print_report(self, results: List[CheckResult]) -> None:
        """Print grouped summary table."""
        self._console.print()

        # Group by category
        by_cat: Dict[str, List[CheckResult]] = {}
        for r in results:
            by_cat.setdefault(r.category, []).append(r)

        STATUS_COLOUR = {
            CheckStatus.PASS: "green",
            CheckStatus.WARN: "yellow",
            CheckStatus.FAIL: "red bold",
            CheckStatus.SKIP: "dim",
        }

        for cat, cat_results in by_cat.items():
            tbl = Table(title=f"[bold]{cat}[/bold]", show_lines=False, box=None,
                        title_justify="left", padding=(0, 1))
            tbl.add_column("", width=2)
            tbl.add_column("Check",    style="white",  min_width=28)
            tbl.add_column("Status",   min_width=6)
            tbl.add_column("Message",  style="white",  ratio=1)
            tbl.add_column("Time(s)",  justify="right", style="dim", min_width=7)

            for r in cat_results:
                colour = STATUS_COLOUR[r.status]
                tbl.add_row(
                    f"[{colour}]{r.icon}[/{colour}]",
                    r.name,
                    f"[{colour}]{r.status}[/{colour}]",
                    r.message,
                    f"{r.duration:.2f}",
                )
            self._console.print(tbl)
            self._console.print()

        # Summary bar
        counts = {s: sum(1 for r in results if r.status == s) for s in CheckStatus}
        self._console.print(
            f"[bold]Summary:[/bold]  "
            f"[green]✓ {counts[CheckStatus.PASS]} PASS[/green]  "
            f"[yellow]⚠ {counts[CheckStatus.WARN]} WARN[/yellow]  "
            f"[red]✗ {counts[CheckStatus.FAIL]} FAIL[/red]  "
            f"[dim]– {counts[CheckStatus.SKIP]} SKIP[/dim]  "
            f"  (total {len(results)}, {sum(r.duration for r in results):.1f}s)\n"
        )

        # Action items
        fails = [r for r in results if r.status == CheckStatus.FAIL]
        warns = [r for r in results if r.status == CheckStatus.WARN]
        if fails:
            self._console.print("[red bold]Action Required:[/red bold]")
            for r in fails:
                self._console.print(f"  [red]✗[/red]  {r.name}: {r.message}")
        if warns:
            self._console.print("[yellow]Warnings:[/yellow]")
            for r in warns:
                self._console.print(f"  [yellow]⚠[/yellow]  {r.name}: {r.message}")

    def _print_live_result(self, r: CheckResult) -> None:
        """Print a single result line as each check completes."""
        COLOUR = {
            CheckStatus.PASS: "green",
            CheckStatus.WARN: "yellow",
            CheckStatus.FAIL: "red",
            CheckStatus.SKIP: "dim",
        }
        c = COLOUR[r.status]
        self._console.print(
            f"  [{c}]{r.icon}[/{c}]  [dim]{r.category:<18}[/dim]"
            f"  {r.name:<38}  [{c}]{r.status}[/{c}]  {r.message[:60]}"
        )

    # ── Export ────────────────────────────────────────────────────────────────

    def export(self, results: List[CheckResult], path: Path) -> None:
        suffix = path.suffix.lower()
        if suffix == ".json":
            self._export_json(results, path)
        elif suffix == ".html":
            self._export_html(results, path)
        else:
            self._export_json(results, path)
        logger.info(f"Diagnostic report exported to {path}")
        self._console.print(f"[green]Report exported to {path}[/green]")

    def _export_json(self, results: List[CheckResult], path: Path) -> None:
        report = {
            "timestamp":    time.strftime("%Y-%m-%dT%H:%M:%S"),
            "project_root": str(self._root),
            "simulate":     self._simulate,
            "summary": {
                "pass": sum(1 for r in results if r.status == CheckStatus.PASS),
                "warn": sum(1 for r in results if r.status == CheckStatus.WARN),
                "fail": sum(1 for r in results if r.status == CheckStatus.FAIL),
                "skip": sum(1 for r in results if r.status == CheckStatus.SKIP),
                "total": len(results),
                "duration_s": round(sum(r.duration for r in results), 2),
            },
            "checks": [asdict(r) for r in results],
        }
        with open(path, "w") as f:
            json.dump(report, f, indent=2, default=str)

    def _export_html(self, results: List[CheckResult], path: Path) -> None:
        STATUS_COLOUR = {
            "PASS": "#22c55e", "WARN": "#f59e0b",
            "FAIL": "#ef4444", "SKIP": "#6b7280",
        }
        template_str = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>OpenLab Diagnostic Report – {{ timestamp }}</title>
  <style>
    body{font-family:monospace;background:#0f172a;color:#e2e8f0;padding:2rem;max-width:1100px;margin:auto}
    h1{color:#38bdf8;border-bottom:1px solid #334155;padding-bottom:.5rem}
    h2{color:#94a3b8;margin-top:2rem;font-size:1rem;text-transform:uppercase;letter-spacing:.1em}
    table{width:100%;border-collapse:collapse;margin:.5rem 0}
    th{background:#1e293b;padding:.4rem .8rem;text-align:left;font-size:.8rem;color:#94a3b8}
    td{padding:.35rem .8rem;border-bottom:1px solid #1e293b;font-size:.85rem}
    tr:hover td{background:#1e293b44}
    .PASS{color:#22c55e} .WARN{color:#f59e0b} .FAIL{color:#ef4444} .SKIP{color:#6b7280}
    .summary{display:flex;gap:2rem;margin:1.5rem 0;padding:1rem;background:#1e293b;border-radius:.5rem}
    .sum-item{text-align:center} .sum-val{font-size:2rem;font-weight:bold} .sum-lbl{font-size:.8rem;color:#94a3b8}
    .detail{font-size:.75rem;color:#64748b;max-width:300px;word-break:break-all}
  </style>
</head>
<body>
<h1>OpenLab Diagnostic Report</h1>
<p style="color:#64748b">{{ timestamp }} | Project: {{ project_root }} | Simulate: {{ simulate }}</p>

<div class="summary">
  {% for key, cls in [("pass","PASS"),("warn","WARN"),("fail","FAIL"),("skip","SKIP")] %}
  <div class="sum-item">
    <div class="sum-val {{ cls }}">{{ summary[key] }}</div>
    <div class="sum-lbl">{{ cls }}</div>
  </div>
  {% endfor %}
  <div class="sum-item">
    <div class="sum-val" style="color:#38bdf8">{{ summary.total }}</div>
    <div class="sum-lbl">TOTAL</div>
  </div>
  <div class="sum-item">
    <div class="sum-val" style="color:#94a3b8">{{ "%.1f"|format(summary.duration_s) }}s</div>
    <div class="sum-lbl">DURATION</div>
  </div>
</div>

{% for cat, cat_checks in by_category.items() %}
<h2>{{ cat }}</h2>
<table>
  <tr><th>Status</th><th>Check</th><th>Message</th><th>Time(s)</th><th>Detail</th></tr>
  {% for r in cat_checks %}
  <tr>
    <td class="{{ r.status }}">{{ icons[r.status] }} {{ r.status }}</td>
    <td>{{ r.name }}</td>
    <td>{{ r.message }}</td>
    <td>{{ "%.2f"|format(r.duration) }}</td>
    <td class="detail">{{ r.detail | string | truncate(80) if r.detail else "" }}</td>
  </tr>
  {% endfor %}
</table>
{% endfor %}
</body></html>"""
        by_cat: Dict[str, List] = {}
        for r in results:
            by_cat.setdefault(r.category, []).append(r)

        summary = {
            "pass": sum(1 for r in results if r.status == CheckStatus.PASS),
            "warn": sum(1 for r in results if r.status == CheckStatus.WARN),
            "fail": sum(1 for r in results if r.status == CheckStatus.FAIL),
            "skip": sum(1 for r in results if r.status == CheckStatus.SKIP),
            "total": len(results),
            "duration_s": sum(r.duration for r in results),
        }

        env  = Environment()
        tmpl = env.from_string(template_str)
        html = tmpl.render(
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
            project_root=str(self._root),
            simulate=self._simulate,
            summary=summary,
            by_category=by_cat,
            icons={"PASS": "✓", "WARN": "⚠", "FAIL": "✗", "SKIP": "–"},
        )
        with open(path, "w") as f:
            f.write(html)

    # ── Check list builder ────────────────────────────────────────────────────

    def _build_check_list(self, quick: bool) -> list:
        """Return ordered list of (fn, args, kwargs) tuples."""
        cfg  = self._cfg
        root = self._root

        ot_ip   = cfg.get("opentrons",   {}).get("ip",  os.getenv("OPENTRONS_HOST", ""))
        ot_port = cfg.get("opentrons",   {}).get("port", 31950)
        ur_ip   = cfg.get("robotic_arm", {}).get("ip",  os.getenv("UR_HOST", ""))

        vortex_port  = cfg.get("vortex",     {}).get("port", os.getenv("VORTEX_PORT", ""))
        cent_port    = cfg.get("centrifuge",  {}).get("port", os.getenv("CENTRIFUGE_PORT", ""))
        inc_port     = cfg.get("incubator",   {}).get("port", os.getenv("INCUBATOR_PORT", ""))
        relay_port   = cfg.get("magnetic_separator", {}).get("relay_port",
                               os.getenv("RELAY_PORT", ""))
        estop_pin    = cfg.get("safety", {}).get("estop_gpio_pin",
                               int(os.getenv("EMERGENCY_STOP_GPIO_PIN", 0) or 0))

        checks = [
            # ── Environment ──────────────────────────────────────────────────
            (check_python_version,   [], {}),
            (check_venv_active,      [], {}),
            (check_python_packages,  [REQUIRED_PACKAGES, OPTIONAL_PACKAGES], {}),
            (check_os_compatibility, [], {}),
            (check_system_resources, [], {}),

            # ── Config ───────────────────────────────────────────────────────
            (check_config_files,    [root],                             {}),
            (check_dot_env,         [root / ".env"],                    {}),
            (check_hardware_config, [root / "config" / "hardware_config.yaml"], {}),

            # ── Permissions ───────────────────────────────────────────────────
            (check_dialout_group,   [], {}),

            # ── Serial ports ──────────────────────────────────────────────────
            (check_serial_ports_available, [], {}),
        ]

        if not self._simulate:
            checks += [
                # ── Opentrons ─────────────────────────────────────────────────
                (check_opentrons_http,     [ot_ip, ot_port], {}),
                (check_opentrons_pipettes, [ot_ip, ot_port], {}),

                # ── UR Arm ────────────────────────────────────────────────────
                (check_tcp_port,    [ur_ip, 30004, "UR RTDE"], {}),
                (check_tcp_port,    [ur_ip, 29999, "UR Dashboard"], {}),
                (check_ur_rtde,     [ur_ip], {}),
                (check_ur_dashboard,[ur_ip], {}),

                # ── Serial devices ────────────────────────────────────────────
                (check_serial_device, [vortex_port, 9600, "Vortex"], {}),
                (check_serial_device, [cent_port,   9600, "Centrifuge"], {}),
                (check_serial_device, [inc_port,    9600, "Incubator"], {}),
                (check_relay_board,   [relay_port], {}),
                (check_gpio_available,[estop_pin], {}),
            ]

        # ── Protocols & platform ──────────────────────────────────────────────
        checks += [
            (check_example_protocol, [root / "protocols"], {}),
        ]

        if not quick:
            checks.append(
                (check_simulation_run, [root / "protocols"], {})
            )

        return checks
