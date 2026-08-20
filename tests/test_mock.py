"""
OpenLab – Comprehensive Mock Unit Tests
========================================
All tests use unittest.mock to patch hardware interfaces.
No real hardware, serial ports, or network connections are needed.

Coverage
────────
  MockSerial           – patches serial.Serial for all peripheral drivers
  MockRequests         – patches requests.Session for Opentrons HTTP API
  MockRTDE             – patches ur_rtde (rtde_control / rtde_receive)
  MockGPIO             – patches RPi.GPIO
  MockRelay            – patches USB relay board serial

Test classes
────────────
  TestVortexDriver           – IKA vortex serial protocol
  TestCentrifugeDriver       – Eppendorf centrifuge serial protocol
  TestIncubatorDriver        – Inheco ODTC serial protocol
  TestMagneticSeparatorDriver – relay board / GPIO control
  TestRockerDriver           – relay board control
  TestOpentronsController    – HTTP API (upload / run / poll / commands)
  TestRoboticArmDriver       – RTDE motion, gripper, force control
  TestTubeHandler            – high-level arm tube operations
  TestSafetyMonitor          – fault detection, E-stop, arm gate
  TestOrchestrator           – full step dispatch with mocked equipment
  TestProtocolParser         – malformed input, edge cases
  TestRiskAssessor           – hazard classification, BSL levels
  TestResourceManager        – stock checks, buffer math, Excel export
  TestAPIEndpoints           – FastAPI routes with TestClient
  TestEdgeCases              – timeout, comm loss, imbalance, over-temp
  TestEStopPropagation       – E-stop flows through all equipment
"""
from __future__ import annotations

import io
import json
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import (
    MagicMock, Mock, PropertyMock,
    call, patch, mock_open, ANY
)

import pytest

# ── Helpers ───────────────────────────────────────────────────────────────────

def _sim_cfg(name: str, **extra):
    from src.equipment.base_equipment import EquipmentConfig
    return EquipmentConfig(name=name, simulate=False, extra=extra)

def _fake_serial_response(responses: list[bytes]):
    """Return a serial.Serial mock that yields responses one by one."""
    s = MagicMock()
    s.is_open = True
    s.readline.side_effect = responses + [b""]
    s.write    = MagicMock()
    s.close    = MagicMock()
    return s


# ══════════════════════════════════════════════════════════════════════════════
# Vortex
# ══════════════════════════════════════════════════════════════════════════════

class TestVortexDriver(unittest.TestCase):

    def _make(self, serial_mock):
        from src.equipment.peripherals.vortex import Vortex
        from src.equipment.base_equipment import EquipmentConfig
        cfg = EquipmentConfig(name="vortex", port="/dev/ttyUSB1",
                              baud_rate=9600, simulate=False)
        with patch("serial.Serial", return_value=serial_mock):
            v = Vortex(cfg)
            v.connect()
        return v

    def test_connect_sends_name_query(self):
        s = _fake_serial_response([b"IKA Vortex 3\r\n"])
        v = self._make(s)
        # Should have written IN_NAME\r\n
        s.write.assert_called_once_with(b"IN_NAME\r\n")

    def test_set_speed_sends_correct_command(self):
        s = _fake_serial_response([b"IKA\r\n", b""])
        v = self._make(s)
        s.write.reset_mock()
        v.set_speed(1500)
        s.write.assert_called_with(b"OUT_SP_4 1500\r\n")

    def test_set_speed_clamps_to_max(self):
        s = _fake_serial_response([b"IKA\r\n", b""])
        v = self._make(s)
        s.write.reset_mock()
        v.set_speed(99999)     # way above max
        written = s.write.call_args[0][0].decode()
        rpm_val = int(written.strip().split()[-1])
        self.assertLessEqual(rpm_val, v.MAX_RPM)

    def test_set_speed_clamps_to_zero(self):
        s = _fake_serial_response([b"IKA\r\n", b""])
        v = self._make(s)
        s.write.reset_mock()
        v.set_speed(-500)
        written = s.write.call_args[0][0].decode()
        rpm_val = int(written.strip().split()[-1])
        self.assertEqual(rpm_val, 0)

    def test_start_sends_start_command(self):
        s = _fake_serial_response([b"IKA\r\n", b""])
        v = self._make(s)
        s.write.reset_mock()
        v.start()
        s.write.assert_called_with(b"START_4\r\n")

    def test_stop_sends_stop_command(self):
        s = _fake_serial_response([b"IKA\r\n", b""])
        v = self._make(s)
        s.write.reset_mock()
        v.stop()
        s.write.assert_called_with(b"STOP_4\r\n")

    def test_get_actual_rpm(self):
        s = _fake_serial_response([b"IKA\r\n", b"1234\r\n"])
        v = self._make(s)
        self.assertEqual(v.get_actual_rpm(), 1234)

    def test_get_actual_rpm_bad_response_returns_zero(self):
        s = _fake_serial_response([b"IKA\r\n", b"ERROR\r\n"])
        v = self._make(s)
        self.assertEqual(v.get_actual_rpm(), 0)

    def test_run_for_sends_all_commands(self):
        s = _fake_serial_response([b"IKA\r\n"])
        v = self._make(s)
        s.write.reset_mock()
        with patch("time.sleep"):
            v.run_for(rpm=2000, duration_s=30)
        calls = [c[0][0].decode().strip() for c in s.write.call_args_list]
        self.assertIn("OUT_SP_4 2000", calls)
        self.assertIn("START_4", calls)
        self.assertIn("STOP_4", calls)

    def test_disconnect_closes_serial(self):
        s = _fake_serial_response([b"IKA\r\n"])
        v = self._make(s)
        v.disconnect()
        s.close.assert_called_once()

    def test_emergency_stop_calls_stop(self):
        s = _fake_serial_response([b"IKA\r\n"])
        v = self._make(s)
        s.write.reset_mock()
        v.emergency_stop()
        last_cmd = s.write.call_args_list[-1][0][0].decode().strip()
        self.assertEqual(last_cmd, "STOP_4")

    def test_connect_failure_sets_error_status(self):
        from src.equipment.peripherals.vortex import Vortex
        from src.equipment.base_equipment import EquipmentConfig, EquipmentStatus
        cfg = EquipmentConfig(name="vortex", port="/dev/ttyUSB1", simulate=False)
        with patch("serial.Serial", side_effect=Exception("No port")):
            v = Vortex(cfg)
            result = v.connect()
        self.assertFalse(result)
        self.assertEqual(v.status, EquipmentStatus.ERROR)


# ══════════════════════════════════════════════════════════════════════════════
# Centrifuge
# ══════════════════════════════════════════════════════════════════════════════

