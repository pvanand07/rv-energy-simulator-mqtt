"""
app/sim_db.py
─────────────────────────────────────────────────────────────────────────────
Standalone SQLite database for the simulator's appliance configuration.

Tables:
  sim_appliances      — full appliance config with electrical params
  sim_schedules       — multiple time-window schedules per appliance
  sim_weather_plans   — 7-day weather plans for simulation
  mqtt_settings       — MQTT broker and topic configuration (single row)
"""
from __future__ import annotations
import json, aiosqlite
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timezone

SIM_DB = str(Path(__file__).parent.parent / "data" / "simulator.db")
Path(SIM_DB).parent.mkdir(parents=True, exist_ok=True)

_DDL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sim_appliances (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    cat             TEXT    NOT NULL DEFAULT 'medium',
    icon            TEXT    NOT NULL DEFAULT '🔌',
    clr             TEXT    NOT NULL DEFAULT '#0A84FF',
    voltage_v       REAL    NOT NULL DEFAULT 120.0,
    current_a       REAL    NOT NULL DEFAULT 1.0,
    power_factor    REAL    NOT NULL DEFAULT 0.95,
    efficiency_pct  REAL    NOT NULL DEFAULT 90.0,
    duty_cycle_pct  REAL    NOT NULL DEFAULT 100.0,
    avg_power_w     REAL    NOT NULL DEFAULT 0.0,
    always_on       INTEGER NOT NULL DEFAULT 0,
    cycle_pattern   TEXT    NOT NULL DEFAULT 'constant',
    on_state        INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Multiple schedule windows per appliance
-- e.g. coffee machine: 06:00-06:30 (12 min use), 11:00-11:30 (15 min), 16:00-16:30 (10 min)
CREATE TABLE IF NOT EXISTS sim_schedules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    appliance_id    INTEGER NOT NULL REFERENCES sim_appliances(id) ON DELETE CASCADE,
    start_hhmm      TEXT    NOT NULL DEFAULT '08:00',
    end_hhmm        TEXT    NOT NULL DEFAULT '09:00',
    active_minutes  REAL    NOT NULL DEFAULT 30.0,
    days_of_week    TEXT    NOT NULL DEFAULT '1111111',
    label           TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sim_weather_plans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_name       TEXT    NOT NULL DEFAULT 'Default Plan',
    day_index       INTEGER NOT NULL DEFAULT 0,
    condition       TEXT    NOT NULL DEFAULT 'sunny',
    temp_c          REAL    NOT NULL DEFAULT 22.0,
    cloud_pct       REAL    NOT NULL DEFAULT 10.0,
    sunrise_hhmm    TEXT    NOT NULL DEFAULT '06:30',
    sunset_hhmm     TEXT    NOT NULL DEFAULT '19:30',
    UNIQUE(plan_name, day_index)
);

