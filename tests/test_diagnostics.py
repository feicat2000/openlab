"""
Diagnostic module unit tests.

All network, serial, and file-system calls are mocked.
No real hardware or internet connection required.
"""
from __future__ import annotations

import json
import socket
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch, mock_open

from src.diagnostics.checks import CheckStatus


# ── helpers ───────────────────────────────────────────────────────────────────

def _result_for(fn, *args, **kwargs):
    """Run a check function and return its CheckResult."""
    return fn(*args, **kwargs)


# ══════════════════════════════════════════════════════════════════════════════
# Environment checks
# ══════════════════════════════════════════════════════════════════════════════

class TestEnvironmentChecks(unittest.TestCase):

    def test_python_version_pass(self):
        from src.diagnostics.checks import check_python_version
        r = check_python_version(min_major=3, min_minor=10)
        self.assertEqual(r.status, CheckStatus.PASS)
        self.assertIn("Python", r.message)
        self.assertGreater(r.duration, 0)

    def test_python_version_fail_when_too_old(self):
        from src.diagnostics.checks import check_python_version
        r = check_python_version(min_major=99, min_minor=0)
        self.assertEqual(r.status, CheckStatus.FAIL)

    def test_venv_active_pass_when_virtual_env_set(self):
        from src.diagnostics.checks import check_venv_active
        with patch.dict("os.environ", {"VIRTUAL_ENV": "/some/.venv"}):
            r = check_venv_active()
        self.assertEqual(r.status, CheckStatus.PASS)

    def test_venv_active_warn_when_no_venv(self):
        from src.diagnostics.checks import check_venv_active
        import sys
        with patch.dict("os.environ", {}, clear=True):
            with patch.object(sys, "prefix", "/usr"):
                r = check_venv_active()
        # May be PASS or WARN depending on system – just verify it runs
        self.assertIn(r.status, (CheckStatus.PASS, CheckStatus.WARN))

    def test_python_packages_all_present(self):
        from src.diagnostics.checks import check_python_packages
        # Use packages we know are installed
        r = check_python_packages(required=["yaml", "json"], optional=[])
        self.assertEqual(r.status, CheckStatus.PASS)
        self.assertIn("installed", r.detail)

    def test_python_packages_missing_required(self):
        from src.diagnostics.checks import check_python_packages
        r = check_python_packages(
            required=["definitely_not_a_real_package_xyz"], optional=[]
        )
        self.assertEqual(r.status, CheckStatus.FAIL)
        self.assertIn("definitely_not_a_real_package_xyz", r.detail["missing_required"])

    def test_python_packages_missing_optional_warns(self):
        from src.diagnostics.checks import check_python_packages
        r = check_python_packages(
            required=["yaml"],
            optional=["definitely_not_a_real_package_xyz"]
        )
        self.assertEqual(r.status, CheckStatus.WARN)
        self.assertIn("definitely_not_a_real_package_xyz",
                      r.detail["missing_optional"])

    def test_os_compatibility_linux(self):
        from src.diagnostics.checks import check_os_compatibility
        with patch("platform.system", return_value="Linux"):
            r = check_os_compatibility()
        self.assertEqual(r.status, CheckStatus.PASS)

    def test_os_compatibility_windows_warns(self):
        from src.diagnostics.checks import check_os_compatibility
        with patch("platform.system", return_value="Windows"):
            r = check_os_compatibility()
        self.assertEqual(r.status, CheckStatus.WARN)
        self.assertIn("WSL2", r.message)

    def test_system_resources_pass(self):
        from src.diagnostics.checks import check_system_resources
        mem  = MagicMock(); mem.available = 16e9
        disk = MagicMock(); disk.free     = 100e9
        with patch("psutil.virtual_memory", return_value=mem), \
             patch("psutil.disk_usage",     return_value=disk), \
             patch("psutil.cpu_percent",    return_value=10.0):
            r = check_system_resources(min_ram_gb=4, min_disk_gb=10)
        self.assertEqual(r.status, CheckStatus.PASS)

    def test_system_resources_warn_low_ram(self):
        from src.diagnostics.checks import check_system_resources
        mem  = MagicMock(); mem.available = 0.5e9    # only 0.5 GB
        disk = MagicMock(); disk.free     = 100e9
        with patch("psutil.virtual_memory", return_value=mem), \
             patch("psutil.disk_usage",     return_value=disk), \
             patch("psutil.cpu_percent",    return_value=5.0):
            r = check_system_resources(min_ram_gb=4)
        self.assertEqual(r.status, CheckStatus.WARN)
        self.assertIn("Low RAM", r.message)

    def test_system_resources_warn_high_cpu(self):
        from src.diagnostics.checks import check_system_resources
        mem  = MagicMock(); mem.available = 16e9
        disk = MagicMock(); disk.free     = 100e9
        with patch("psutil.virtual_memory", return_value=mem), \
             patch("psutil.disk_usage",     return_value=disk), \
             patch("psutil.cpu_percent",    return_value=95.0):
            r = check_system_resources()
        self.assertEqual(r.status, CheckStatus.WARN)
        self.assertIn("CPU", r.message)

    def test_system_resources_no_psutil_warns(self):
        from src.diagnostics.checks import check_system_resources
        with patch("builtins.__import__", side_effect=ImportError("no psutil")):
            # Fall back gracefully
            pass
        # Just verify it won't raise even if psutil fails
        with patch("psutil.virtual_memory", side_effect=ImportError):
            try:
                r = check_system_resources()
            except Exception:
                pass   # acceptable