class TestCentrifugeDriver(unittest.TestCase):

    def _make(self, serial_mock=None):
        from src.equipment.peripherals.centrifuge import Centrifuge
        from src.equipment.base_equipment import EquipmentConfig
        cfg = EquipmentConfig(name="centrifuge", port="/dev/ttyUSB2", simulate=False)
        if serial_mock is None:
            serial_mock = _fake_serial_response([b""])
        with patch("serial.Serial", return_value=serial_mock):
            c = Centrifuge(cfg)
            c.connect()
        return c, serial_mock

    def test_configure_sends_speed_time(self):
        c, s = self._make()
        s.write.reset_mock()
        c.configure(rpm=5000, duration_s=60)
        cmds = [w[0][0].decode().strip() for w in s.write.call_args_list]
        self.assertTrue(any("SR 5000" in cmd for cmd in cmds))
        self.assertTrue(any("ST 60" in cmd for cmd in cmds))

    def test_configure_clamps_rpm_to_max(self):
        c, s = self._make()
        s.write.reset_mock()
        c.configure(rpm=999999, duration_s=10)
        cmds = [w[0][0].decode().strip() for w in s.write.call_args_list]
        speed_cmd = next(cmd for cmd in cmds if cmd.startswith("SR"))
        rpm_val = int(speed_cmd.split()[1])
        self.assertLessEqual(rpm_val, c.MAX_RPM)

    def test_configure_with_temperature(self):
        c, s = self._make()
        s.write.reset_mock()
        c.configure(rpm=3000, duration_s=30, temperature_c=4)
        cmds = [w[0][0].decode().strip() for w in s.write.call_args_list]
        self.assertTrue(any("STE 4" in cmd for cmd in cmds))

    def test_start_sends_go(self):
        c, s = self._make()
        c.configure(5000, 60)
        s.write.reset_mock()
        c.start()
        s.write.assert_called_with(b"GO\r\n")

    def test_stop_sends_brake(self):
        c, s = self._make()
        s.write.reset_mock()
        c.stop(brake=True)
        s.write.assert_called_with(b"BR\r\n")

    def test_get_actual_rpm_parses_response(self):
        c, s = self._make()
        # Clear side_effect so return_value takes precedence
        s.readline.side_effect = None
        s.readline.return_value = b"8765\r\n"
        self.assertEqual(c.get_actual_rpm(), 8765)

    def test_is_ready_when_below_idle_rpm(self):
        c, s = self._make()
        s.readline.side_effect = None
        s.readline.return_value = b"10\r\n"   # < 50 → idle
        self.assertTrue(c.is_ready())

    def test_is_not_ready_when_spinning(self):
        c, s = self._make()
        s.readline.side_effect = None
        s.readline.return_value = b"5000\r\n"
        self.assertFalse(c.is_ready())

    def test_lid_open_close(self):
        c, s = self._make()
        s.write.reset_mock()
        c.lid_open()
        c.lid_close()
        cmds = [w[0][0].decode().strip() for w in s.write.call_args_list]
        self.assertIn("OL", cmds)
        self.assertIn("CL", cmds)

    def test_run_blocks_and_calls_start(self):
        c, s = self._make()
        s.readline.return_value = b"0\r\n"   # always idle
        with patch("time.sleep"):
            c.run(rpm=5000, duration_s=2, wait=True)
        # GO must have been sent
        all_writes = [w[0][0].decode().strip() for w in s.write.call_args_list]
        self.assertIn("GO", all_writes)

    def test_emergency_stop_brakes(self):
        c, s = self._make()
        s.write.reset_mock()
        c.emergency_stop()
        cmds = [w[0][0].decode().strip() for w in s.write.call_args_list]
        self.assertIn("BR", cmds)


# ══════════════════════════════════════════════════════════════════════════════
# Incubator
# ══════════════════════════════════════════════════════════════════════════════

class TestIncubatorDriver(unittest.TestCase):

    def _make(self):
        from src.equipment.peripherals.incubator import Incubator
        from src.equipment.base_equipment import EquipmentConfig
        cfg = EquipmentConfig(name="incubator", port="/dev/ttyUSB3", simulate=False)
        s = _fake_serial_response([b"37.2\r\n"] * 20)
        with patch("serial.Serial", return_value=s):
            inc = Incubator(cfg)
            inc.connect()
        return inc, s

    def test_set_temperature_sends_correct_command(self):
        inc, s = self._make()
        s.write.reset_mock()
        inc.set_temperature(56.0)
        s.write.assert_called_with(b"SET TEMP 56.0\r\n")

    def test_set_temperature_clamps_to_max(self):
        inc, s = self._make()
        s.write.reset_mock()
        inc.set_temperature(999)
        cmd = s.write.call_args[0][0].decode()
        temp = float(cmd.strip().split()[-1])
        self.assertLessEqual(temp, inc.MAX_TEMP_C)

    def test_set_temperature_clamps_to_min(self):
        inc, s = self._make()
        s.write.reset_mock()
        inc.set_temperature(-100)
        cmd = s.write.call_args[0][0].decode()
        temp = float(cmd.strip().split()[-1])
        self.assertGreaterEqual(temp, inc.MIN_TEMP_C)

    def test_get_actual_temperature_parses_response(self):
        inc, s = self._make()
        s.readline.return_value = b"GET TEMP 37.2\r\n"
        result = inc.get_actual_temperature()
        self.assertAlmostEqual(result, 37.2)

    def test_set_shaking_sends_command(self):
        inc, s = self._make()
        s.write.reset_mock()
        inc.set_shaking(500)
        s.write.assert_called_with(b"SET SHAKE 500\r\n")

    def test_start_shaking_sends_command(self):
        inc, s = self._make()
        s.write.reset_mock()
        inc.start_shaking()
        s.write.assert_called_with(b"START SHAKE\r\n")

    def test_stop_shaking_sends_command(self):
        inc, s = self._make()
        s.write.reset_mock()
        inc.stop_shaking()
        s.write.assert_called_with(b"STOP SHAKE\r\n")

    def test_stop_all_sends_stop_all(self):
        inc, s = self._make()
        s.write.reset_mock()
        inc.stop_all()
        s.write.assert_called_with(b"STOP ALL\r\n")

    def test_wait_for_temperature_returns_true_when_at_setpoint(self):
        inc, s = self._make()
        inc._set_temp = 37.0
        s.readline.return_value = b"GET TEMP 37.1\r\n"
        with patch("time.sleep"):
            result = inc.wait_for_temperature(tolerance_c=0.5)
        self.assertTrue(result)

    def test_wait_for_temperature_times_out(self):
        inc, s = self._make()
        inc._set_temp = 95.0
        s.readline.return_value = b"GET TEMP 20.0\r\n"
        # time: first call sets deadline (t=0.0), second call (while check) exceeds it
        fake_time = iter([0.0, 100.0])
        with patch("time.sleep"), \
             patch("src.equipment.peripherals.incubator.time.time",
                   side_effect=fake_time):
            result = inc.wait_for_temperature(tolerance_c=0.5, timeout_s=0.001)
        self.assertFalse(result)

    def test_incubate_full_sequence(self):
        inc, s = self._make()
        s.readline.return_value = b"GET TEMP 37.0\r\n"
        with patch("time.sleep"):
            inc.incubate(temperature_c=37, duration_s=1, shake_rpm=300)
        all_writes = [w[0][0].decode().strip() for w in s.write.call_args_list]
        self.assertTrue(any("SET TEMP" in c for c in all_writes))
        self.assertTrue(any("SET SHAKE" in c for c in all_writes))
        self.assertTrue(any("START SHAKE" in c for c in all_writes))
        self.assertTrue(any("STOP SHAKE" in c for c in all_writes))

    def test_emergency_stop_calls_stop_all(self):
        inc, s = self._make()
        s.write.reset_mock()
        inc.emergency_stop()
        cmds = [w[0][0].decode().strip() for w in s.write.call_args_list]
        self.assertIn("STOP ALL", cmds)


# ══════════════════════════════════════════════════════════════════════════════
# Magnetic Separator
# ══════════════════════════════════════════════════════════════════════════════

