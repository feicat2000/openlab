# OpenLab Hardware Integration Guide

## Physical Layout

```
┌──────────────────────────────────────────────────────────────────────────┐
│                          LAB BENCH (1800 × 750 mm)                       │
│                                                                          │
│  ┌─────────────┐   ┌──────────────────────────────────────┐              │
│  │  UR3e Arm   │   │        Opentrons Flex / OT-2          │              │
│  │  (pedestal) │   │  [Tip Rack] [Tube Rack] [Reservoir]  │              │
│  │      ┌──┐   │   │  [Plate  ] [Collection] [Waste     ] │              │
│  │      │  │◄──┼───┤  Handoff pos: [0.42,-0.08,0.31]     │              │
│  │      └──┘   │   └──────────────────────────────────────┘              │
│  └─────────────┘                                                          │
│       │                                                                   │
│       │  (arm reach)                                                      │
│       ↓                                                                   │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  │
│  │  Vortex  │  │Centrifuge│  │ Incubator│  │Mag. Sep. │  │  Rocker  │  │
│  │IKA Vtx3  │  │Eppend.   │  │Inheco    │  │Alpaqua   │  │Stuart SB3│  │
│  │[0.20,0.30│  │5430R     │  │ODTC      │  │MagnumFLX │  │[0.25,0.36│  │
│  │ 0.25]    │  │[0.10,0.42│  │[0.00,0.45│  │[0.35,0.25│  │ 0.20]    │  │
│  └──────────┘  │ 0.20]    │  │ 0.20]    │  │ 0.20]    │  └──────────┘  │
│                └──────────┘  └──────────┘  └──────────┘                 │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  WORKSTATION PC  │  Ethernet Switch  │  UPS  │  USB Relay Board   │  │
│  └────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
```

## Network / Communication Map

```
Workstation PC (Ubuntu 22.04)
│
├── Ethernet ──► Opentrons OT-2/Flex     (192.168.1.x or USB-DHCP 169.254.x.x)
│                HTTP API port 31950
│
├── Ethernet ──► UR3e Controller         (192.168.1.10)
│                RTDE port 30004
│                Dashboard port 29999
│
├── USB → /dev/ttyUSB1 ──► Vortex (IKA RS-232, 9600 7E1)
├── USB → /dev/ttyUSB2 ──► Centrifuge (Eppendorf RS-232, 9600 8N1)
├── USB → /dev/ttyUSB3 ──► Incubator (Inheco RS-232, 9600 8N1)
├── USB → /dev/ttyUSB5 ──► USB Relay Board (CH340, relay for mag sep + rocker)
│
└── USB → Robotiq 2F-85 Gripper (URCap on UR controller, or USB adapter)
```

## Step-by-Step Hardware Setup

### 1. Opentrons OT-2 / Flex

```bash
# USB connection (OT-2)
ip addr show | grep 169.254      # should see 169.254.x.x
curl http://169.254.10.10:31950/health

# Network connection (fixed IP)
# Set in Opentrons app: Settings → Network → Set static IP
curl http://192.168.1.50:31950/health
```

Update `.env`:
```
OPENTRONS_HOST=169.254.10.10
```

### 2. Universal Robots UR3e

1. Power on, go to teach pendant
2. **Initialize**: Hamburger menu → Initialize → ON
3. Set IP: Setup → Network → Set to `192.168.1.10` (match your LAN)
4. Enable **Remote Control**: Setup → Safety → Remote Control → Enable
5. Install **Robotiq URCap** for gripper (USB stick from Robotiq)

Test from PC:
```bash
nc -z 192.168.1.10 30004 && echo "RTDE OK"
nc -z 192.168.1.10 29999 && echo "Dashboard OK"
```

Update `.env`:
```
UR_HOST=192.168.1.10
```

### 3. Calibrating Arm Positions

Run the teach utility (interactive):
```bash
python tools/teach_positions.py --arm-ip 192.168.1.10
```

This will:
- Jog the arm to each named position manually
- Record TCP pose from `rtde_receive.getActualTCPPose()`
- Write calibrated values to `config/hardware_config.yaml` → `arm_positions`

**Critical positions to calibrate:**
- `opentrons_handoff` – exactly above each tube rack slot
- `vortex` – on the vortex rubber tip, tube aligned
- `centrifuge_load` / `centrifuge_unload` – inside rotor hole
- `magnetic_separator` – tube seated in magnet slot
- `cap_tightener` – tube held in fixture, cap aligned with gripper

### 4. Serial Peripherals