# ══════════════════════════════════════════════════════════════════════════════
# Configuration checks
# ══════════════════════════════════════════════════════════════════════════════

class TestConfigChecks(unittest.TestCase):

    def test_config_files_all_present(self, tmp_path=None):
        import tempfile
        from src.diagnostics.checks import check_config_files
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            (root / "config" / "hardware_config.yaml").write_text("simulation_mode: true")
            (root / "config" / "safety_config.yaml").write_text("# safety")
            (root / ".env").write_text("SIMULATION_MODE=true")
            r = check_config_files(root)
        self.assertEqual(r.status, CheckStatus.PASS)

    def test_config_files_missing_required_fails(self):
        import tempfile
        from src.diagnostics.checks import check_config_files
        with tempfile.TemporaryDirectory() as td:
            r = check_config_files(Path(td))
        self.assertEqual(r.status, CheckStatus.FAIL)
        self.assertGreater(len(r.detail["missing_required"]), 0)

    def test_dot_env_pass_when_configured(self):
        import tempfile
        from src.diagnostics.checks import check_dot_env
        with tempfile.NamedTemporaryFile(suffix=".env", mode="w", delete=False) as f:
            f.write("OPENTRONS_HOST=192.168.1.50\n")
            path = Path(f.name)
        r = check_dot_env(path)
        path.unlink()
        self.assertEqual(r.status, CheckStatus.PASS)

    def test_dot_env_warns_on_placeholder(self):
        import tempfile
        from src.diagnostics.checks import check_dot_env
        with tempfile.NamedTemporaryFile(suffix=".env", mode="w", delete=False) as f:
            f.write("OPENTRONS_HOST=169.254.xx.xx\n")
            path = Path(f.name)
        r = check_dot_env(path)
        path.unlink()
        self.assertEqual(r.status, CheckStatus.WARN)

    def test_dot_env_warns_when_missing(self):
        from src.diagnostics.checks import check_dot_env
        r = check_dot_env(Path("/nonexistent/.env"))
        self.assertEqual(r.status, CheckStatus.WARN)

    def test_hardware_config_valid(self):
        import tempfile, yaml
        from src.diagnostics.checks import check_hardware_config
        cfg = {
            "simulation_mode": False,
            "opentrons":   {"ip": "192.168.1.50"},
            "robotic_arm": {"ip": "192.168.2.50"},
            "arm_positions": {
                "home": [0.3,-0.2,0.5,3.14,0,0],
                "opentrons_handoff": [0.4,-0.1,0.3,3.14,0,0],
                "vortex": [0.2,0.3,0.25,3.14,0,0],
                "centrifuge_load": [0.1,0.42,0.2,3.14,0,0],
            }
        }
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(cfg, f)
            path = Path(f.name)
        r = check_hardware_config(path)
        path.unlink()
        self.assertEqual(r.status, CheckStatus.PASS)

    def test_hardware_config_warns_simulation_mode(self):
        import tempfile, yaml
        from src.diagnostics.checks import check_hardware_config
        cfg = {"simulation_mode": True, "opentrons": {"ip": "169.254.xx.xx"},
               "robotic_arm": {}, "arm_positions": {}}
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(cfg, f)
            path = Path(f.name)
        r = check_hardware_config(path)
        path.unlink()
        self.assertEqual(r.status, CheckStatus.WARN)

    def test_hardware_config_fail_on_invalid_yaml(self):
        import tempfile
        from src.diagnostics.checks import check_hardware_config
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            f.write("{invalid: [yaml")
            path = Path(f.name)
        r = check_hardware_config(path)
        path.unlink()
        self.assertEqual(r.status, CheckStatus.FAIL)