class TestMagneticSeparatorDriver(unittest.TestCase):

    def _make_serial_backend(self):
        from src.equipment.peripherals.magnetic_separator import MagneticSeparator, MagnetState
        from src.equipment.base_equipment import EquipmentConfig
        cfg = EquipmentConfig(name="mag", simulate=False,
                              extra={"relay_backend": "serial",
                                     "relay_port": "/dev/ttyUSB5",
                                     "relay_channel": 1})
        s = MagicMock()
        s.is_open = True
        with patch("serial.Serial", return_value=s):
            m = MagneticSeparator(cfg)
            m.connect()
        return m, s

    def test_engage_writes_relay_on_bytes(self):
        m, s = self._make_serial_backend()
        s.write.reset_mock()
        m.engage()
        # Expect bytes [0xA0, 1, 1, 0xA2]
        s.write.assert_called_once_with(bytes([0xA0, 1, 1, 0xA2]))

    def test_disengage_writes_relay_off_bytes(self):
        m, s = self._make_serial_backend()
        s.write.reset_mock()
        m.disengage()
        s.write.assert_called_once_with(bytes([0xA0, 1, 0, 0xA1]))

    def test_engage_sets_state_engaged(self):
        from src.equipment.peripherals.magnetic_separator import MagnetState
        m, s = self._make_serial_backend()
        m.engage()
        self.assertEqual(m.state, MagnetState.ENGAGED)

    def test_disengage_sets_state_disengaged(self):
        from src.equipment.peripherals.magnetic_separator import MagnetState
        m, s = self._make_serial_backend()
        m.engage()
        m.disengage()
        self.assertEqual(m.state, MagnetState.DISENGAGED)

    def test_separate_engages_then_leaves_engaged(self):
        from src.equipment.peripherals.magnetic_separator import MagnetState
        m, s = self._make_serial_backend()
        with patch("time.sleep"):
            m.separate(duration_s=10)
        self.assertEqual(m.state, MagnetState.ENGAGED)   # still engaged after

    def test_emergency_stop_disengages(self):
        from src.equipment.peripherals.magnetic_separator import MagnetState
        m, s = self._make_serial_backend()
        m.engage()
        m.emergency_stop()
        self.assertEqual(m.state, MagnetState.DISENGAGED)

    def test_gpio_backend_sets_pin_high_on_engage(self):
        from src.equipment.peripherals.magnetic_separator import MagneticSeparator
        from src.equipment.base_equipment import EquipmentConfig, EquipmentStatus
        cfg = EquipmentConfig(name="mag", simulate=False,
                              extra={"relay_backend": "gpio", "gpio_pin": 22})
        gpio_mock = MagicMock()
        gpio_mock.HIGH = 1
        gpio_mock.LOW  = 0
        # Bypass connect(); inject state directly
        m = MagneticSeparator(cfg)
        m._backend  = "gpio"
        m._gpio     = gpio_mock
        m._gpio_pin = 22
        m.status    = EquipmentStatus.IDLE
        m.engage()
        gpio_mock.output.assert_called_with(22, gpio_mock.HIGH)

    def test_relay_checksum_correct_for_ch2(self):
        from src.equipment.peripherals.magnetic_separator import MagneticSeparator
        from src.equipment.base_equipment import EquipmentConfig
        cfg = EquipmentConfig(name="mag", simulate=False,
                              extra={"relay_backend": "serial",
                                     "relay_port": "/dev/ttyUSB5",
                                     "relay_channel": 2})
        s = MagicMock(); s.is_open = True
        with patch("serial.Serial", return_value=s):
            m = MagneticSeparator(cfg)
            m.connect()
        s.write.reset_mock()
        m.engage()
        written = s.write.call_args[0][0]
        # Checksum = 0xA0 + channel + state
        self.assertEqual(written[3], (0xA0 + 2 + 1) & 0xFF)


# ══════════════════════════════════════════════════════════════════════════════
# Rocker
# ══════════════════════════════════════════════════════════════════════════════

class TestRockerDriver(unittest.TestCase):

    def _make(self):
        from src.equipment.peripherals.rocker import Rocker
        from src.equipment.base_equipment import EquipmentConfig
        cfg = EquipmentConfig(name="rocker", simulate=False,
                              extra={"relay_backend": "serial",
                                     "relay_port": "/dev/ttyUSB4",
                                     "relay_channel": 2})
        s = MagicMock(); s.is_open = True
        with patch("serial.Serial", return_value=s):
            r = Rocker(cfg)
            r.connect()
        return r, s

    def test_start_writes_relay_on(self):
        r, s = self._make()
        s.write.reset_mock()
        r.start()
        s.write.assert_called_once_with(bytes([0xA0, 2, 1, 0xA3]))

    def test_stop_writes_relay_off(self):
        r, s = self._make()
        r.start()
        s.write.reset_mock()
        r.stop()
        s.write.assert_called_once_with(bytes([0xA0, 2, 0, 0xA2]))

    def test_rock_for_starts_and_stops(self):
        r, s = self._make()
        s.write.reset_mock()
        with patch("time.sleep"):
            r.rock_for(duration_s=5)
        cmds = [w[0][0] for w in s.write.call_args_list]
        self.assertEqual(cmds[0], bytes([0xA0, 2, 1, 0xA3]))   # ON
        self.assertEqual(cmds[-1], bytes([0xA0, 2, 0, 0xA2]))  # OFF

    def test_emergency_stop_stops_relay(self):
        r, s = self._make()
        r.start()
        s.write.reset_mock()
        r.emergency_stop()
        s.write.assert_called_with(bytes([0xA0, 2, 0, 0xA2]))


# ══════════════════════════════════════════════════════════════════════════════
# Opentrons Controller
# ══════════════════════════════════════════════════════════════════════════════

class TestOpentronsController(unittest.TestCase):

    def _make(self, session_mock=None):
        from src.equipment.opentrons_controller import OpentronsController
        from src.equipment.base_equipment import EquipmentConfig
        cfg = EquipmentConfig(name="opentrons", ip_address="192.168.1.50",
                              simulate=False, extra={"port": 31950})
        if session_mock is None:
            session_mock = MagicMock()
        with patch("requests.Session", return_value=session_mock):
            ot = OpentronsController(cfg)
        ot._session = session_mock
        return ot, session_mock

    def _health_ok(self):
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {"robot_model": "OT-2", "api_version": "5.0"}
        return r

    def test_connect_success(self):
        from src.equipment.base_equipment import EquipmentStatus
        ot, s = self._make()
        s.get.return_value = self._health_ok()
        result = ot.connect()
        self.assertTrue(result)
        self.assertEqual(ot.status, EquipmentStatus.IDLE)

    def test_connect_failure_on_http_error(self):
        from src.equipment.base_equipment import EquipmentStatus
        ot, s = self._make()
        s.get.side_effect = Exception("connection refused")
        result = ot.connect()
        self.assertFalse(result)
        self.assertEqual(ot.status, EquipmentStatus.ERROR)

    def test_connect_sets_opentrons_version_header(self):
        ot, s = self._make()
        s.get.return_value = self._health_ok()
        ot.connect()
        s.headers.update.assert_called_with({"Opentrons-Version": "5"})

    def test_upload_protocol_posts_file(self):
        ot, s = self._make()
        s.get.return_value = self._health_ok()
        ot.connect()
        r = MagicMock()
        r.json.return_value = {"data": {"id": "proto-123"}}
        s.post.return_value = r
        proto_path = Path("protocols/dna_extraction.yaml")
        with patch("builtins.open", mock_open(read_data=b"yaml content")):
            pid = ot.upload_protocol(proto_path)
        self.assertEqual(pid, "proto-123")
        s.post.assert_called_once()
        call_kwargs = s.post.call_args
        self.assertIn("/protocols", call_kwargs[0][0])

    def test_create_run_returns_run_id(self):
        ot, s = self._make()
        s.get.return_value = self._health_ok()
        ot.connect()
        r = MagicMock()
        r.json.return_value = {"data": {"id": "run-abc"}}
        s.post.return_value = r
        rid = ot.create_run("proto-123")
        self.assertEqual(rid, "run-abc")
        self.assertEqual(ot._run_id, "run-abc")

    def test_start_run_posts_play_action(self):
        ot, s = self._make()
        s.get.return_value = self._health_ok()
        ot.connect()
        r = MagicMock(); r.json.return_value = {"data": {"id": "run-abc"}}
        s.post.return_value = r
        ot.create_run("proto-123")
        s.post.reset_mock()
        ot.start_run("run-abc")
        call_json = s.post.call_args[1]["json"]
        self.assertEqual(call_json["data"]["actionType"], "play")

    def test_poll_run_returns_succeeded(self):
        ot, s = self._make()
        s.get.return_value = self._health_ok()
        ot.connect()
        # First poll → running, second → succeeded
        run_responses = [
            MagicMock(**{"json.return_value": {"data": {"status": "running"}}}),
            MagicMock(**{"json.return_value": {"data": {"status": "succeeded"}}}),
        ]
        s.get.side_effect = run_responses
        with patch("time.sleep"):
            status = ot.poll_run("run-abc", poll_interval=0.01)
        self.assertEqual(status, "succeeded")

    def test_poll_run_returns_failed(self):
        ot, s = self._make()
        s.get.return_value = self._health_ok()
        ot.connect()
        s.get.return_value = MagicMock(
            **{"json.return_value": {"data": {"status": "failed"}}}
        )
        with patch("time.sleep"):
            status = ot.poll_run("run-abc")
        self.assertEqual(status, "failed")

    def test_poll_run_raises_timeout(self):
        ot, s = self._make()
        s.get.return_value = MagicMock(
            **{"json.return_value": {"data": {"status": "running"}}}
        )
        ot._run_id = "run-abc"
        with patch("time.sleep"), self.assertRaises(TimeoutError):
            ot.poll_run("run-abc", timeout=0.01)

    def test_emergency_stop_posts_stop_action(self):
        ot, s = self._make()
        s.get.return_value = self._health_ok()
        ot.connect()
        ot._run_id = "run-abc"
        s.post.reset_mock()
        ot.emergency_stop()
        call_url  = s.post.call_args[0][0]
        call_json = s.post.call_args[1]["json"]
        self.assertIn("run-abc", call_url)
        self.assertEqual(call_json["data"]["actionType"], "stop")

    def test_home_robot_posts_to_home_endpoint(self):
        ot, s = self._make()
        ot.home_robot()
        call_url = s.post.call_args[0][0]
        self.assertIn("/robot/home", call_url)

    def test_lights_on(self):
        ot, s = self._make()
        ot.lights_on(True)
        s.post.assert_called()
        body = s.post.call_args[1]["json"]
        self.assertTrue(body["on"])

    def test_direct_command_posts_to_commands_endpoint(self):
        ot, s = self._make()
        r = MagicMock()
        r.json.return_value = {"data": {"status": "queued"}}
        s.post.return_value = r
        cmd = {"commandType": "aspirate", "params": {"volume": 100}}
        ot.direct_command("run-abc", cmd)
        call_url = s.post.call_args[0][0]
        self.assertIn("commands", call_url)


