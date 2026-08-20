"""
Diagnostic check primitives.

Each check function returns a CheckResult dataclass with:
  name      – short identifier
  category  – subsystem grouping
  status    – PASS | WARN | FAIL | SKIP
  message   – human-readable detail
  detail    – extended raw output (dict, str, None)
  duration  – seconds taken to run the check
"""
from __future__ import annotations

import importlib
import os
import platform
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


class CheckStatus(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass
class CheckResult:
    name:      str
    category:  str
    status:    CheckStatus
    message:   str
    detail:    Any            = None
    duration:  float          = 0.0

    @property
    def icon(self) -> str:
        return {"PASS": "✓", "WARN": "⚠", "FAIL": "✗", "SKIP": "–"}[self.status]


def _timed(fn):
    """Decorator: inject duration into CheckResult."""
    def wrapper(*args, **kwargs) -> CheckResult:
        t0 = time.perf_counter()
        result: CheckResult = fn(*args, **kwargs)
        result.duration = time.perf_counter() - t0
        return result
    wrapper.__name__ = fn.__name__
    return wrapper


# ── Python environment ────────────────────────────────────────────────────────

@_timed
def check_python_version(min_major=3, min_minor=10) -> CheckResult:
    import sys
    v = sys.version_info
    ok = (v.major, v.minor) >= (min_major, min_minor)
    return CheckResult(
        name="python_version",
        category="Environment",
        status=CheckStatus.PASS if ok else CheckStatus.FAIL,
        message=f"Python {v.major}.{v.minor}.{v.micro}"
                + ("" if ok else f" – need >= {min_major}.{min_minor}"),
        detail={"version": str(v), "executable": sys.executable},
    )


@_timed
def check_python_packages(required: List[str], optional: List[str]) -> CheckResult:
    missing_req  = []
    missing_opt  = []
    installed    = {}

    for pkg in required:
        mod = pkg.replace("-", "_").split(">=")[0].split("==")[0]
        try:
            m = importlib.import_module(mod)
            installed[pkg] = getattr(m, "__version__", "?")
        except ImportError:
            missing_req.append(pkg)

    for pkg in optional:
        mod = pkg.replace("-", "_")
        try:
            m = importlib.import_module(mod)
            installed[pkg] = getattr(m, "__version__", "?")
        except ImportError:
            missing_opt.append(pkg)

    if missing_req:
        status  = CheckStatus.FAIL
        message = f"Missing required packages: {missing_req}"
    elif missing_opt:
        status  = CheckStatus.WARN
        message = f"Missing optional packages (hardware drivers): {missing_opt}"
    else:
        status  = CheckStatus.PASS
        message = f"All {len(required)} required + {len(optional)} optional packages OK"

    return CheckResult(
        name="python_packages",
        category="Environment",
        status=status,
        message=message,
        detail={"installed": installed, "missing_required": missing_req,
                "missing_optional": missing_opt},
    )


@_timed
def check_venv_active() -> CheckResult:
    import sys
    venv = os.environ.get("VIRTUAL_ENV") or sys.prefix
    active = "venv" in venv.lower() or ".venv" in venv
    return CheckResult(
        name="venv_active",
        category="Environment",
        status=CheckStatus.PASS if active else CheckStatus.WARN,
        message=f"Venv: {venv}" if active else "No virtualenv active – run 'source activate_openlab.sh'",
        detail={"VIRTUAL_ENV": os.environ.get("VIRTUAL_ENV"), "sys.prefix": venv},
    )


# ── System resources ──────────────────────────────────────────────────────────

@_timed
def check_system_resources(
    min_ram_gb: float = 4.0,
    min_disk_gb: float = 10.0,
) -> CheckResult:
    try:
        import psutil
        mem  = psutil.virtual_memory()
        disk = psutil.disk_usage(".")
        cpu  = psutil.cpu_percent(interval=0.5)
        ram_gb  = mem.available / 1e9
        disk_gb = disk.free / 1e9

        issues = []
        if ram_gb  < min_ram_gb:  issues.append(f"Low RAM: {ram_gb:.1f}GB free")
        if disk_gb < min_disk_gb: issues.append(f"Low disk: {disk_gb:.1f}GB free")
        if cpu     > 80:          issues.append(f"High CPU: {cpu:.0f}%")

        status  = CheckStatus.WARN if issues else CheckStatus.PASS
        message = "; ".join(issues) if issues else (
            f"RAM {ram_gb:.1f}GB free, Disk {disk_gb:.1f}GB free, CPU {cpu:.0f}%"
        )
        detail  = {"ram_available_gb": round(ram_gb, 2),
                   "disk_free_gb": round(disk_gb, 2),
                   "cpu_percent": cpu,
                   "platform": platform.platform()}
    except ImportError:
        status  = CheckStatus.WARN
        message = "psutil not installed – resource check skipped"
        detail  = None

    return CheckResult("system_resources", "System", status, message, detail)


@_timed
def check_os_compatibility() -> CheckResult:
    os_name = platform.system()
    ok = os_name in ("Linux", "Darwin")
    return CheckResult(
        name="os_compatibility",
        category="System",
        status=CheckStatus.PASS if ok else CheckStatus.WARN,
        message=f"OS: {os_name} {platform.release()}"
                + ("" if ok else " – Windows: use WSL2 for serial devices"),
        detail={"os": os_name, "release": platform.release(), "arch": platform.machine()},
    )


# ── Config files ──────────────────────────────────────────────────────────────

@_timed
def check_config_files(project_root: Path) -> CheckResult:
    required = [
        project_root / "config" / "hardware_config.yaml",
        project_root / ".env",
    ]
    recommended = [
        project_root / "config" / "safety_config.yaml",
    ]
    missing_req = [str(f) for f in required    if not f.exists()]
    missing_rec = [str(f) for f in recommended if not f.exists()]

    if missing_req:
        status  = CheckStatus.FAIL
        message = f"Missing required config files: {missing_req}"
    elif missing_rec:
        status  = CheckStatus.WARN
        message = f"Missing recommended config: {missing_rec}"
    else:
        status  = CheckStatus.PASS
        message = "All config files present"

    return CheckResult("config_files", "Configuration", status, message,
                       detail={"missing_required": missing_req,
                               "missing_recommended": missing_rec})


@_timed
def check_hardware_config(config_path: Path) -> CheckResult:
    try:
        import yaml
        with open(config_path) as f:
            cfg = yaml.safe_load(f)

        issues = []
        warnings = []

        sim = cfg.get("simulation_mode", True)
        if sim:
            warnings.append("simulation_mode=true – no real hardware will be used")

        ot_ip = cfg.get("opentrons", {}).get("ip", "")
        if not ot_ip or "xx" in ot_ip:
            warnings.append("Opentrons IP not configured (still placeholder)")

        arm_ip = cfg.get("robotic_arm", {}).get("ip", "")
        if not arm_ip or arm_ip == "192.168.1.10":
            warnings.append("UR arm IP may be default – verify network")

        positions = cfg.get("arm_positions", {})
        expected_keys = ["home", "opentrons_handoff", "vortex", "centrifuge_load"]
        missing_pos = [k for k in expected_keys if k not in positions]
        if missing_pos:
            warnings.append(f"Missing arm positions: {missing_pos}")

        if issues:
            status = CheckStatus.FAIL
        elif warnings:
            status = CheckStatus.WARN
        else:
            status = CheckStatus.PASS

        message = "; ".join(warnings) if warnings else "Hardware config valid"
        return CheckResult("hardware_config", "Configuration", status, message,
                           detail={"simulation_mode": sim, "warnings": warnings})

    except Exception as exc:
        return CheckResult("hardware_config", "Configuration", CheckStatus.FAIL,
                           f"Config parse error: {exc}")


@_timed
def check_dot_env(env_path: Path) -> CheckResult:
    if not env_path.exists():
        return CheckResult("dot_env", "Configuration", CheckStatus.WARN,
                           ".env file not found – run setup.sh to generate it")
    issues = []
    with open(env_path) as f:
        content = f.read()
    placeholders = ["169.254.xx.xx", "sk-ant-...", "192.168.1.10"]
    for ph in placeholders:
        if ph in content:
            issues.append(f"Placeholder not replaced: '{ph}'")
    status  = CheckStatus.WARN if issues else CheckStatus.PASS
    message = "; ".join(issues) if issues else ".env configured"
    return CheckResult("dot_env", "Configuration", status, message,
                       detail={"placeholders_found": issues})


# ── Network connectivity ──────────────────────────────────────────────────────

@_timed
def check_tcp_port(
    host: str, port: int, name: str, category: str = "Network", timeout: float = 3.0
) -> CheckResult:
    if not host or "xx" in host:
        return CheckResult(
            name=f"tcp_{name.lower().replace(' ', '_')}",
            category=category,
            status=CheckStatus.SKIP,
            message=f"{name}: IP not configured",
        )
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return CheckResult(
            name=f"tcp_{name.lower().replace(' ', '_')}",
            category=category,
            status=CheckStatus.PASS,
            message=f"{name} reachable at {host}:{port}",
            detail={"host": host, "port": port},
        )
    except (socket.timeout, ConnectionRefusedError, OSError) as exc:
        return CheckResult(
            name=f"tcp_{name.lower().replace(' ', '_')}",
            category=category,
            status=CheckStatus.FAIL,
            message=f"{name} NOT reachable at {host}:{port} – {exc}",
            detail={"host": host, "port": port, "error": str(exc)},
        )


@_timed
def check_opentrons_http(host: str, port: int = 31950) -> CheckResult:
    if not host or "xx" in host:
        return CheckResult("opentrons_http", "Opentrons", CheckStatus.SKIP,
                           "Opentrons IP not configured")
    try:
        import requests
        r = requests.get(f"http://{host}:{port}/health", timeout=5)
        r.raise_for_status()
        data = r.json()
        return CheckResult(
            "opentrons_http", "Opentrons", CheckStatus.PASS,
            f"OT health OK – model={data.get('robot_model','?')} "
            f"api={data.get('api_version','?')}",
            detail=data,
        )
    except Exception as exc:
        return CheckResult("opentrons_http", "Opentrons", CheckStatus.FAIL,
                           f"Opentrons HTTP health failed: {exc}",
                           detail={"error": str(exc)})


@_timed
def check_opentrons_pipettes(host: str, port: int = 31950) -> CheckResult:
    if not host or "xx" in host:
        return CheckResult("opentrons_pipettes", "Opentrons", CheckStatus.SKIP,
                           "Opentrons IP not configured")
    try:
        import requests
        r = requests.get(f"http://{host}:{port}/pipettes", timeout=5)
        r.raise_for_status()
        pipettes = r.json()
        mounted  = {k: v for k, v in pipettes.items() if v.get("model")}
        if not mounted:
            return CheckResult("opentrons_pipettes", "Opentrons", CheckStatus.WARN,
                               "No pipettes mounted", detail=pipettes)
        return CheckResult("opentrons_pipettes", "Opentrons", CheckStatus.PASS,
                           f"Pipettes: {list(mounted.keys())}", detail=mounted)
    except Exception as exc:
        return CheckResult("opentrons_pipettes", "Opentrons", CheckStatus.FAIL,
                           f"Pipette query failed: {exc}")


# ── Serial ports ──────────────────────────────────────────────────────────────

@_timed
def check_serial_ports_available() -> CheckResult:
    """List all /dev/ttyUSB* and /dev/ttyACM* ports."""
    ports = []
    if platform.system() == "Linux":
        import glob
        ports = glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")
    elif platform.system() == "Darwin":
        import glob
        ports = glob.glob("/dev/tty.usbserial*") + glob.glob("/dev/tty.usbmodem*")

    status  = CheckStatus.PASS if ports else CheckStatus.WARN
    message = f"Serial ports found: {ports}" if ports else "No USB serial ports detected"
    return CheckResult("serial_ports", "Serial Devices", status, message,
                       detail={"ports": ports})


@_timed
def check_serial_device(port: str, baud: int, name: str,
                         test_cmd: bytes = b"IN_NAME\r\n") -> CheckResult:
    if not port:
        return CheckResult(f"serial_{name}", "Serial Devices", CheckStatus.SKIP,
                           f"{name}: port not configured")
    if not Path(port).exists():
        return CheckResult(f"serial_{name}", "Serial Devices", CheckStatus.FAIL,
                           f"{name}: port {port} does not exist")
    try:
        import serial
        s = serial.Serial(port, baud, timeout=2)
        s.write(test_cmd)
        time.sleep(0.3)
        resp = s.read(s.in_waiting or 64)
        s.close()
        status  = CheckStatus.PASS if resp else CheckStatus.WARN
        message = f"{name} responded: {resp[:40]}" if resp else f"{name}: no response to test command"
        return CheckResult(f"serial_{name}", "Serial Devices", status, message,
                           detail={"port": port, "baud": baud, "response": repr(resp)})
    except Exception as exc:
        return CheckResult(f"serial_{name}", "Serial Devices", CheckStatus.FAIL,
                           f"{name} serial error: {exc}", detail={"error": str(exc)})


@_timed
def check_dialout_group() -> CheckResult:
    """Check if current user is in dialout group (Linux serial access)."""
    if platform.system() != "Linux":
        return CheckResult("dialout_group", "Permissions", CheckStatus.SKIP,
                           "Not Linux – group check skipped")
    import grp, pwd
    user   = os.getlogin()
    groups = [g.gr_name for g in grp.getgrall() if user in g.gr_mem]
    primary = pwd.getpwnam(user).pw_gid
    groups += [grp.getgrgid(primary).gr_name]
    in_dialout = "dialout" in groups
    return CheckResult(
        "dialout_group", "Permissions",
        CheckStatus.PASS if in_dialout else CheckStatus.WARN,
        f"User '{user}' {'is' if in_dialout else 'is NOT'} in dialout group"
        + ("" if in_dialout else " – run: sudo usermod -aG dialout $USER && newgrp dialout"),
        detail={"user": user, "groups": groups},
    )


# ── Relay board ───────────────────────────────────────────────────────────────

@_timed
def check_relay_board(port: str, channel_count: int = 8) -> CheckResult:
    if not port:
        return CheckResult("relay_board", "Peripherals", CheckStatus.SKIP,
                           "Relay port not configured")
    if not Path(port).exists():
        return CheckResult("relay_board", "Peripherals", CheckStatus.FAIL,
                           f"Relay port {port} not found")
    try:
        import serial
        s = serial.Serial(port, 9600, timeout=1)
        # Toggle relay 1 ON then OFF as a functional test
        s.write(bytes([0xA0, 1, 1, 0xA2]))
        time.sleep(0.2)
        s.write(bytes([0xA0, 1, 0, 0xA1]))
        s.close()
        return CheckResult("relay_board", "Peripherals", CheckStatus.PASS,
                           f"Relay board on {port} – toggled channel 1 OK",
                           detail={"port": port, "channels": channel_count})
    except Exception as exc:
        return CheckResult("relay_board", "Peripherals", CheckStatus.FAIL,
                           f"Relay board error: {exc}")


# ── UR arm ────────────────────────────────────────────────────────────────────

@_timed
def check_ur_rtde(host: str, port: int = 30004) -> CheckResult:
    if not host:
        return CheckResult("ur_rtde", "Robotic Arm", CheckStatus.SKIP,
                           "UR host not configured")
    try:
        import rtde_receive
        r = rtde_receive.RTDEReceiveInterface(host)
        mode = r.getRobotMode()
        r.disconnect()
        status  = CheckStatus.PASS if mode == 7 else CheckStatus.WARN
        message = f"UR arm RTDE connected – mode {mode}" + ("" if mode == 7 else " (not RUNNING)")
        return CheckResult("ur_rtde", "Robotic Arm", status, message,
                           detail={"host": host, "robot_mode": mode})
    except ImportError:
        # Fall back to raw TCP probe
        return check_tcp_port(host, port, "UR RTDE", "Robotic Arm")
    except Exception as exc:
        return CheckResult("ur_rtde", "Robotic Arm", CheckStatus.FAIL,
                           f"UR RTDE error: {exc}", detail={"error": str(exc)})


@_timed
def check_ur_dashboard(host: str) -> CheckResult:
    if not host:
        return CheckResult("ur_dashboard", "Robotic Arm", CheckStatus.SKIP,
                           "UR host not configured")
    try:
        import dashboard_client
        dc = dashboard_client.DashboardClient(host)
        dc.connect()
        mode = dc.getRobotMode()
        dc.disconnect()
        return CheckResult("ur_dashboard", "Robotic Arm", CheckStatus.PASS,
                           f"Dashboard OK – {mode}", detail={"mode": mode})
    except ImportError:
        return check_tcp_port(host, 29999, "UR Dashboard", "Robotic Arm")
    except Exception as exc:
        return CheckResult("ur_dashboard", "Robotic Arm", CheckStatus.FAIL,
                           f"Dashboard error: {exc}")


# ── GPIO ──────────────────────────────────────────────────────────────────────

@_timed
def check_gpio_available(estop_pin: Optional[int]) -> CheckResult:
    if platform.machine() not in ("aarch64", "armv7l"):
        return CheckResult("gpio", "GPIO", CheckStatus.SKIP,
                           "Not running on ARM – GPIO not applicable")
    try:
        import RPi.GPIO as GPIO
        GPIO.setmode(GPIO.BCM)
        if estop_pin:
            GPIO.setup(estop_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            val = GPIO.input(estop_pin)
            GPIO.cleanup()
            pressed = (val == GPIO.LOW)
            return CheckResult(
                "gpio", "GPIO",
                CheckStatus.WARN if pressed else CheckStatus.PASS,
                f"GPIO OK – E-stop pin {estop_pin}={'PRESSED' if pressed else 'open'}",
                detail={"estop_pin": estop_pin, "pressed": pressed},
            )
        GPIO.cleanup()
        return CheckResult("gpio", "GPIO", CheckStatus.PASS, "GPIO module available")
    except ImportError:
        return CheckResult("gpio", "GPIO", CheckStatus.WARN,
                           "RPi.GPIO not installed")
    except Exception as exc:
        return CheckResult("gpio", "GPIO", CheckStatus.FAIL,
                           f"GPIO error: {exc}")


# ── Protocol validation ───────────────────────────────────────────────────────

@_timed
def check_example_protocol(protocols_dir: Path) -> CheckResult:
    example = protocols_dir / "dna_extraction.yaml"
    if not example.exists():
        return CheckResult("example_protocol", "Protocols", CheckStatus.WARN,
                           f"Example protocol not found at {example}")
    try:
        from src.core.protocol_parser import ProtocolParser
        doc = ProtocolParser().load(example)
        return CheckResult(
            "example_protocol", "Protocols", CheckStatus.PASS,
            f"Example protocol '{doc.metadata.name}' parsed OK "
            f"({len(doc.steps)} steps, {len(doc.reagents)} reagents)",
            detail={"name": doc.metadata.name, "steps": len(doc.steps),
                    "reagents": len(doc.reagents)},
        )
    except Exception as exc:
        return CheckResult("example_protocol", "Protocols", CheckStatus.FAIL,
                           f"Protocol parse error: {exc}")


# ── Simulation smoke test ─────────────────────────────────────────────────────

@_timed
def check_simulation_run(protocols_dir: Path) -> CheckResult:
    example = protocols_dir / "dna_extraction.yaml"
    if not example.exists():
        return CheckResult("simulation_run", "Platform", CheckStatus.SKIP,
                           "Example protocol not found – skipping simulation")
    try:
        import tempfile
        from src.core.protocol_parser import ProtocolParser
        from src.core.orchestrator import Orchestrator
        doc  = ProtocolParser().load(example)
        orch = Orchestrator(config={}, simulate=True)
        with tempfile.TemporaryDirectory() as td:
            rec = orch.execute(doc, runs_dir=Path(td),
                               confirm_before_run=False)
        completed = sum(1 for s in rec.steps if s.status == "completed")
        total     = len(rec.steps)
        ok        = rec.status == "completed" and completed == total
        return CheckResult(
            "simulation_run", "Platform",
            CheckStatus.PASS if ok else CheckStatus.FAIL,
            f"Simulation run: {rec.status} ({completed}/{total} steps completed)",
            detail={"run_id": rec.run_id, "status": rec.status,
                    "steps_done": completed, "errors": rec.errors},
        )
    except Exception as exc:
        return CheckResult("simulation_run", "Platform", CheckStatus.FAIL,
                           f"Simulation run crashed: {exc}",
                           detail={"error": str(exc)})