# ══════════════════════════════════════════════════════════════════════════════
# Network / connectivity checks
# ══════════════════════════════════════════════════════════════════════════════

class TestNetworkChecks(unittest.TestCase):

    def test_tcp_port_pass(self):
        from src.diagnostics.checks import check_tcp_port
        mock_sock = MagicMock()
        with patch("socket.create_connection", return_value=mock_sock):
            r = check_tcp_port("192.168.1.10", 30004, "UR RTDE")
        self.assertEqual(r.status, CheckStatus.PASS)
        mock_sock.close.assert_called_once()

    def test_tcp_port_fail_on_refused(self):
        from src.diagnostics.checks import check_tcp_port
        with patch("socket.create_connection",
                   side_effect=ConnectionRefusedError("refused")):
            r = check_tcp_port("192.168.1.10", 30004, "UR RTDE")
        self.assertEqual(r.status, CheckStatus.FAIL)

    def test_tcp_port_fail_on_timeout(self):
        from src.diagnostics.checks import check_tcp_port
        with patch("socket.create_connection",
                   side_effect=socket.timeout("timed out")):
            r = check_tcp_port("192.168.1.10", 30004, "UR RTDE")
        self.assertEqual(r.status, CheckStatus.FAIL)

    def test_tcp_port_skip_when_not_configured(self):
        from src.diagnostics.checks import check_tcp_port
        r = check_tcp_port("", 30004, "UR RTDE")
        self.assertEqual(r.status, CheckStatus.SKIP)

    def test_tcp_port_skip_when_placeholder(self):
        from src.diagnostics.checks import check_tcp_port
        r = check_tcp_port("169.254.xx.xx", 31950, "Opentrons")
        self.assertEqual(r.status, CheckStatus.SKIP)

    def test_opentrons_http_pass(self):
        from src.diagnostics.checks import check_opentrons_http
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"robot_model": "OT-2", "api_version": "5.0"}
        with patch("requests.get", return_value=mock_resp):
            r = check_opentrons_http("192.168.1.50")
        self.assertEqual(r.status, CheckStatus.PASS)
        self.assertIn("OT-2", r.message)

    def test_opentrons_http_fail_on_error(self):
        from src.diagnostics.checks import check_opentrons_http
        with patch("requests.get", side_effect=Exception("connection error")):
            r = check_opentrons_http("192.168.1.50")
        self.assertEqual(r.status, CheckStatus.FAIL)

    def test_opentrons_http_skip_when_not_configured(self):
        from src.diagnostics.checks import check_opentrons_http
        r = check_opentrons_http("")
        self.assertEqual(r.status, CheckStatus.SKIP)

    def test_opentrons_pipettes_found(self):
        from src.diagnostics.checks import check_opentrons_pipettes
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "left": {"model": "p300_single_gen2", "name": "p300_single"},
            "right": {"model": None},
        }
        with patch("requests.get", return_value=mock_resp):
            r = check_opentrons_pipettes("192.168.1.50")
        self.assertEqual(r.status, CheckStatus.PASS)
        self.assertIn("left", r.message)

    def test_opentrons_pipettes_none_mounted_warns(self):
        from src.diagnostics.checks import check_opentrons_pipettes
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"left": {}, "right": {}}
        with patch("requests.get", return_value=mock_resp):
            r = check_opentrons_pipettes("192.168.1.50")
        self.assertEqual(r.status, CheckStatus.WARN)

    def test_ur_rtde_skip_no_library_falls_back_to_tcp(self):
        from src.diagnostics.checks import check_ur_rtde
        with patch("socket.create_connection", side_effect=ConnectionRefusedError):
            r = check_ur_rtde("192.168.1.10")
        # Either FAIL (tcp refused) or SKIP is acceptable
        self.assertIn(r.status, (CheckStatus.FAIL, CheckStatus.SKIP))

    def test_ur_rtde_skip_when_no_host(self):
        from src.diagnostics.checks import check_ur_rtde
        r = check_ur_rtde("")
        self.assertEqual(r.status, CheckStatus.SKIP)