# ══════════════════════════════════════════════════════════════════════════════
# Robotic Arm
# ══════════════════════════════════════════════════════════════════════════════

class TestRoboticArmDriver(unittest.TestCase):

    def _make_rtde_mocks(self):
        rtde_c = MagicMock()
        rtde_c.moveL.return_value = True
        rtde_c.moveJ.return_value = True
        rtde_c.forceMode.return_value = None
        rtde_c.forceModeStop.return_value = None
        rtde_c.stopScript.return_value = None

        rtde_r = MagicMock()
        rtde_r.getRobotMode.return_value = 7   # RUNNING
        rtde_r.getActualTCPPose.return_value   = [0.3, 0.2, 0.4, 3.14, 0.0, 0.0]
        rtde_r.getActualTCPForce.return_value  = [0, 0, 0, 0, 0, 0.5]  # 0.5 Nm

        dashboard = MagicMock()
        return rtde_c, rtde_r, dashboard

    def _make(self):
        """
        Build a RoboticArm with RTDE mocks injected directly.
        Bypasses connect() to avoid patching module-level rtde imports
        (which may not exist when ur_rtde is not installed).
        """
        from src.equipment.robotic_arm import RoboticArm
        from src.equipment.base_equipment import EquipmentConfig, EquipmentStatus
        cfg = EquipmentConfig(name="arm", ip_address="192.168.1.10", simulate=False)
        rtde_c, rtde_r, dashboard = self._make_rtde_mocks()

        arm = RoboticArm(cfg)
        # Inject mocks directly – no need to call connect()
        arm._rtde_c    = rtde_c
        arm._rtde_r    = rtde_r
        arm._dashboard = dashboard
        arm.status     = EquipmentStatus.IDLE
        return arm, rtde_c, rtde_r

    def test_connect_sets_idle_status(self):
        """connect() in simulation mode must set IDLE."""
        from src.equipment.robotic_arm import RoboticArm
        from src.equipment.base_equipment import EquipmentConfig, EquipmentStatus
        cfg = EquipmentConfig(name="arm", ip_address="192.168.1.10", simulate=True)
        arm = RoboticArm(cfg)
        result = arm.connect()
        self.assertTrue(result)
        self.assertEqual(arm.status, EquipmentStatus.IDLE)

    def test_is_ready_when_mode_7(self):
        arm, _, rtde_r = self._make()
        rtde_r.getRobotMode.return_value = 7
        self.assertTrue(arm.is_ready())

    def test_is_not_ready_when_not_mode_7(self):
        arm, _, rtde_r = self._make()
        rtde_r.getRobotMode.return_value = 5   # not RUNNING
        self.assertFalse(arm.is_ready())

    def test_move_to_calls_movel(self):
        arm, rtde_c, _ = self._make()
        pose = [0.3, 0.2, 0.5, 3.14, 0.0, 0.0]
        arm.move_to(pose, speed=0.3)
        rtde_c.moveL.assert_called_once_with(pose, 0.3, arm.ACCEL)

    def test_move_to_raises_on_failure(self):
        arm, rtde_c, _ = self._make()
        rtde_c.moveL.return_value = False
        with self.assertRaises(RuntimeError):
            arm.move_to([0.3, 0.2, 0.5, 3.14, 0.0, 0.0])

    def test_home_moves_to_home_position(self):
        arm, rtde_c, _ = self._make()
        rtde_c.moveL.return_value = True
        arm.home()
        rtde_c.moveL.assert_called()
        called_pose = rtde_c.moveL.call_args[0][0]
        self.assertEqual(called_pose, arm._positions.home)

    def test_gripper_open_sends_urscript(self):
        arm, _, _ = self._make()
        with patch.object(arm, "_send_urscript") as mock_script:
            arm.gripper_open()
        mock_script.assert_called_once()
        self.assertIn("rq_move_and_wait_mm", mock_script.call_args[0][0])

    def test_gripper_close_sends_urscript(self):
        arm, _, _ = self._make()
        with patch.object(arm, "_send_urscript") as mock_script:
            arm.gripper_close(force_pct=50)
        mock_script.assert_called_once()

    def test_pick_tube_sequence(self):
        arm, rtde_c, _ = self._make()
        rtde_c.moveL.return_value = True
        pose = [0.4, -0.1, 0.3, 3.14, 0.0, 0.0]
        with patch.object(arm, "_send_urscript"):
            arm.pick_tube(pose)
        # Should: gripper_open → approach → descend → close → retract
        self.assertGreaterEqual(rtde_c.moveL.call_count, 2)

    def test_place_tube_sequence(self):
        arm, rtde_c, _ = self._make()
        rtde_c.moveL.return_value = True
        pose = [0.4, -0.1, 0.3, 3.14, 0.0, 0.0]
        with patch.object(arm, "_send_urscript"):
            arm.place_tube(pose)
        self.assertGreaterEqual(rtde_c.moveL.call_count, 2)

    def test_invert_tube_rotates_wrist(self):
        arm, rtde_c, rtde_r = self._make()
        rtde_c.moveL.return_value = True
        rtde_r.getActualTCPPose.return_value = [0.3, 0.2, 0.4, 3.14, 0.0, 0.0]
        with patch("time.sleep"):
            arm.invert_tube(n_times=2)
        # 2 inversions = 4 moveL calls (up + down per inversion)
        self.assertEqual(rtde_c.moveL.call_count, 4)

    def test_tighten_cap_enters_force_mode(self):
        arm, rtde_c, rtde_r = self._make()
        rtde_c.moveL.return_value = True
        # Return increasing torque so it hits target
        rtde_r.getActualTCPForce.return_value = [0, 0, 0, 0, 0, 5.0]  # 5 Nm immediately
        arm.tighten_cap(target_torque_Nm=0.3)
        rtde_c.forceMode.assert_called_once()
        rtde_c.forceModeStop.assert_called_once()

    def test_tighten_cap_returns_true_when_torque_reached(self):
        arm, rtde_c, rtde_r = self._make()
        rtde_c.moveL.return_value = True
        rtde_r.getActualTCPPose.return_value = [0.3, 0.2, 0.4, 3.14, 0.0, 0.0]
        rtde_r.getActualTCPForce.return_value = [0, 0, 0, 0, 0, 10.0]  # well above
        result = arm.tighten_cap(target_torque_Nm=0.3)
        self.assertTrue(result)

    def test_tighten_cap_returns_false_when_max_turns_exceeded(self):
        arm, rtde_c, rtde_r = self._make()
        rtde_c.moveL.return_value = True
        rtde_r.getActualTCPPose.return_value = [0.3, 0.2, 0.4, 3.14, 0.0, 0.0]
        rtde_r.getActualTCPForce.return_value = [0, 0, 0, 0, 0, 0.001]  # never enough
        result = arm.tighten_cap(target_torque_Nm=100.0, max_turns=0.001)
        self.assertFalse(result)

    def test_emergency_stop_calls_stop_script(self):
        arm, rtde_c, _ = self._make()
        arm.emergency_stop()
        rtde_c.stopScript.assert_called()

    def test_disconnect_stops_script(self):
        arm, rtde_c, _ = self._make()
        arm.disconnect()
        rtde_c.stopScript.assert_called()


