# OpenLab – Bill of Materials

> Recommended hardware for a full automated in-vitro experiment workstation.
> Prices are indicative list prices (USD, 2025–2026). Request quotes for academic/volume pricing.

---

## 1. Liquid Handling Robot

| Item | Model | Qty | Unit Price | Total | Notes |
|------|-------|-----|------------|-------|-------|
| Opentrons Flex | Opentrons Flex OT-3 | 1 | $24,900 | $24,900 | Preferred – gripper, 8-ch & 96-ch pipettes |
| *or* Opentrons OT-2 | Opentrons OT-2 | 1 | $9,995 | $9,995 | Budget option; no built-in gripper |
| Gen2 Single-channel Pipette P300 | Opentrons | 1 | $999 | $999 | For precise small volumes |
| Gen2 8-channel Pipette P300 | Opentrons | 1 | $1,299 | $1,299 | High-throughput plates |
| OT-2 300µL Tips (10×96) | Opentrons | 2 | $49 | $98 | Per protocol |
| OT-2 20µL Tips (10×96) | Opentrons | 2 | $49 | $98 | |
| **Subtotal (Flex option)** | | | | **~$27,400** | |

---

## 2. Collaborative Robotic Arm

| Item | Model | Qty | Unit Price | Total | Notes |
|------|-------|-----|------------|-------|-------|
| Collaborative Robot Arm | Universal Robots UR3e | 1 | $35,000 | $35,000 | 3 kg payload, 500mm reach, force/torque sensing, ISO 10218-1 |
| *or* UR5e | Universal Robots UR5e | 1 | $45,000 | $45,000 | 5 kg, 850mm reach – for larger/heavier equipment |
| Gripper – Tube Body | Robotiq 2F-85 | 1 | $4,900 | $4,900 | Adaptive, fits 0.5–50 mL tubes |
| Gripper – Cap Handler | Custom 3D-printed or Robotiq FT 300 | 1 | $500 | $500 | For cap tightening with torque sensing |
| UR Control Box & Teach Pendant | Included with UR | 1 | – | included | e-Series controller |
| Arm Mounting Base / Pedestal | Custom weld or 80/20 aluminium | 1 | $800 | $800 | Must be level and rigidly fixed |
| **Subtotal** | | | | **~$41,200** | |

---

## 3. Peripheral Instruments

| Item | Model | Qty | Unit Price | Total | Notes |
|------|-------|-----|------------|-------|-------|
| Vortex Mixer | IKA Vortex 3 Electronic | 1 | $450 | $450 | RS-232 control, 0–3000 rpm |
| Centrifuge | Eppendorf 5430R | 1 | $9,500 | $9,500 | 14,000 rpm, refrigerated, RS-232 |
| *or* Budget centrifuge | Eppendorf 5430 (non-refrig) | 1 | $5,800 | $5,800 | No temp control |
| Incubator / Thermoshaker | Inheco ODTC (On-Deck Thermal Cycler) | 1 | $6,500 | $6,500 | RS-232, 4–100°C, 0–1400 rpm shake |
| *or* Eppendorf ThermoMixer C | Eppendorf | 1 | $3,200 | $3,200 | Simpler, widely available |
| Magnetic Separator | Alpaqua Magnum FLX | 1 | $1,800 | $1,800 | Motorised, fits 1.5 & 2 mL tubes |
| *or* DynaMag-2 | Thermo Fisher | 1 | $350 | $350 | Manual, relay-actuated in this system |
| Tube Rocker / Nutator | Stuart SB3 Tube Roller | 1 | $550 | $550 | On/Off relay control |
| **Subtotal** | | | | **~$19,300** | |

---

## 4. Workstation Infrastructure

| Item | Qty | Unit Price | Total | Notes |
|------|-----|------------|-------|-------|
| Lab Bench (1800×750mm) | 1 | $1,500 | $1,500 | Chemical-resistant surface |
| Safety Fence / Light Curtain | 1 | $2,500 | $2,500 | ISO/TS 15066 compliant for shared workspace |
| USB-Relay Board (8-ch, CH340) | 2 | $25 | $50 | Controls rocker, mag separator, aux |
| Raspberry Pi 4 (8GB) + case + PSU | 1 | $120 | $120 | GPIO E-stop, relay control (optional) |
| Managed Ethernet Switch (8-port) | 1 | $80 | $80 | Local lab network for all instruments |
| UPS (1000VA) | 1 | $350 | $350 | Power backup – prevents mid-run failure |
| Emergency Stop Button (NC, mushroom) | 2 | $25 | $50 | Hardwired to UR safety input |
| Cable Management / Drag Chain | 1 | $200 | $200 | Keep arm cables tidy |
| 80/20 Aluminium Frame (deck) | 1 | $600 | $600 | Instrument positioning and fixturing |
| Serial-to-USB adapters (FTDI) | 4 | $20 | $80 | For vortex, centrifuge, incubator, rocker |
| Power Strip (hospital-grade) | 2 | $80 | $160 | Surge protected |
| **Subtotal** | | | | **~$5,690** | |