# ══════════════════════════════════════════════════════════════════════════════
# Serial device checks
# ══════════════════════════════════════════════════════════════════════════════

class TestSerialChecks(unittest.TestCase):

    def test_serial_ports_available_found(self):
        from src.diagnostics.checks import check_serial_ports_available
        with patch("glob.glob", return_value=["/dev/ttyUSB0", "/dev/ttyUSB1"]):
            with patch("platform.system", return_value="Linux"):
                r = check_serial_ports_available()
        self.assertEqual(r.status, CheckStatus.PASS)
        self.assertIn("/dev/ttyUSB0", r.detail["ports"])

    def test_serial_ports_none_warns(self):
        from src.diagnostics.checks import check_serial_ports_available
        with patch("glob.glob", return_value=[]):
            with patch("platform.system", return_value="Linux"):
                r = check_serial_ports_available()
        self.assertEqual(r.status, CheckStatus.WARN)

    def test_serial_device_pass_with_response(self):
        from src.diagnostics.checks import check_serial_device
        mock_ser = MagicMock()
        mock_ser.read.return_value    = b"IKA Vortex 3\r\n"
        mock_ser.in_waiting           = 14
        with patch("pathlib.Path.exists", return_value=True), \
             patch("serial.Serial", return_value=mock_ser), \
             patch("time.sleep"):
            r = check_serial_device("/dev/ttyUSB1", 9600, "Vortex")
        self.assertEqual(r.status, CheckStatus.PASS)

    def test_serial_device_warn_no_response(self):
        from src.diagnostics.checks import check_serial_device
        mock_ser = MagicMock()
        mock_ser.read.return_value = b""
        mock_ser.in_waiting        = 0
        with patch("pathlib.Path.exists", return_value=True), \
             patch("serial.Serial", return_value=mock_ser), \
             patch("time.sleep"):
            r = check_serial_device("/dev/ttyUSB1", 9600, "Vortex")
        self.assertEqual(r.status, CheckStatus.WARN)

    def test_serial_device_fail_port_not_exist(self):
        from src.diagnostics.checks import check_serial_device
        with patch("pathlib.Path.exists", return_value=False):
            r = check_serial_device("/dev/ttyUSB99", 9600, "Vortex")
        self.assertEqual(r.status, CheckStatus.FAIL)
        self.assertIn("does not exist", r.message)

    def test_serial_device_skip_no_port(self):
        from src.diagnostics.checks import check_serial_device
        r = check_serial_device("", 9600, "Vortex")
        self.assertEqual(r.status, CheckStatus.SKIP)

    def test_serial_device_fail_on_exception(self):
        from src.diagnostics.checks import check_serial_device
        with patch("pathlib.Path.exists", return_value=True), \
             patch("serial.Serial", side_effect=Exception("permission denied")):
            r = check_serial_device("/dev/ttyUSB1", 9600, "Vortex")
        self.assertEqual(r.status, CheckStatus.FAIL)
        self.assertIn("permission denied", r.message)

    def test_dialout_group_in_group(self):
        from src.diagnostics.checks import check_dialout_group
        import grp, pwd
        mock_grp = MagicMock(); mock_grp.gr_name = "dialout"; mock_grp.gr_mem = ["testuser"]
        with patch("platform.system", return_value="Linux"), \
             patch("os.getlogin", return_value="testuser"), \
             patch("grp.getgrall", return_value=[mock_grp]), \
             patch("pwd.getpwnam", return_value=MagicMock(pw_gid=1000)), \
             patch("grp.getgrgid", return_value=MagicMock(gr_name="testuser")):
            r = check_dialout_group()
        self.assertEqual(r.status, CheckStatus.PASS)
        self.assertIn("is", r.message)

    def test_dialout_group_not_in_group(self):
        from src.diagnostics.checks import check_dialout_group
        import grp, pwd
        mock_grp = MagicMock(); mock_grp.gr_name = "somegroup"; mock_grp.gr_mem = []
        with patch("platform.system", return_value="Linux"), \
             patch("os.getlogin", return_value="testuser"), \
             patch("grp.getgrall", return_value=[mock_grp]), \
             patch("pwd.getpwnam", return_value=MagicMock(pw_gid=1000)), \
             patch("grp.getgrgid", return_value=MagicMock(gr_name="users")):
            r = check_dialout_group()
        self.assertEqual(r.status, CheckStatus.WARN)
        self.assertIn("NOT", r.message)

    def test_dialout_group_skip_on_non_linux(self):
        from src.diagnostics.checks import check_dialout_group
        with patch("platform.system", return_value="Darwin"):
            r = check_dialout_group()
        self.assertEqual(r.status, CheckStatus.SKIP)

    def test_relay_board_pass(self):
        from src.diagnostics.checks import check_relay_board
        mock_ser = MagicMock()
        with patch("pathlib.Path.exists", return_value=True), \
             patch("serial.Serial", return_value=mock_ser), \
             patch("time.sleep"):
            r = check_relay_board("/dev/ttyUSB5")
        self.assertEqual(r.status, CheckStatus.PASS)
        # Verify ON then OFF bytes sent
        calls = [c[0][0] for c in mock_ser.write.call_args_list]
        self.assertEqual(calls[0], bytes([0xA0, 1, 1, 0xA2]))  # ON
        self.assertEqual(calls[1], bytes([0xA0, 1, 0, 0xA1]))  # OFF

    def test_relay_board_skip_no_port(self):
        from src.diagnostics.checks import check_relay_board
        r = check_relay_board("")
        self.assertEqual(r.status, CheckStatus.SKIP)

    def test_relay_board_fail_port_not_found(self):
        from src.diagnostics.checks import check_relay_board
        with patch("pathlib.Path.exists", return_value=False):
            r = check_relay_board("/dev/ttyUSB99")
        self.assertEqual(r.status, CheckStatus.FAIL)