# ══════════════════════════════════════════════════════════════════════════════
# Tube Handler
# ══════════════════════════════════════════════════════════════════════════════

class TestTubeHandler(unittest.TestCase):

    def _make(self):
        from src.equipment.peripherals.tube_handler import TubeHandler
        arm = MagicMock()
        arm._positions = MagicMock()
        arm._positions.cap_tightener = [0.15, 0.35, 0.2, 3.14, 0.0, 0.0]
        arm._positions.vortex        = [0.2,  0.3,  0.25, 3.14, 0.0, 0.0]
        arm._positions.rocker        = [0.25, 0.36, 0.2, 3.14, 0.0, 0.0]
        arm._positions.incubator     = [0.0,  0.45, 0.2, 3.14, 0.0, 0.0]
        arm._positions.centrifuge_load   = [0.1, 0.42, 0.2, 3.14, 0.0, 0.0]
        arm._positions.centrifuge_unload = [0.1, 0.42, 0.3, 3.14, 0.0, 0.0]
        arm.tighten_cap.return_value = True
        return TubeHandler(arm), arm

    def test_tighten_cap_sequence(self):
        th, arm = self._make()
        pose = [0.4, -0.1, 0.3, 3.14, 0.0, 0.0]
        th.tighten_cap(pose)
        arm.pick_tube.assert_called_once_with(pose)
        arm.tighten_cap.assert_called_once()
        arm.place_tube.assert_called_once_with(pose)
        arm.home.assert_called()

    def test_loosen_cap_sequence(self):
        th, arm = self._make()
        pose = [0.4, -0.1, 0.3, 3.14, 0.0, 0.0]
        th.loosen_cap(pose)
        arm.pick_tube.assert_called_once_with(pose)
        arm.loosen_cap.assert_called_once()
        arm.place_tube.assert_called_once_with(pose)
        arm.home.assert_called()

    def test_invert_tube_calls_arm_invert(self):
        th, arm = self._make()
        pose = [0.4, -0.1, 0.3, 3.14, 0.0, 0.0]
        th.invert_tube(pose, n_times=5)
        # TubeHandler.invert_tube delegates to arm.invert_tube(n_times=n_times)
        arm.invert_tube.assert_called_once()
        _, kwargs = arm.invert_tube.call_args
        self.assertEqual(kwargs.get("n_times"), 5)

    def test_transfer_tube(self):
        th, arm = self._make()
        src  = [0.3, 0.0, 0.25, 3.14, 0.0, 0.0]
        dest = [0.2, 0.3, 0.25, 3.14, 0.0, 0.0]
        th.transfer_tube(src, dest)
        arm.pick_tube.assert_called_once_with(src)
        arm.place_tube.assert_called_once_with(dest)

    def test_transfer_to_vortex(self):
        th, arm = self._make()
        pose = [0.3, 0.0, 0.25, 3.14, 0.0, 0.0]
        th.transfer_to_vortex(pose)
        arm.place_tube.assert_called_once_with(arm._positions.vortex)

    def test_transfer_to_centrifuge(self):
        th, arm = self._make()
        pose = [0.3, 0.0, 0.25, 3.14, 0.0, 0.0]
        th.transfer_to_centrifuge(pose)
        arm.place_tube.assert_called_once_with(arm._positions.centrifuge_load)


# ══════════════════════════════════════════════════════════════════════════════
# Safety Monitor
# ══════════════════════════════════════════════════════════════════════════════

class TestSafetyMonitor(unittest.TestCase):

    def _make(self, config=None):
        from src.safety.safety_monitor import SafetyMonitor
        cfg = config or {"max_incubator_temp_c": 95, "max_centrifuge_rpm": 14000}
        return SafetyMonitor(cfg)

    def test_arm_can_move_initially(self):
        mon = self._make()
        self.assertTrue(mon.can_arm_move())

    def test_estop_blocks_arm(self):
        mon = self._make()
        mon.trigger_estop("test")
        self.assertFalse(mon.can_arm_move())

    def test_estop_propagates_to_all_equipment(self):
        mon = self._make()
        eq1 = MagicMock(); eq1.config = MagicMock(simulate=False)
        eq2 = MagicMock(); eq2.config = MagicMock(simulate=False)
        mon.register("dev1", eq1)
        mon.register("dev2", eq2)
        mon.trigger_estop("test")
        eq1.emergency_stop.assert_called_once()
        eq2.emergency_stop.assert_called_once()

    def test_reset_estop_unblocks_arm(self):
        mon = self._make()
        mon.trigger_estop("test")
        self.assertFalse(mon.can_arm_move())
        mon.reset_estop()
        self.assertTrue(mon.can_arm_move())

    def test_centrifuge_spinning_blocks_arm(self):
        from src.safety.safety_monitor import SafetyFaultType
        mon = self._make()
        mon._set_fault(SafetyFaultType.CENTRIFUGE_SPINNING, "centrifuge", "spinning")
        self.assertFalse(mon.can_arm_move())

    def test_clearing_centrifuge_fault_unblocks_arm(self):
        from src.safety.safety_monitor import SafetyFaultType
        mon = self._make()
        mon._set_fault(SafetyFaultType.CENTRIFUGE_SPINNING, "centrifuge", "spinning")
        mon._clear_fault(SafetyFaultType.CENTRIFUGE_SPINNING)
        self.assertTrue(mon.can_arm_move())

    def test_assert_arm_safe_raises_when_blocked(self):
        mon = self._make()
        mon.trigger_estop("test")
        with self.assertRaises(RuntimeError):
            mon.assert_arm_safe()

    def test_event_recorded_on_estop(self):
        mon = self._make()
        mon.trigger_estop("test_source")
        events = mon.get_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].source, "test_source")

    def test_callback_invoked_on_fault(self):
        from src.safety.safety_monitor import SafetyFaultType
        mon = self._make()
        cb = MagicMock()
        mon.add_callback(cb)
        mon._set_fault(SafetyFaultType.EQUIPMENT_ERROR, "device", "error state")
        cb.assert_called_once()
        event = cb.call_args[0][0]
        self.assertEqual(event.fault, SafetyFaultType.EQUIPMENT_ERROR)

    def test_over_temp_check_triggers_fault(self):
        from src.safety.safety_monitor import SafetyFaultType
        mon = self._make({"max_incubator_temp_c": 50})
        inc = MagicMock()
        inc.config.simulate = False
        inc.get_actual_temperature.return_value = 99.0
        inc.stop_all = MagicMock()
        mon.register("incubator", inc)

        # Patch isinstance to return True for incubator check
        from src.equipment.peripherals.incubator import Incubator
        with patch("src.safety.safety_monitor.isinstance", side_effect=lambda obj, cls: True):
            mon._check_incubator()

        active = mon.get_active_faults()
        self.assertIn(SafetyFaultType.OVER_TEMPERATURE, active)

    def test_equipment_error_status_triggers_fault(self):
        from src.equipment.base_equipment import EquipmentStatus
        from src.safety.safety_monitor import SafetyFaultType
        mon = self._make()
        dev = MagicMock()
        dev.status = EquipmentStatus.ERROR
        dev.config.simulate = False
        mon.register("bad_device", dev)
        mon._check_equipment_errors()
        active = mon.get_active_faults()
        self.assertIn(SafetyFaultType.EQUIPMENT_ERROR, active)

    def test_get_active_faults_empty_initially(self):
        mon = self._make()
        self.assertEqual(mon.get_active_faults(), [])

    def test_monitor_start_stop(self):
        mon = self._make()
        mon.start()
        time.sleep(0.1)
        mon.stop()
        self.assertFalse(mon._running)