---

## 5. Safety Equipment (Lab-Wide)

| Item | Qty | Unit Price | Total | Notes |
|------|-----|------------|-------|-------|
| Biosafety Cabinet Class II A2 | 1 | $8,000 | $8,000 | Required for BSL2 aerosol steps |
| Chemical Fume Hood | 1 | $4,000 | $4,000 | For organic solvents (ethanol, etc.) |
| Autoclave (table-top) | 1 | $5,500 | $5,500 | Biohazardous waste sterilisation |
| Eyewash Station | 1 | $300 | $300 | Plumbed or self-contained |
| Chemical Spill Kit | 1 | $150 | $150 | |
| Biohazard Spill Kit | 1 | $80 | $80 | |
| Fire Extinguisher (CO2) | 1 | $150 | $150 | For electrical + flammable solvent fires |
| First Aid Kit | 1 | $60 | $60 | |
| **Subtotal** | | | | **~$18,240** | |

---

## 6. Computing & Software

| Item | Qty | Unit Price | Total | Notes |
|------|-----|------------|-------|-------|
| Workstation PC (Linux/Ubuntu 22.04) | 1 | $1,200 | $1,200 | i7/Ryzen 7, 32GB RAM, SSD |
| Monitor (27") | 1 | $300 | $300 | |
| OpenLab Software (this repo) | 1 | $0 | $0 | Open source |
| Opentrons App | 1 | $0 | $0 | Free |
| UR Polyscope license | 1 | included | included | With robot |
| **Subtotal** | | | | **~$1,500** | |

---

## 7. Consumables (per run – DNA extraction example)

| Item | Per-Run Qty | Unit Cost | Per-Run Cost |
|------|-------------|-----------|--------------|
| 1.5mL Eppendorf SafeLock tubes | 8 | $0.25 | $2.00 |
| 200µL barrier tips | 40 | $0.08 | $3.20 |
| 20µL barrier tips | 16 | $0.05 | $0.80 |
| Nitrile gloves | 1 pair | $0.30 | $0.30 |
| Autoclave bags | 1 | $0.50 | $0.50 |
| **Per-run total** | | | **~$6.80** |

---

## Summary – Total Capital Cost

| Category | Cost (USD) |
|----------|-----------|
| Liquid Handling Robot (Flex) | $27,400 |
| Collaborative Robot Arm (UR3e) | $41,200 |
| Peripheral Instruments | $19,300 |
| Workstation Infrastructure | $5,690 |
| Safety Equipment | $18,240 |
| Computing | $1,500 |
| **Total (Flex config)** | **~$113,330** |
| **Total (OT-2 config, budget)** | **~$98,425** |

> Installation, commissioning, and training: add 15–20% to capital cost.
> Annual service contracts: ~10% of instrument cost.

---

## Hardware Alternatives

| Role | Primary Recommendation | Budget Alternative | Notes |
|------|----------------------|-------------------|-------|
| Liquid handler | Opentrons Flex | Opentrons OT-2 | OT-2 needs arm to compensate for no gripper |
| Robotic arm | UR3e | Doosan M0609, Franka FR3 | UR has best Python ecosystem (ur_rtde) |
| Centrifuge | Eppendorf 5430R | Hettich MIKRO 185 | RS-232 on 5430R makes automation easier |
| Vortex | IKA Vortex 3 | Scientific Industries Vortex-Genie 2 | IKA has NAMUR serial protocol |
| Incubator/Thermomixer | Inheco ODTC | Eppendorf ThermoMixer C | Inheco designed for liquid handlers |
| Magnetic separator | Alpaqua Magnum FLX | Thermo DynaMag-2 + relay | Motorised preferred for repeatability |
| Rocker | Stuart SB3 | Any relay-controlled model | Any rocker with relay control works |
| E-stop interface | UR safety I/O | Pilz PNOZ safety relay | Use UR's native safety-rated inputs |

---

## Software Stack

| Component | Technology |
|-----------|-----------|
| Orchestration | Python 3.10+ (this repo) |
| Opentrons control | Opentrons HTTP API v5 |
| UR arm control | `ur_rtde` 1.5+ (RTDE protocol) |
| Gripper control | Robotiq URCap (URScript) |
| Serial peripherals | `pyserial` |
| REST API | FastAPI + Uvicorn |
| Protocol format | YAML (this repo's schema) |
| Data logging | Loguru + JSON run records |
| Reports | Pandas + openpyxl |
| Safety | Custom watchdog thread |
| OS | Ubuntu 22.04 LTS |
