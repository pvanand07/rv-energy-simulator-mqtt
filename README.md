# RV Energy Simulator — Standalone Package

**Elevatics AI** — Standalone RV energy simulation + MQTT telemetry system  
*Simulate realistic appliance data, stream it over MQTT, visualise in real time.*

---

## Architecture Overview

```
┌─────────────────────────────────┐    MQTT (port 1883)    ┌────────────────────────────────┐
│  SIMULATOR   (port 5001)         │ ─────────────────────► │  DASHBOARD   (port 5002)        │
│                                  │                         │                                 │
│  • Appliance card configurator   │  rv/energy/battery      │  • Real-time SOC gauge          │
│  • Multi-window scheduler        │  rv/energy/solar        │  • Per-appliance V/A/W charts   │
│  • 7-day weather planner         │  rv/energy/appliances/* │  • Stability Score (0–10)       │
│  • 9 realistic cycle patterns    │  rv/energy/summary      │  • 24h history buffer           │
│  • Live simulation (step-by-step)│  rv/energy/weather      │  • Alert system                 │
│  • Embedded MQTT broker          │ ◄───────────────────────│  • MQTT subscriber              │
└─────────────────────────────────┘    SSE + REST            └────────────────────────────────┘
         │                                                              │
         │                                                              ▼
         └──────────────────── SQLite (data/simulator.db) ─────────────┘
                               Appliance configs + schedules
                               Weather plans + MQTT settings
```

---

## Quick Start

```bash
cd rv-energy-simulator
pip install -r requirements.txt

# Terminal 1 — Appliance configurator + simulator
python simulator.py          # → http://localhost:5001

# Terminal 2 — Real-time dashboard
python broker_dashboard.py   # → http://localhost:5002
```

Then on the Simulator at **localhost:5001**, click **▶ Start Live Sim** — data flows instantly to the Dashboard.

---

## What Each File Does

| File | Port | Purpose |
|---|---|---|
| `simulator.py` | 5001 | FastAPI app: appliance configurator, scheduler, simulation, MQTT publisher |
| `broker_dashboard.py` | 5002 | FastAPI app: MQTT subscriber, real-time charts, stability score display |
| `app/sim_db.py` | — | SQLite schema: appliances, schedules, weather plans, MQTT settings |
| `app/sim_engine.py` | — | 2880-step simulation engine with 9 realistic cycle patterns |
| `app/mqtt_service.py` | — | Embedded MQTT broker (amqtt) + paho publisher + paho subscriber |
| `templates/simulator.html` | — | Apple-design configurator UI (appliance cards + scheduler) |
| `templates/dashboard.html` | — | Real-time dashboard with line charts, gauge, alert strip |

---

## Features

### Appliance Configurator (localhost:5001)

- **Card UI** for each appliance showing all electrical parameters
- **Editable fields:** Voltage (V), Current (A), Power Factor, Efficiency (%), Duty Cycle (%), Avg Power (W)
- **Computed live:** Apparent VA, Real Watts, Battery Draw W
- **9 cycle patterns** for realistic simulation (see below)
- **Multi-window scheduler** — add multiple time windows per appliance
  - Example: Coffee machine — 06:00–06:30 (12 min), 11:00–11:30 (15 min), 16:00–16:30 (10 min)
  - Days-of-week per schedule window (Mon–Sun toggles)
  - Delete individual windows or the whole appliance
- **Add / Delete appliances** via UI
- **Always-on appliances** (Fridge, WiFi, HMI, Cameras) use cycle patterns instead of schedules

### Realistic Cycle Patterns

| Pattern | Appliance | Behaviour |
|---|---|---|
| `compressor` | Refrigerator | 24% ON at 3.5× rated; 76% at 0.06× fan-only |
| `wifi_traffic` | WiFi Router | 5% burst at 2.5–4×; 95% at 0.35–0.45× |
| `display_sleep` | HMI Tablet | Night 0.05×; Day 0.7×; 10% random interaction spikes |
| `motion_sensor` | Cameras | 25% day / 4% night active at 1.2×; standby at 0.12× |
| `network_load` | Starlink | Base 0.5–0.75× + 8% daytime heavy usage |
| `thermostat` | Water heater, AC | 42% ON cycling with random variation |
| `dimmer` | LED lights | Gaussian brightness variation around 0.8× |
| `wash_cycle` | Washer | Wash 0.8× → Rinse 0.4× → Spin 1.3× → Done |
| `constant` | Most appliances | 1.0× with ±1% noise |

### Weather Planner

- 7-day plan with per-day settings:
  - Weather condition (sunny / partly cloudy / overcast / rainy)
  - Temperature (°C) — feeds LiFePO4 temperature derating
  - Cloud cover (%) — computed into irradiance factor
  - Sunrise + sunset times — defines solar window
- Solar irradiance uses sine arch between sunrise and sunset
- Multi-day simulation carries SOC forward across days

### Battery Model

- LiFePO4 temperature derating (4 bands: <0°C→0.70, <10°C→0.85, <20°C→0.95, ≥20°C→1.00)
- **10% hard floor** — battery never depletes below 10% (cell protection)
- **20% soft warning** — alerts fire when SOC drops to 20%
- 2880-step (30-second) simulation resolution
- 0.95× usable fraction (LiFePO4 safe operating range)