# ══════════════════════════════════════════════════════════════════════════════
# Orchestrator (mocked equipment)
# ══════════════════════════════════════════════════════════════════════════════

class TestOrchestratorMocked(unittest.TestCase):

    PROTOCOL_YAML = """
metadata:
  name: "Mock Run"
  safety_level: BSL1
materials:
  reagents:
    - name: PBS
      volume_ul: 100
      hazard_class: none
consumables:
  - name: tubes
    quantity: 2
equipment:
  - name: opentrons
  - name: vortex
  - name: incubator
  - name: centrifuge
  - name: magnetic_separator
  - name: rocker
steps:
  - id: 1
    name: Pipette
    type: pipette
    params: {source: "A1", destination: "B1", volume_ul: 50}
  - id: 2
    name: Vortex
    type: vortex
    params: {rpm: 1000, duration_s: 1}
  - id: 3
    name: Incubate
    type: incubate
    params: {temperature_c: 37, duration_s: 1}
  - id: 4
    name: Centrifuge
    type: centrifuge
    params: {rpm: 5000, duration_s: 1}
  - id: 5
    name: Mag Sep
    type: magnetic_sep
    params: {duration_s: 1}
  - id: 6
    name: Rock
    type: rock
    params: {duration_s: 1}
  - id: 7
    name: Wait
    type: wait
    params: {duration_s: 0}
  - id: 8
    name: Comment
    type: comment
    notes: "annotation"
"""

    def _run(self, yaml_str=None, skip=None):
        from src.core.protocol_parser import ProtocolParser
        from src.core.orchestrator import Orchestrator
        doc  = ProtocolParser().load_string(yaml_str or self.PROTOCOL_YAML)
        orch = Orchestrator(config={}, simulate=True)
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            rec = orch.execute(doc, runs_dir=Path(td),
                               confirm_before_run=False, skip_steps=skip or [])
        return rec

    def test_all_steps_complete(self):
        rec = self._run()
        self.assertEqual(rec.status, "completed")
        self.assertEqual(len([s for s in rec.steps if s.status == "completed"]),
                         len(rec.steps))

    def test_skip_steps_excluded(self):
        rec = self._run(skip=[2, 4])
        completed_ids = [s.step_id for s in rec.steps if s.status == "completed"]
        self.assertNotIn(2, completed_ids)
        self.assertNotIn(4, completed_ids)

    def test_run_record_has_all_step_fields(self):
        rec = self._run()
        for step in rec.steps:
            self.assertIsNotNone(step.step_id)
            self.assertIsNotNone(step.step_name)
            self.assertGreater(step.ended_at, step.started_at)

    def test_run_record_saved_as_json(self):
        from src.core.protocol_parser import ProtocolParser
        from src.core.orchestrator import Orchestrator
        import tempfile
        doc  = ProtocolParser().load_string(self.PROTOCOL_YAML)
        orch = Orchestrator(config={}, simulate=True)
        with tempfile.TemporaryDirectory() as td:
            orch.execute(doc, runs_dir=Path(td), confirm_before_run=False)
            saved = list(Path(td).glob("*.json"))
            self.assertEqual(len(saved), 1)
            with open(saved[0]) as f:
                data = json.load(f)
        self.assertIn("run_id", data)
        self.assertIn("steps", data)

    def test_step_failure_sets_record_failed(self):
        from src.core.protocol_parser import ProtocolParser
        from src.core.orchestrator import Orchestrator
        import tempfile
        doc  = ProtocolParser().load_string(self.PROTOCOL_YAML)
        orch = Orchestrator(config={}, simulate=True)
        # Poison vortex step by making it throw
        original = orch._step_vortex
        def bad_vortex(step):
            raise RuntimeError("Vortex motor fault")
        orch._step_vortex = bad_vortex
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(RuntimeError):
                orch.execute(doc, runs_dir=Path(td), confirm_before_run=False)
            saved = list(Path(td).glob("*.json"))
            with open(saved[0]) as f:
                data = json.load(f)
        self.assertEqual(data["status"], "failed")
        self.assertGreater(len(data["errors"]), 0)

    def test_manual_step_skipped_in_simulate(self):
        yaml_str = self.PROTOCOL_YAML.replace(
            "  - id: 8\n    name: Comment\n    type: comment\n    notes: \"annotation\"",
            "  - id: 8\n    name: Manual\n    type: manual\n    notes: \"Do this manually\""
        )
        rec = self._run(yaml_str)
        self.assertEqual(rec.status, "completed")


# ══════════════════════════════════════════════════════════════════════════════
# Protocol Parser – edge cases
# ══════════════════════════════════════════════════════════════════════════════

class TestProtocolParserEdgeCases(unittest.TestCase):

    def _parse(self, yml):
        from src.core.protocol_parser import ProtocolParser
        return ProtocolParser().load_string(yml)

    def test_minimal_protocol_no_materials(self):
        doc = self._parse("""
metadata: {name: Minimal}
steps:
  - id: 1
    name: Wait
    type: wait
    params: {duration_s: 1}
""")
        self.assertEqual(len(doc.reagents), 0)
        self.assertEqual(len(doc.steps), 1)

    def test_equipment_as_string_list(self):
        doc = self._parse("""
metadata: {name: Test}
equipment:
  - opentrons
  - centrifuge
steps: []
""")
        names = [e.name for e in doc.equipment]
        self.assertIn("opentrons", names)
        self.assertIn("centrifuge", names)

    def test_required_equipment_names_excludes_optional(self):
        doc = self._parse("""
metadata: {name: Test}
equipment:
  - name: opentrons
    required: true
  - name: rocker
    required: false
steps: []
""")
        self.assertIn("opentrons", doc.required_equipment_names)
        self.assertNotIn("rocker", doc.required_equipment_names)

    def test_step_property_accessors(self):
        doc = self._parse("""
metadata: {name: Test}
steps:
  - id: 1
    name: Vortex step
    type: vortex
    params:
      rpm: 2500
      duration_s: 45
""")
        step = doc.steps[0]
        self.assertEqual(step.rpm, 2500)
        self.assertEqual(step.duration_s, 45)

    def test_validate_against_hardware_returns_missing(self):
        from src.core.protocol_parser import ProtocolParser
        doc = self._parse("""
metadata: {name: Test}
equipment:
  - opentrons
  - centrifuge
  - vortex
steps: []
""")
        missing = ProtocolParser().validate_against_hardware(
            doc, available_equipment=["opentrons"]
        )
        self.assertIn("centrifuge", missing)
        self.assertIn("vortex", missing)

    def test_validate_against_hardware_empty_when_all_present(self):
        from src.core.protocol_parser import ProtocolParser
        doc = self._parse("""
metadata: {name: Test}
equipment:
  - opentrons
steps: []
""")
        missing = ProtocolParser().validate_against_hardware(
            doc, available_equipment=["opentrons", "centrifuge"]
        )
        self.assertEqual(missing, [])

    def test_safety_level_defaults_to_bsl1(self):
        doc = self._parse("metadata: {name: Default}\nsteps: []")
        from src.core.protocol_parser import SafetyLevel
        self.assertEqual(doc.metadata.safety_level, SafetyLevel.BSL1)

    def test_n_inversions_default(self):
        doc = self._parse("""
metadata: {name: T}
steps:
  - id: 1
    name: Invert
    type: invert_tube
    params: {}
""")
        self.assertEqual(doc.steps[0].n_inversions, 10)   # default


# ══════════════════════════════════════════════════════════════════════════════
# Risk Assessor – comprehensive
# ══════════════════════════════════════════════════════════════════════════════

