# OpenLab

A Python platform for controlling an Opentrons liquid handler + a robotic
arm to run automated in-vitro wet-lab experiments: pipetting, vortexing,
incubation, temperature control, magnetic separation, rocking, tube
tightening/inverting, and centrifugation.

## Goal

Given a protocol as input, the system:
- generates a materials list, consumables list, and risk assessment
- drives the Opentrons + robotic arm to execute the protocol end to end

## Layout

- `src/core` — orchestration/scheduling
- `src/equipment` — device drivers (Opentrons, robotic arm, relay board for
  mag separator/rocker)
- `src/safety` — risk assessment
- `src/inventory` — consumables/materials tracking
- `src/diagnostics`, `src/audit` — run diagnostics and audit trail
- `src/nlp` — protocol parsing
- `src/notify` — run notifications
- `src/ui` — user interface
- `src/data`, `src/calibration` — data handling and instrument calibration
- `protocols/` — example protocols (DNA extraction, plasmid miniprep
  96-well, sandwich ELISA 384-well, VHH expression + Ni purification
  96-well)
- `config/hardware_config.yaml` — hardware setup (Opentrons IP/port, robotic
  arm, relay backend); defaults to `simulation_mode: true`
- `docs/BOM.md` — recommended hardware bill of materials
- `docs/hardware_guide.md` — hardware setup guide

## Setup

```bash
./setup.sh          # creates venv, installs requirements
python main.py       # run
```

Sensitive values (IPs, credentials) go in a local `.env` (not committed);
`config/hardware_config.yaml` holds non-sensitive hardware layout and
defaults to simulation mode until real hardware is configured.