Identify serial ports:
```bash
ls /dev/ttyUSB*
dmesg | grep ttyUSB | tail -20
```

For consistent naming add to `/etc/udev/rules.d/99-openlab.rules` (setup.sh does this automatically):
```
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", SYMLINK+="vortex", MODE="0666"
```

Test vortex connection:
```bash
python -c "
import serial, time
s = serial.Serial('/dev/ttyUSB1', 9600, timeout=2)
s.write(b'IN_NAME\r\n')
time.sleep(0.5)
print(s.readline())
s.close()
"
```

### 5. USB Relay Board (for Mag Sep + Rocker)

Uses CH340 USB-HID relay board (8-channel, ~$20).

Protocol: `[0xA0, channel, state, checksum]`
- Channel 1 → Magnetic separator
- Channel 2 → Tube rocker

Test:
```bash
python -c "
import serial
r = serial.Serial('/dev/ttyUSB5', 9600)
r.write(bytes([0xA0, 1, 1, 0xA2]))  # Relay 1 ON
"
```

### 6. Safety Wiring

**Hardware E-stop (mandatory for UR arm):**
```
Emergency Stop Button (NC) → UR Controller Safety I/O
  Pin EI0 (Safety Input 0) + Pin GND

In UR Polyscope:
  Setup → Safety → Safety I/O → Emergency Stop → Input 0
```

**Software E-stop** is handled by `SafetyMonitor.trigger_estop()`.

For Raspberry Pi GPIO E-stop button:
- Connect NC button between BCM pin 17 and GND
- Set `EMERGENCY_STOP_GPIO_PIN=17` in `.env`

## Running the Platform

```bash
# Activate environment
source activate_openlab.sh

# 1. Validate protocol (no hardware)
python main.py validate protocols/dna_extraction.yaml

# 2. Show materials and risk only
python main.py materials protocols/dna_extraction.yaml
python main.py risk protocols/dna_extraction.yaml

# 3. Simulation run (safe, no hardware)
python main.py run protocols/dna_extraction.yaml --simulate

# 4. Real hardware run (after calibration)
python main.py run protocols/dna_extraction.yaml --no-simulate

# 5. REST API (for LIMS integration / remote control)
python main.py api --port 8000
# → open http://localhost:8000/docs for Swagger UI

# 6. Run tests
pytest tests/ -v
```

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `curl 169.254.x.x` fails | OT-2 not detected | Check USB cable, run `ip addr` |
| UR arm mode ≠ 7 | Robot not initialized | Teach pendant → Initialize |
| Serial timeout | Wrong port or baud | `dmesg \| grep ttyUSB`, check BOM |
| Arm move blocked | Centrifuge still spinning | Wait for RPM < 50, check safety monitor |
| `ur_rtde` import error | Library not installed | `pip install ur-rtde` (needs real hardware or mock) |
| Cap not tightening | Torque limit too low | Increase `cap_torque_Nm` in protocol |
| Temp not reached | Incubator not warming | Check RS-232 cable, verify `STOP ALL` not sent |

## Writing New Protocols

1. Copy `protocols/dna_extraction.yaml` as template
2. Edit `metadata`, `materials`, `consumables`, `equipment`, `steps`
3. Validate: `python main.py validate protocols/my_protocol.yaml`
4. Test in simulation: `python main.py run protocols/my_protocol.yaml --simulate`
5. Calibrate any new arm positions using `tools/teach_positions.py`
6. Run on hardware: `python main.py run protocols/my_protocol.yaml --no-simulate`

### Protocol Step Reference

| Step type | Key params | Example |
|-----------|-----------|---------|
| `pipette` | source, destination, volume_ul, mix_repetitions | Transfer 200µL A1→B1 |
| `vortex` | rpm, duration_s, tube_position | 2000 rpm, 30s |
| `centrifuge` | rpm, duration_s, temp_c, tube_position | 13000 rpm, 60s, 4°C |
| `incubate` | temperature_c, duration_s, shake_rpm, tube_position | 37°C, 600s, 300 rpm |
| `magnetic_sep` | duration_s | 120s engagement |
| `rock` | duration_s, rpm, tube_position | 300s |
| `tighten_cap` | tube_position | Force-controlled |
| `loosen_cap` | tube_position | Reverse screw |
| `invert_tube` | tube_position, n_times | 10 inversions |
| `transfer_tube` | source_pose, dest_pose | Arm picks + places |
| `temperature_set` | temperature_c, wait_for_temp | Set 37°C, wait |
| `wait` | duration_s | Timed pause |
| `comment` | notes | Log annotation |
| `manual` | notes | Pause for human |