class TestRiskAssessorComprehensive(unittest.TestCase):

    def _assess(self, yml):
        from src.core.protocol_parser import ProtocolParser
        from src.core.risk_assessment import RiskAssessor
        doc = ProtocolParser().load_string(yml)
        return RiskAssessor().assess(doc)

    def test_no_hazards_overall_low(self):
        from src.core.risk_assessment import Severity
        r = self._assess("""
metadata: {name: T, safety_level: BSL1}
materials:
  reagents:
    - name: Water
      volume_ul: 10
      hazard_class: none
steps: []
""")
        self.assertEqual(r.overall_risk, Severity.LOW)

    def test_biohazard_reagent_adds_chemical_risk(self):
        from src.core.risk_assessment import HazardCategory
        r = self._assess("""
metadata: {name: T, safety_level: BSL1}
materials:
  reagents:
    - name: Blood
      volume_ul: 200
      hazard_class: biohazard
steps: []
""")
        cats = [ri.category for ri in r.risks]
        self.assertIn(HazardCategory.CHEMICAL, cats)

    def test_bsl3_blocks_clearance(self):
        r = self._assess("""
metadata: {name: T, safety_level: BSL3}
steps: []
""")
        self.assertFalse(r.clearance)
        self.assertTrue(any("BSO" in note or "BIOSAFETY" in note.upper()
                            for note in r.notes))

    def test_centrifuge_adds_mechanical_risk(self):
        from src.core.risk_assessment import HazardCategory
        r = self._assess("""
metadata: {name: T}
equipment:
  - centrifuge
steps: []
""")
        cats = [ri.category for ri in r.risks]
        self.assertIn(HazardCategory.MECHANICAL, cats)

    def test_high_temp_step_adds_thermal_risk(self):
        from src.core.risk_assessment import HazardCategory
        r = self._assess("""
metadata: {name: T}
equipment: []
steps:
  - id: 1
    name: Hot incubation
    type: incubate
    params: {temperature_c: 80, duration_s: 600}
""")
        cats = [ri.category for ri in r.risks]
        self.assertIn(HazardCategory.THERMAL, cats)

    def test_low_temp_step_no_thermal_risk(self):
        from src.core.risk_assessment import HazardCategory
        r = self._assess("""
metadata: {name: T}
steps:
  - id: 1
    name: Cold incubation
    type: incubate
    params: {temperature_c: 37, duration_s: 600}
""")
        cats = [ri.category for ri in r.risks]
        self.assertNotIn(HazardCategory.THERMAL, cats)

    def test_flammable_reagent_includes_fire_ppe(self):
        r = self._assess("""
metadata: {name: T}
materials:
  reagents:
    - name: EtOH
      volume_ul: 500
      hazard_class: flammable
steps: []
""")
        ppe_items = [p.item for p in r.ppe]
        self.assertTrue(any("lab coat" in p.lower() or "glove" in p.lower()
                            for p in ppe_items))

    def test_waste_streams_generated_for_biohazard(self):
        r = self._assess("""
metadata: {name: T, safety_level: BSL2}
steps: []
""")
        self.assertGreater(len(r.waste_streams), 0)
        labels = [w.label for w in r.waste_streams]
        self.assertTrue(any("biohaz" in l.lower() for l in labels))

    def test_export_json(self, tmp_path=None):
        import tempfile
        from src.core.protocol_parser import ProtocolParser
        from src.core.risk_assessment import RiskAssessor
        doc = ProtocolParser().load_string("metadata: {name: T}\nsteps: []")
        rep = RiskAssessor().assess(doc)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = Path(f.name)
        RiskAssessor().export_json(rep, path)
        with open(path) as f:
            data = json.load(f)
        self.assertIn("protocol_name", data)
        self.assertIn("overall_risk", data)
        path.unlink()


# ══════════════════════════════════════════════════════════════════════════════
# Resource Manager
# ══════════════════════════════════════════════════════════════════════════════

class TestResourceManagerMocked(unittest.TestCase):

    def _parse(self, yml):
        from src.core.protocol_parser import ProtocolParser
        return ProtocolParser().load_string(yml)

    def test_stock_add_and_check(self):
        from src.core.resource_manager import ResourceManager
        rm = ResourceManager()
        rm.add_stock("PBS", 5000, unit="uL", location="Fridge A")
        doc = self._parse("""
metadata: {name: T}
materials:
  reagents:
    - name: PBS
      volume_ul: 100
      hazard_class: none
steps: []
""")
        report = rm.generate_material_report(doc, safety_buffer=1.0)
        self.assertEqual(report.reagents[0]["Stock Status"], "OK")

    def test_low_stock_flagged(self):
        from src.core.resource_manager import ResourceManager
        rm = ResourceManager()
        rm.add_stock("PBS", 50, unit="uL")   # only 50µL
        doc = self._parse("""
metadata: {name: T}
materials:
  reagents:
    - name: PBS
      volume_ul: 200           # needs 200µL (×1.15 = 230µL)
      hazard_class: none
steps: []
""")
        report = rm.generate_material_report(doc, safety_buffer=1.15)
        status = report.reagents[0]["Stock Status"]
        self.assertIn("LOW", status)
        self.assertGreater(len(report.warnings), 0)

    def test_not_in_inventory_is_missing(self):
        from src.core.resource_manager import ResourceManager
        rm = ResourceManager()
        doc = self._parse("""
metadata: {name: T}
materials:
  reagents:
    - name: MysteryReagent
      volume_ul: 50
      hazard_class: none
steps: []
""")
        report = rm.generate_material_report(doc)
        self.assertIn("MysteryReagent", report.missing)

    def test_consume_decrements_stock(self):
        from src.core.resource_manager import ResourceManager
        rm = ResourceManager()
        rm.add_stock("Ethanol", 10000, unit="uL")
        rm.consume("Ethanol", 500)
        self.assertAlmostEqual(
            rm._inventory["ethanol"].quantity_available, 9500, places=0
        )

    def test_consume_does_not_go_negative(self):
        from src.core.resource_manager import ResourceManager
        rm = ResourceManager()
        rm.add_stock("Water", 100, unit="uL")
        rm.consume("Water", 999999)
        self.assertEqual(rm._inventory["water"].quantity_available, 0)

    def test_excel_export_creates_file(self):
        import tempfile
        from src.core.resource_manager import ResourceManager
        rm = ResourceManager()
        doc = self._parse("""
metadata: {name: ExcelTest}
materials:
  reagents:
    - name: PBS
      volume_ul: 100
      hazard_class: none
consumables:
  - name: Tubes
    quantity: 4
steps:
  - id: 1
    name: Wait
    type: wait
    params: {duration_s: 1}
""")
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
            path = Path(f.name)
        rm.export_excel(doc, path)
        self.assertTrue(path.exists())
        self.assertGreater(path.stat().st_size, 0)
        path.unlink()


# ══════════════════════════════════════════════════════════════════════════════
# Edge cases & error handling
# ══════════════════════════════════════════════════════════════════════════════