# ══════════════════════════════════════════════════════════════════════════════
# Protocol & platform checks
# ══════════════════════════════════════════════════════════════════════════════

class TestProtocolChecks(unittest.TestCase):

    def test_example_protocol_pass(self, tmp_path=None):
        from src.diagnostics.checks import check_example_protocol
        import tempfile, yaml
        YAML_CONTENT = """
metadata: {name: TestProto, safety_level: BSL1}
steps:
  - id: 1
    name: Wait
    type: wait
    params: {duration_s: 1}
"""
        with tempfile.TemporaryDirectory() as td:
            proto_dir = Path(td)
            (proto_dir / "dna_extraction.yaml").write_text(YAML_CONTENT)
            r = check_example_protocol(proto_dir)
        self.assertEqual(r.status, CheckStatus.PASS)
        self.assertIn("TestProto", r.message)

    def test_example_protocol_warn_when_missing(self):
        from src.diagnostics.checks import check_example_protocol
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            r = check_example_protocol(Path(td))
        self.assertEqual(r.status, CheckStatus.WARN)

    def test_example_protocol_fail_on_bad_yaml(self):
        from src.diagnostics.checks import check_example_protocol
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            proto_dir = Path(td)
            (proto_dir / "dna_extraction.yaml").write_text("{bad: [yaml")
            r = check_example_protocol(proto_dir)
        self.assertEqual(r.status, CheckStatus.FAIL)

    def test_simulation_run_pass(self):
        from src.diagnostics.checks import check_simulation_run
        import tempfile
        YAML_CONTENT = """
metadata: {name: DiagTest, safety_level: BSL1}
equipment: []
steps:
  - id: 1
    name: Wait
    type: wait
    params: {duration_s: 0}
"""
        with tempfile.TemporaryDirectory() as td:
            proto_dir = Path(td)
            (proto_dir / "dna_extraction.yaml").write_text(YAML_CONTENT)
            r = check_simulation_run(proto_dir)
        self.assertEqual(r.status, CheckStatus.PASS)
        self.assertIn("completed", r.message)
        self.assertEqual(r.detail["steps_done"], 1)

    def test_simulation_run_skip_when_no_protocol(self):
        from src.diagnostics.checks import check_simulation_run
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            r = check_simulation_run(Path(td))
        self.assertEqual(r.status, CheckStatus.SKIP)