### MQTT Telemetry

- **Embedded broker** (amqtt, port 1883) — no external mosquitto needed
- Configurable publish rate (0.1–60 seconds/step)
- Per-topic enable/disable (appliances, battery, solar, summary, weather)
- QoS 0/1/2 support
- Configurable base topic (default: `rv/energy`)

### MQTT Topics

```
rv/energy/appliances/{id}   → {ts, id, voltage_v, current_a, power_w}
rv/energy/battery            → {ts, soc_pct, kwh, reserve_hit, net_kw}
rv/energy/solar              → {ts, solar_kw, load_kw}
rv/energy/summary            → {ts, soc_pct, load_kw, solar_kw}
rv/energy/weather            → {ts, condition, temp_c, cloud_pct}
rv/energy/control            → inbound commands (start/stop/reset)
```

### Real-Time Dashboard (localhost:5002)

- **Stability Score gauge** (0–10) computed live from MQTT telemetry
- **Battery SOC** with 10% and 20% reserve markers on chart
- **Rolling 60-minute charts**: SOC trend, Solar vs Load power
- **Per-appliance line charts**: V, A, W updated on each MQTT message
- **Live appliance table**: current V/A/W for every device with share bar
- **Alert strip**: auto-computed from incoming data (low SOC, reserve hit, inverter overload)
- **History tab**: full buffer of all received SOC and power data
- **MQTT reconnect**: configure broker host/port/topic from UI

---

## API Reference

### Simulator (port 5001)

```
GET    /api/appliances              List all appliances with schedules
POST   /api/appliances              Create appliance
PUT    /api/appliances/{id}         Update appliance
DELETE /api/appliances/{id}         Delete appliance
POST   /api/appliances/{id}/toggle  Toggle on/off state

GET    /api/appliances/{id}/schedules       List schedules for appliance
POST   /api/appliances/{id}/schedules       Add schedule window
PUT    /api/schedules/{sid}                 Update schedule window
DELETE /api/schedules/{sid}                 Delete schedule window

GET    /api/weather/{plan_name}             Get 7-day weather plan
PUT    /api/weather/{plan_name}/{day}       Update one day

GET    /api/mqtt/settings                   Get MQTT settings
PUT    /api/mqtt/settings                   Save MQTT settings

POST   /api/simulate                        Run multi-day simulation (batch)
POST   /api/simulate/live/start             Start live streaming simulation
POST   /api/simulate/live/stop              Stop live simulation
GET    /api/simulate/live/status            Get current sim state
GET    /api/simulate/live/stream            SSE stream of live sim steps
```

### Dashboard (port 5002)

```
GET    /api/status                          Latest telemetry snapshot
GET    /api/history/battery?n=200           Battery SOC history (last N)
GET    /api/history/solar?n=200             Solar/load history
GET    /api/history/appliance/{id}?n=200    Per-appliance V/A/W history
GET    /api/appliances                      Active appliances with latest data
GET    /api/stability                       Live stability score breakdown
GET    /api/stream                          SSE stream of MQTT updates
POST   /api/connect                         Reconnect to different MQTT broker
```

---

## Integration with RV Energy Intelligence

This simulator feeds data to the main `rv-energy-intelligence` resource calculator:

```
rv-energy-simulator (port 5001)
        │
        │  MQTT  rv/energy/#
        ▼
   localhost:1883
        │
        ├── broker_dashboard.py (port 5002)  ← real-time analytics
        │
        └── rv-energy-intelligence (port 5000)  ← stability score + history
             POST /api/simulate  ← receive aggregated daily data
```

For direct integration: the stability score computation in `broker_dashboard.py` 
(`_compute_si()`) uses the same four-pillar formula as `app/stability.py` in the main app.

---

## Run on Separate Machines

The simulator and dashboard are designed to run on different devices on the same network:

**Machine A (RV Jetson AGX Orin) — Simulator:**
```bash
python simulator.py
# Accessible at http://JETSON_IP:5001
```

**Machine B (Laptop / Tablet) — Dashboard:**
```bash
# Edit broker_host in UI to point to Machine A's IP
python broker_dashboard.py
# Open http://localhost:5002
# Set MQTT broker: JETSON_IP:1883
```

---

## Default Appliances

| Appliance | Category | Always-on | Pattern |
|---|---|---|---|
| Refrigerator | high | ✓ | compressor |
| WiFi Router | low | ✓ | wifi_traffic |
| HMI Tablet | low | ✓ | display_sleep |
| Security Cameras | low | ✓ | motion_sensor |
| Starlink | low | ✓ | network_load |
| Electric Stove | high | — | constant |
| Water Heater (Main) | high | — | thermostat |
| Air Conditioner | high | — | thermostat |
| Coffee Machine | medium | — | constant |
| Microwave | medium | — | constant |
| LED Lights | low | — | dimmer |
| Fan(s) | low | — | constant |
| TV | low | — | constant |
| Washer | high | — | wash_cycle |