class TestEdgeCases(unittest.TestCase):

    def test_vortex_serial_write_exception_does_not_crash(self):
        """If serial write fails mid-operation, status goes to ERROR."""
        from src.equipment.peripherals.vortex import Vortex
        from src.equipment.base_equipment import EquipmentConfig
        cfg = EquipmentConfig(name="vortex", port="/dev/ttyUSB1", simulate=False)
        s = MagicMock()
        s.is_open = True
        s.readline.return_value = b"IKA\r\n"
        s.write.side_effect = [None, OSError("device disconnected")]
        with patch("serial.Serial", return_value=s):
            v = Vortex(cfg)
            v.connect()  # uses first write call
        # Second write (set_speed) should surface the error
        with self.assertRaises(OSError):
            v.set_speed(1000)

    def test_opentrons_http_timeout_raises(self):
        from src.equipment.opentrons_controller import OpentronsController
        from src.equipment.base_equipment import EquipmentConfig
        import requests
        cfg = EquipmentConfig(name="ot", ip_address="192.168.1.50", simulate=False)
        with patch("requests.Session") as MockSession:
            session = MockSession.return_value
            session.get.side_effect = requests.exceptions.Timeout("timed out")
            ot = OpentronsController(cfg)
            result = ot.connect()
        self.assertFalse(result)

    def test_centrifuge_rpm_below_min_clamped(self):
        from src.equipment.peripherals.centrifuge import Centrifuge
        from src.equipment.base_equipment import EquipmentConfig
        cfg = EquipmentConfig(name="cent", port="/dev/ttyUSB2", simulate=False)
        s = _fake_serial_response([b""])
        with patch("serial.Serial", return_value=s):
            c = Centrifuge(cfg)
            c.connect()
        s.write.reset_mock()
        c.configure(rpm=-500, duration_s=30)   # negative rpm → clamped to 100
        cmds = [w[0][0].decode().strip() for w in s.write.call_args_list]
        speed_cmd = next((c for c in cmds if c.startswith("SR")), None)
        self.assertIsNotNone(speed_cmd)
        rpm_val = int(speed_cmd.split()[1])
        self.assertGreaterEqual(rpm_val, 100)

    def test_safety_monitor_callback_exception_does_not_crash(self):
        """A broken callback must not prevent other callbacks from running."""
        from src.safety.safety_monitor import SafetyFaultType
        mon = __import__("src.safety.safety_monitor",
                         fromlist=["SafetyMonitor"]).SafetyMonitor({})
        broken_cb = Mock(side_effect=RuntimeError("callback error"))
        good_cb   = Mock()
        mon.add_callback(broken_cb)
        mon.add_callback(good_cb)
        mon._set_fault(SafetyFaultType.EQUIPMENT_ERROR, "dev", "err")
        good_cb.assert_called_once()   # still called despite broken_cb

    def test_arm_move_safe_raises_after_timeout(self):
        from src.core.orchestrator import Orchestrator
        orch = Orchestrator(config={}, simulate=True)
        # Inject a safety monitor that always blocks
        mon = MagicMock()
        mon.can_arm_move.return_value = False
        mon.get_active_faults.return_value = ["ESTOP"]
        orch._safety = mon
        with self.assertRaises(RuntimeError):
            orch._arm_move_safe()


# ══════════════════════════════════════════════════════════════════════════════
# API endpoints
# ══════════════════════════════════════════════════════════════════════════════

class TestAPIEndpoints(unittest.TestCase):

    def _client(self):
        from fastapi.testclient import TestClient
        from src.ui.api import create_app
        app = create_app(config={}, simulate=True)
        return TestClient(app)

    def test_health_returns_ok(self):
        c = self._client()
        r = c.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")
        self.assertTrue(r.json()["simulate"])

    def test_equipment_status_returns_all_devices(self):
        c = self._client()
        r = c.get("/equipment/status")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        for device in ["opentrons", "robotic_arm", "vortex", "centrifuge"]:
            self.assertIn(device, data)

    def test_validate_valid_protocol(self):
        c = self._client()
        yaml_content = b"""
metadata: {name: Test, safety_level: BSL1}
materials:
  reagents:
    - name: PBS
      volume_ul: 100
      hazard_class: none
consumables: []
equipment: []
steps:
  - id: 1
    name: Wait
    type: wait
    params: {duration_s: 1}
"""
        r = c.post(
            "/protocol/validate",
            files={"file": ("test.yaml", io.BytesIO(yaml_content), "application/yaml")},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["valid"])
        self.assertEqual(body["steps"], 1)

    def test_validate_malformed_protocol_returns_400(self):
        c = self._client()
        r = c.post(
            "/protocol/validate",
            files={"file": ("bad.yaml", io.BytesIO(b"not: valid: yaml: ["), "text/plain")},
        )
        self.assertEqual(r.status_code, 400)

    def test_run_submission_returns_202(self):
        c = self._client()
        yaml_content = b"""
metadata: {name: Test}
steps:
  - id: 1
    name: Wait
    type: wait
    params: {duration_s: 0}
"""
        r = c.post(
            "/run",
            files={"file": ("proto.yaml", io.BytesIO(yaml_content), "text/plain")},
        )
        self.assertEqual(r.status_code, 202)
        body = r.json()
        self.assertIn("run_id", body)
        self.assertEqual(body["status"], "queued")

    def test_get_run_unknown_returns_404(self):
        c = self._client()
        r = c.get("/run/nonexistent-run-id")
        self.assertEqual(r.status_code, 404)

    def test_list_runs_returns_lists(self):
        c = self._client()
        r = c.get("/runs")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("active", body)
        self.assertIn("saved", body)

    def test_estop_endpoint(self):
        c = self._client()
        r = c.post("/estop")
        self.assertEqual(r.status_code, 200)
        self.assertIn("estop_triggered", r.json()["status"])

    def test_estop_reset_wrong_confirmation(self):
        c = self._client()
        r = c.post("/estop/reset", json={"confirm": "wrong string"})
        self.assertEqual(r.status_code, 400)

    def test_estop_reset_correct_confirmation(self):
        c = self._client()
        r = c.post("/estop/reset",
                   json={"confirm": "I_CONFIRM_EQUIPMENT_IS_SAFE"})
        self.assertEqual(r.status_code, 200)

    def test_abort_run_not_found(self):
        c = self._client()
        r = c.post("/run/nonexistent/abort")
        self.assertEqual(r.status_code, 404)


# ══════════════════════════════════════════════════════════════════════════════
# E-stop propagation
# ══════════════════════════════════════════════════════════════════════════════

class TestEStopPropagation(unittest.TestCase):

    def test_estop_reaches_all_registered_equipment(self):
        from src.safety.safety_monitor import SafetyMonitor
        mon = SafetyMonitor({"max_incubator_temp_c": 95, "max_centrifuge_rpm": 14000})
        devices = {}
        for name in ["opentrons", "arm", "vortex", "centrifuge", "incubator"]:
            m = MagicMock()
            m.config.simulate = False
            devices[name] = m
            mon.register(name, m)
        mon.trigger_estop("test_propagation")
        for name, dev in devices.items():
            dev.emergency_stop.assert_called_once(), \
                f"emergency_stop not called on {name}"

    def test_estop_clears_all_equipment_statuses(self):
        from src.equipment.base_equipment import EquipmentStatus
        # Simulate mode devices all enter ESTOP state
        from src.equipment.peripherals.vortex import Vortex
        from src.equipment.base_equipment import EquipmentConfig
        cfg = EquipmentConfig(name="vortex", simulate=True)
        v   = Vortex(cfg)
        v.connect()
        v.start()
        self.assertEqual(v.status, EquipmentStatus.RUNNING)
        v.emergency_stop()
        self.assertEqual(v.status, EquipmentStatus.ESTOP)

    def test_centrifuge_estop_brakes_immediately(self):
        from src.equipment.peripherals.centrifuge import Centrifuge
        from src.equipment.base_equipment import EquipmentConfig, EquipmentStatus
        cfg = EquipmentConfig(name="cent", port="/dev/ttyUSB2", simulate=False)
        s = _fake_serial_response([b""])
        with patch("serial.Serial", return_value=s):
            c = Centrifuge(cfg)
            c.connect()
            c.start()
        s.write.reset_mock()
        c.emergency_stop()
        cmds = [w[0][0].decode().strip() for w in s.write.call_args_list]
        self.assertIn("BR", cmds)

    def test_incubator_estop_calls_stop_all(self):
        from src.equipment.peripherals.incubator import Incubator
        from src.equipment.base_equipment import EquipmentConfig
        cfg = EquipmentConfig(name="inc", port="/dev/ttyUSB3", simulate=False)
        s = _fake_serial_response([b"GET TEMP 37.0\r\n"] * 10)
        with patch("serial.Serial", return_value=s):
            inc = Incubator(cfg)
            inc.connect()
        s.write.reset_mock()
        inc.emergency_stop()
        cmds = [w[0][0].decode().strip() for w in s.write.call_args_list]
        self.assertIn("STOP ALL", cmds)

    def test_magnetic_separator_estop_disengages_magnet(self):
        from src.equipment.peripherals.magnetic_separator import MagneticSeparator, MagnetState
        from src.equipment.base_equipment import EquipmentConfig
        cfg = EquipmentConfig(name="mag", simulate=False,
                              extra={"relay_backend": "serial",
                                     "relay_port": "/dev/ttyUSB5",
                                     "relay_channel": 1})
        s = MagicMock(); s.is_open = True
        with patch("serial.Serial", return_value=s):
            m = MagneticSeparator(cfg)
            m.connect()
            m.engage()
        m.emergency_stop()
        self.assertEqual(m.state, MagnetState.DISENGAGED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