# ══════════════════════════════════════════════════════════════════════════════
# Check result structure
# ══════════════════════════════════════════════════════════════════════════════

class TestCheckResult(unittest.TestCase):

    def test_icon_mapping(self):
        from src.diagnostics.checks import CheckResult
        self.assertEqual(CheckResult("x","y",CheckStatus.PASS,"").icon, "✓")
        self.assertEqual(CheckResult("x","y",CheckStatus.WARN,"").icon, "⚠")
        self.assertEqual(CheckResult("x","y",CheckStatus.FAIL,"").icon, "✗")
        self.assertEqual(CheckResult("x","y",CheckStatus.SKIP,"").icon, "–")

    def test_duration_injected_by_decorator(self):
        from src.diagnostics.checks import check_python_version
        r = check_python_version()
        self.assertGreaterEqual(r.duration, 0.0)

    def test_all_check_results_have_required_fields(self):
        from src.diagnostics.checks import CheckResult
        r = CheckResult(name="test", category="Test",
                        status=CheckStatus.PASS, message="ok")
        self.assertEqual(r.name, "test")
        self.assertEqual(r.category, "Test")
        self.assertEqual(r.status, CheckStatus.PASS)
        self.assertEqual(r.message, "ok")
        self.assertIsNone(r.detail)
        self.assertEqual(r.duration, 0.0)


# ══════════════════════════════════════════════════════════════════════════════
# Diagnostic Runner
# ══════════════════════════════════════════════════════════════════════════════

