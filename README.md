# RV Energy Simulator — Standalone Package

**Elevatics AI** — Standalone RV energy simulation + optional MQTT telemetry  
*Simulate realistic appliance data, view it in real time in the browser, and optionally publish to an external MQTT broker.*

---

## Architecture Overview

A **single FastAPI process** serves the dashboard, the simulator UI, REST APIs, and Server-Sent Events (SSE). The simulation engine runs in-process; live steps update the dashboard over SSE. **MQTT is optional**: `SimPublisher` publishes to a broker you configure (for example Mosquitto on your network)—there is no embedded broker in this package.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  simulator.py  (port 5001)                                                    │
│                                                                               │
│  GET /              → Real-time dashboard (charts, SOC, stability)            │
│  GET /sim           → Appliance configurator, scheduler, live sim controls    │
│                                                                               │
│  app/sim_engine.py  → 2880-step day simulation                               │
│  app/sim_db.py      → SQLite (data/simulator.db)                             │
│  app/mqtt_service.py → SimPublisher only (optional external MQTT)            │
└──────────────────────────────────────────────────────────────────────────────┘
         │                                    │
         │ SSE /api/stream, /api/simulate/live/stream
         ▼                                    ▼
    Browser (dashboard + simulator)    Optional: MQTT broker (external)
                                              rv/energy/… topics
```

---

## Quick Start

```bash
cd rv-energy-simulator-mqtt
pip install -r requirements.txt

python simulator.py          # → http://localhost:5001
```

- **Dashboard:** [http://localhost:5001/](http://localhost:5001/) — real-time charts and telemetry  
- **Simulator:** [http://localhost:5001/sim](http://localhost:5001/sim) — configure appliances, weather, and click **▶ Start Live Sim**

Live data flows from the simulator to the dashboard over SSE in the same process—no second app or MQTT hop required for the UI.

---

## What Each File Does

| File | Purpose |
|---|---|
| `simulator.py` | FastAPI app: dashboard (`/`), configurator (`/sim`), simulation, SSE, dashboard REST, optional MQTT publish |
| `app/sim_db.py` | SQLite schema: appliances, schedules, weather plans, MQTT settings, profiles, sim settings |
| `app/sim_engine.py` | 2880-step simulation engine with realistic cycle patterns |
| `app/mqtt_service.py` | `SimPublisher` only — publishes telemetry to a configured external broker |
| `templates/simulator.html` | Configurator UI (appliance cards + scheduler) |
| `templates/dashboard.html` | Real-time dashboard (charts, gauge, history) |

---

## Features

### Appliance Configurator (`/sim`)

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

### Optional MQTT Telemetry

- **No embedded broker** — point `broker_host` / `broker_port` at **Mosquitto** (or another broker) on your LAN or cloud
- Configurable publish rate (0.1–60 seconds per step)
- Per-topic enable/disable (appliances, battery, solar, summary, weather)
- QoS and retain supported via settings
- Configurable base topic (default: `rv/energy`)

### MQTT Topics (when publishing is enabled)

```
rv/energy/registry           → appliance metadata (names, icons, …)
rv/energy/appliances/{id}   → {ts, id, voltage_v, current_a, power_w, …}
rv/energy/battery            → {ts, soc_pct, kwh, reserve_hit, net_kw}
rv/energy/solar              → {ts, solar_kw, load_kw}
rv/energy/summary            → {ts, soc_pct, load_kw, solar_kw}
```

### Real-Time Dashboard (`/`)

- **Stability Score gauge** (0–10) computed from live step telemetry
- **Battery SOC** with 10% and 20% reserve markers on chart
- **Rolling charts**: SOC trend, Solar vs Load power
- **Per-appliance line charts**: V, A, W updated each step
- **Live appliance table**: current V/A/W for every device
- **Alert strip**: low SOC, reserve hit, stability warnings
- **History tab**: buffered SOC and power data (`/api/history/…`)
- **Connection modal**: legacy UI may call `POST /api/connect` (no-op in single-app mode); broker target for MQTT is configured on the **Simulator** MQTT tab (`/sim`)

---

## API Reference

All routes are served on **port 5001** (prefix `/api` unless noted).

### Simulator & configuration

```
GET    /api/appliances                      List all appliances with schedules
POST   /api/appliances                      Create appliance
PUT    /api/appliances/{id}                 Update appliance
PATCH  /api/appliances/{id}/rename          Rename appliance
DELETE /api/appliances/{id}                 Delete appliance
POST   /api/appliances/{id}/toggle          Toggle on/off state

GET    /api/appliances/{id}/schedules       List schedules for appliance
POST   /api/appliances/{id}/schedules       Add schedule window
PUT    /api/schedules/{sid}                 Update schedule window
DELETE /api/schedules/{sid}                 Delete schedule window

GET    /api/weather/{plan_name}             Get 7-day weather plan
PUT    /api/weather/{plan_name}/{day}       Update one day

GET    /api/profiles                        List profiles
GET    /api/profiles/{name}                 Get profile
POST   /api/profiles                        Create profile
PUT    /api/profiles/{name}                 Update custom profile
DELETE /api/profiles/{name}                 Delete custom profile
POST   /api/profiles/{name}/apply           Apply profile overrides

GET    /api/settings                        Global sim settings (battery, panel, SOC, …)
PUT    /api/settings                        Save sim settings

PUT    /api/mqtt/settings                   Save MQTT publish settings (broker, topics, …)

POST   /api/simulate                        Run multi-day simulation (batch)
POST   /api/simulate/live/start             Start live streaming simulation
POST   /api/simulate/live/stop              Stop live simulation
GET    /api/simulate/live/status            Current sim state
GET    /api/simulate/live/stream            SSE: live sim steps (and unified telemetry)
```

### Dashboard (same app)

```
GET    /api/status                          Latest telemetry snapshot
GET    /api/appliances/live                 Appliances with latest buffered points
GET    /api/history/battery?n=200           Battery SOC history (last N)
GET    /api/history/solar?n=200             Solar/load history
GET    /api/history/appliance/{id}?n=200    Per-appliance history
GET    /api/stability                       Live stability score breakdown
GET    /api/stream                          SSE stream (dashboard events; same pipeline as live sim)
POST   /api/connect                         No-op (kept for UI compatibility)
```

---

## Integration with RV Energy Intelligence

The simulator can **publish** to any MQTT broker so downstream services subscribe to `rv/energy/#` (or your base topic).

```
rv-energy-simulator (port 5001)
        │
        │  optional MQTT publish
        ▼
   your Mosquitto / cloud broker
        │
        ├── rv-energy-intelligence (or other consumers)
        │        POST /api/simulate  ← aggregated daily data, etc.
        │
        └── External dashboards / Home Assistant / monitoring
```

Stability-style scoring in the web dashboard uses a **live** four-pillar formula aligned with the batch **SI score** from `app/sim_engine.py` for end-of-day runs; exact numeric parity between “live estimate” and “full day aggregate” is not guaranteed.

---

## Docker

`docker-compose.yml` runs the **simulator** service on port **5001** (and optional **dbgate** for SQLite browsing). There is no separate dashboard container.

```bash
docker compose up -d
# Dashboard + simulator: http://localhost:5001/ and http://localhost:5001/sim
```

---

## Remote / split deployment

**Typical:** run `python simulator.py` on one machine and open `http://<host>:5001/` in a browser on another device on the same network.

**MQTT-only consumers:** run Mosquitto (or your broker) where reachable, set **MQTT settings** on `/sim` to that host/port, and start live simulation—subscribers receive `rv/energy/…` without needing the web UI on the same host.

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