CREATE TABLE IF NOT EXISTS mqtt_settings (
    id              INTEGER PRIMARY KEY DEFAULT 1,
    broker_host     TEXT NOT NULL DEFAULT 'localhost',
    broker_port     INTEGER NOT NULL DEFAULT 1883,
    username        TEXT NOT NULL DEFAULT '',
    password        TEXT NOT NULL DEFAULT '',
    client_id       TEXT NOT NULL DEFAULT 'rv_simulator',
    base_topic      TEXT NOT NULL DEFAULT 'rv/energy',
    publish_rate_s  REAL NOT NULL DEFAULT 1.0,
    send_appliances INTEGER NOT NULL DEFAULT 1,
    send_battery    INTEGER NOT NULL DEFAULT 1,
    send_solar      INTEGER NOT NULL DEFAULT 1,
    send_summary    INTEGER NOT NULL DEFAULT 1,
    send_weather    INTEGER NOT NULL DEFAULT 1,
    qos             INTEGER NOT NULL DEFAULT 0,
    retain          INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sim_profiles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    label           TEXT    NOT NULL DEFAULT 'Custom',
    description     TEXT    NOT NULL DEFAULT '',
    icon            TEXT    NOT NULL DEFAULT '👤',
    -- Energy behaviour multipliers (1.0 = baseline)
    load_factor     REAL    NOT NULL DEFAULT 1.0,
    -- Appliance on/off overrides stored as JSON array of {id, on}
    app_overrides   TEXT    NOT NULL DEFAULT '[]',
    -- Scenario and experience settings
    experience      TEXT    NOT NULL DEFAULT 'normal',
    occupants       INTEGER NOT NULL DEFAULT 2,
    battery_cap_kwh REAL    NOT NULL DEFAULT 45.0,
    panel_kwp       REAL    NOT NULL DEFAULT 0.8,
    -- Metadata
    is_preset       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO sim_profiles
  (name,label,description,icon,load_factor,experience,occupants,battery_cap_kwh,panel_kwp,is_preset) VALUES
  ('solo',        'Solo Traveler',    'Minimal footprint. Expert usage. One person, low consumption.',         '🧑',  0.65, 'expert', 1, 30.0, 0.6, 1),
  ('couple',      'Couple',           'Two people, moderate usage. Mix of high and low consumption habits.',   '👫',  0.85, 'normal', 2, 40.0, 0.8, 1),
  ('family',      'Family Trip',      '4+ people, all appliances active including AC and laundry.',            '👨‍👩‍👧', 1.30, 'new',    4, 60.0, 1.2, 1),
  ('expert',      'Expert Off-Grid',  'Seasoned boondocker. Optimised schedules, maximum solar efficiency.',   '🧑‍💻', 0.78, 'expert', 2, 50.0, 1.4, 1),
  ('remote_work', 'Remote Worker',    'Electronics priority. Laptop, monitors, Starlink all day.',             '💻',  0.90, 'normal', 1, 40.0, 1.0, 1),
  ('fulltime',    'Full-Time Living', 'Everything running. Washer, AC, cooking. Home away from home.',         '🏡',  1.20, 'normal', 2, 80.0, 2.0, 1),
  ('weekend',     'Weekend Warrior',  'Short trips, high power use concentrated in 2 days.',                   '⛺',  1.10, 'new',    3, 35.0, 0.6, 1);

INSERT OR IGNORE INTO mqtt_settings (id) VALUES (1);

-- ── Global simulation settings (single row, id=1) ─────────────────────────
-- Persists battery, solar, and SOC configuration across sessions.
-- Updated whenever the user changes any field in the Simulate tab.
CREATE TABLE IF NOT EXISTS sim_settings (
    id              INTEGER PRIMARY KEY DEFAULT 1,
    battery_cap_kwh REAL    NOT NULL DEFAULT 45.0,
    panel_kwp       REAL    NOT NULL DEFAULT 0.8,
    starting_soc    REAL    NOT NULL DEFAULT 87.0,  -- stored as percent (0-100)
    load_factor     REAL    NOT NULL DEFAULT 1.0,
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO sim_settings (id) VALUES (1);

"""

# ─── Default appliances with realistic RV cycling patterns ───────────────────
_DEFAULTS = [
    # always_on devices use cycle_pattern to model realistic power modes
    dict(name="Refrigerator",        cat="high",   icon="❄️", clr="#5E9EFF", voltage_v=120, current_a=1.25, power_factor=0.95, efficiency_pct=85, duty_cycle_pct=25,  avg_power_w=42,  always_on=1, cycle_pattern="compressor"),
    dict(name="WiFi Router",         cat="low",    icon="📶", clr="#30D158", voltage_v=12,  current_a=1.25, power_factor=0.90, efficiency_pct=85, duty_cycle_pct=100, avg_power_w=12,  always_on=1, cycle_pattern="wifi_traffic"),
    dict(name="HMI Tablet",          cat="low",    icon="📱", clr="#5E9EFF", voltage_v=12,  current_a=1.00, power_factor=0.91, efficiency_pct=88, duty_cycle_pct=100, avg_power_w=5,   always_on=1, cycle_pattern="display_sleep"),
    dict(name="Security Cameras",    cat="low",    icon="📷", clr="#636366", voltage_v=12,  current_a=2.50, power_factor=0.92, efficiency_pct=90, duty_cycle_pct=100, avg_power_w=8,   always_on=1, cycle_pattern="motion_sensor"),
    dict(name="Starlink",            cat="low",    icon="🛰️", clr="#30D158", voltage_v=48,  current_a=1.25, power_factor=0.94, efficiency_pct=88, duty_cycle_pct=100, avg_power_w=50,  always_on=1, cycle_pattern="network_load"),
    dict(name="Electric Stove",      cat="high",   icon="🔥", clr="#FF6B6B", voltage_v=240, current_a=8.33, power_factor=1.00, efficiency_pct=98, duty_cycle_pct=100, avg_power_w=1500, always_on=0, cycle_pattern="constant"),
    dict(name="Water Heater (Main)", cat="high",   icon="🚿", clr="#FF6B6B", voltage_v=240, current_a=12.5, power_factor=1.00, efficiency_pct=97, duty_cycle_pct=100, avg_power_w=800,  always_on=0, cycle_pattern="thermostat"),
    dict(name="Air Conditioner",     cat="high",   icon="🌬️", clr="#5AC8F5", voltage_v=240, current_a=9.17, power_factor=0.88, efficiency_pct=85, duty_cycle_pct=70,  avg_power_w=1600, always_on=0, cycle_pattern="thermostat"),
    dict(name="Coffee Machine",      cat="medium", icon="☕", clr="#AC8E68", voltage_v=120, current_a=9.17, power_factor=0.97, efficiency_pct=88, duty_cycle_pct=100, avg_power_w=800,  always_on=0, cycle_pattern="constant"),
    dict(name="Microwave",           cat="medium", icon="📡", clr="#FF9F0A", voltage_v=120, current_a=10.0, power_factor=0.96, efficiency_pct=90, duty_cycle_pct=100, avg_power_w=1000, always_on=0, cycle_pattern="constant"),
    dict(name="LED Lights",          cat="low",    icon="💡", clr="#FFD60A", voltage_v=12,  current_a=16.67,power_factor=0.95, efficiency_pct=92, duty_cycle_pct=90,  avg_power_w=80,  always_on=0, cycle_pattern="dimmer"),
    dict(name="Fan(s)",              cat="low",    icon="🌀", clr="#5AC8F5", voltage_v=120, current_a=0.63, power_factor=0.88, efficiency_pct=82, duty_cycle_pct=80,  avg_power_w=45,  always_on=0, cycle_pattern="constant"),
    dict(name="TV",                  cat="low",    icon="📺", clr="#5E9EFF", voltage_v=120, current_a=1.00, power_factor=0.92, efficiency_pct=82, duty_cycle_pct=100, avg_power_w=90,  always_on=0, cycle_pattern="constant"),
    dict(name="Washer",              cat="high",   icon="🫧", clr="#BF5AF2", voltage_v=120, current_a=7.08, power_factor=0.92, efficiency_pct=90, duty_cycle_pct=100, avg_power_w=600,  always_on=0, cycle_pattern="wash_cycle"),
]

# Default schedules for non-always-on appliances
_DEFAULT_SCHEDULES = {
    "Electric Stove":      [("07:00","07:30",20,"Breakfast"),("12:00","12:45",25,"Lunch"),("18:00","19:00",35,"Dinner")],
    "Water Heater (Main)": [("06:00","06:45",30,"Morning shower"),("18:00","18:45",25,"Evening shower")],
    "Air Conditioner":     [("10:00","21:00",360,"Day cooling")],
    "Coffee Machine":      [("06:00","06:30",12,"Morning"),("11:00","11:30",15,"Mid-morning"),("16:00","16:30",10,"Afternoon")],
    "Microwave":           [("07:00","07:30",8,"Breakfast"),("12:00","12:30",10,"Lunch"),("18:30","19:00",8,"Dinner")],
    "LED Lights":          [("18:30","23:30",300,"Evening lights")],
    "Fan(s)":              [("08:00","22:00",600,"Daytime ventilation")],
    "TV":                  [("17:00","22:30",240,"Evening entertainment")],
    "Washer":              [("09:00","10:30",75,"Morning wash")],
}

@asynccontextmanager
async def get_sim_db():
    async with aiosqlite.connect(SIM_DB) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        yield conn

async def init_sim_db():
    async with aiosqlite.connect(SIM_DB) as conn:
        conn.row_factory = aiosqlite.Row
        await conn._execute(conn._conn.executescript, _DDL)
        await conn.commit()
        cur = await conn.execute("SELECT COUNT(*) as n FROM sim_appliances")
        row = await cur.fetchone()
        if row["n"] == 0:
            for d in _DEFAULTS:
                cur2 = await conn.execute(
                    """INSERT INTO sim_appliances
                       (name,cat,icon,clr,voltage_v,current_a,power_factor,
                        efficiency_pct,duty_cycle_pct,avg_power_w,always_on,cycle_pattern)
                       VALUES (:name,:cat,:icon,:clr,:voltage_v,:current_a,:power_factor,
                               :efficiency_pct,:duty_cycle_pct,:avg_power_w,:always_on,:cycle_pattern)""", d)
                aid = cur2.lastrowid
                scheds = _DEFAULT_SCHEDULES.get(d["name"], [])
                for s_h, e_h, mins, lbl in scheds:
                    await conn.execute(
                        "INSERT INTO sim_schedules (appliance_id,start_hhmm,end_hhmm,active_minutes,label) VALUES (?,?,?,?,?)",
                        (aid, s_h, e_h, mins, lbl))
            # Default 7-day weather plan
            conditions = ["sunny","sunny","partly","overcast","rainy","partly","sunny"]
            temps      = [22,23,20,18,16,19,22]
            for i, (cond, temp) in enumerate(zip(conditions, temps)):
                await conn.execute(
                    "INSERT OR IGNORE INTO sim_weather_plans (plan_name,day_index,condition,temp_c,cloud_pct) VALUES (?,?,?,?,?)",
                    ("Default Plan", i, cond, temp, {"sunny":5,"partly":40,"overcast":75,"rainy":90}[cond]))
            await conn.commit()

def row_to_dict(row) -> dict:
    d = dict(row)
    if "on_state" in d: d["on"] = bool(d.pop("on_state"))
    if "always_on" in d: d["always_on"] = bool(d["always_on"])
    return d