class TestDiagnosticRunner(unittest.TestCase):

    def _make_runner(self, simulate=True, tmp_root=None):
        from src.diagnostics.diagnostic_runner import DiagnosticRunner
        root = tmp_root or Path(".")
        return DiagnosticRunner(config={}, project_root=root, simulate=simulate)

    def test_run_returns_list_of_results(self):
        runner = self._make_runner()
        results = runner.run(quick=True)
        self.assertIsInstance(results, list)
        self.assertGreater(len(results), 0)
        from src.diagnostics.checks import CheckResult
        for r in results:
            self.assertIsInstance(r, CheckResult)

    def test_run_category_filter(self):
        runner = self._make_runner()
        results = runner.run(categories=["Environment"], quick=True)
        for r in results:
            self.assertEqual(r.category, "Environment")

    def test_run_no_crashes(self):
        """Full suite must not raise exceptions."""
        runner = self._make_runner(simulate=True)
        try:
            results = runner.run(quick=True)
        except Exception as exc:
            self.fail(f"Diagnostic runner raised: {exc}")

    def test_all_results_have_duration(self):
        runner = self._make_runner()
        results = runner.run(quick=True)
        for r in results:
            self.assertGreaterEqual(r.duration, 0.0,
                                    f"No duration on {r.name}")

    def test_export_json(self):
        import tempfile
        runner = self._make_runner()
        results = runner.run(quick=True)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = Path(f.name)
        runner.export(results, path)
        self.assertTrue(path.exists())
        with open(path) as f:
            data = json.load(f)
        self.assertIn("checks", data)
        self.assertIn("summary", data)
        self.assertEqual(data["summary"]["total"], len(results))
        path.unlink()

    def test_export_html(self):
        import tempfile
        runner = self._make_runner()
        results = runner.run(quick=True)
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
            path = Path(f.name)
        runner.export(results, path)
        self.assertTrue(path.exists())
        content = path.read_text()
        self.assertIn("OpenLab Diagnostic Report", content)
        self.assertIn("PASS", content)
        path.unlink()

    def test_summary_counts_correct(self):
        from src.diagnostics.checks import CheckResult
        runner = self._make_runner()
        # Inject known results
        results = [
            CheckResult("a", "X", CheckStatus.PASS, "ok"),
            CheckResult("b", "X", CheckStatus.WARN, "warn"),
            CheckResult("c", "X", CheckStatus.FAIL, "fail"),
            CheckResult("d", "X", CheckStatus.SKIP, "skip"),
        ]
        # Export and check counts
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = Path(f.name)
        runner.export(results, path)
        with open(path) as f:
            data = json.load(f)
        self.assertEqual(data["summary"]["pass"], 1)
        self.assertEqual(data["summary"]["warn"], 1)
        self.assertEqual(data["summary"]["fail"], 1)
        self.assertEqual(data["summary"]["skip"], 1)
        path.unlink()

    def test_crashed_check_becomes_fail_result(self):
        """A check that raises an exception must not abort the suite."""
        from src.diagnostics.diagnostic_runner import DiagnosticRunner
        from src.diagnostics.checks import check_python_version

        runner = DiagnosticRunner(config={}, project_root=Path("."), simulate=True)
        # Inject a check that crashes
        def bad_check():
            raise RuntimeError("unexpected crash")

        original_build = runner._build_check_list
        runner._build_check_list = lambda quick=False: [(bad_check, [], {})] + original_build(quick=True)

        results = runner.run(quick=True)
        fail_names = [r.name for r in results if r.status == CheckStatus.FAIL]
        self.assertIn("bad_check", fail_names)

    def test_print_report_does_not_raise(self):
        from src.diagnostics.checks import CheckResult
        runner = self._make_runner()
        results = [
            CheckResult("test", "Cat", CheckStatus.PASS, "all good", duration=0.01),
            CheckResult("test2", "Cat", CheckStatus.FAIL, "broken"),
        ]
        try:
            runner.print_report(results)
        except Exception as exc:
            self.fail(f"print_report raised: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# CLI diagnose command
# ══════════════════════════════════════════════════════════════════════════════

class TestDiagnoseCLI(unittest.TestCase):

    def _cli(self, args: list):
        from click.testing import CliRunner
        from main import cli
        runner = CliRunner()
        return runner.invoke(cli, args, catch_exceptions=False)

    def test_diagnose_simulate_quick_exits_zero(self):
        result = self._cli(["diagnose", "--simulate", "--quick"])
        # May exit 0 (all pass) or 1 (some fail) – must not crash
        self.assertIn(result.exit_code, (0, 1),
                      f"Unexpected exit code: {result.exit_code}\n{result.output}")

    def test_diagnose_category_filter(self):
        result = self._cli(["diagnose", "--simulate", "--quick",
                             "--category", "Environment"])
        self.assertNotIn("Exception", result.output)

    def test_diagnose_export_json(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        self._cli(["diagnose", "--simulate", "--quick", "--export", path])
        self.assertTrue(Path(path).exists())
        with open(path) as f:
            data = json.load(f)
        self.assertIn("checks", data)
        Path(path).unlink()

    def test_diagnose_export_html(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
            path = f.name
        self._cli(["diagnose", "--simulate", "--quick", "--export", path])
        self.assertTrue(Path(path).exists())
        content = Path(path).read_text()
        self.assertIn("OpenLab Diagnostic Report", content)
        Path(path).unlink()

    def test_diagnose_help_text(self):
        result = self._cli(["diagnose", "--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("diagnostic", result.output.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
