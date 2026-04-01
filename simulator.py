"""
simulator.py  —  RV Energy Simulator  (port 5001)
─────────────────────────────────────────────────────────────────────────────
FastAPI application providing:
  • Appliance card configurator with inline name editing + multi-window scheduler
  • User profile manager (preset + custom profiles)
  • 1–7 day simulation engine with realistic cycle patterns
  • Weather planner with sunrise/sunset per day
  • Real-time MQTT publisher with configurable topics and rate
  • Live simulation streaming via SSE
  • All configuration persisted in SQLite (data/simulator.db)

Run:
    python simulator.py
    Open: http://localhost:5001
"""
from __future__ import annotations
import asyncio, json, logging, time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from markupsafe import Markup

import uvicorn
from fastapi import FastAPI, APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from app.sim_db   import init_sim_db, get_sim_db, row_to_dict, SIM_DB
from app.sim_engine import run_day_simulation, run_multi_day
from app.mqtt_service import (
    SimPublisher,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s %(message)s")
logger = logging.getLogger("rv.simulator")

# ── Live simulation state ─────────────────────────────────────────────────────
_sim_state = {
    "running": False, "day": 0, "step": 0,
    "soc_pct": 87.0, "battery_kwh": 0.0, "solar_kw": 0.0, "load_kw": 0.0,
    "si_score": 0.0, "si_grade": "F", "publisher": None, "task": None,
    "rate_s": 1.0, "current_step": None,
    "app_meta": {},   # {aid_str: {name, icon, clr, cat}} for MQTT enrichment
}
_sse_queues: list[asyncio.Queue] = []

# ── Dashboard state (merged from broker_dashboard.py) ─────────────────────────
MAX_HISTORY = 1440  # 24 h @ 1 pt/min
_dashboard_state = {
    "appliance_history": defaultdict(lambda: deque(maxlen=MAX_HISTORY)),
    "battery_history": deque(maxlen=MAX_HISTORY),
    "solar_history": deque(maxlen=MAX_HISTORY),
    "summary_history": deque(maxlen=MAX_HISTORY),
    "latest": {
        "soc_pct": 0,
        "battery_kwh": 0,
        "solar_kw": 0,
        "load_kw": 0,
        "net_kw": 0,
        "si_score": 0.0,
        "si_grade": "—",
        "reserve_hit": False,
        "last_update": "—",
        "connected": False,
    },
}


def _compute_live_si(soc_pct: float, solar_kw: float, load_kw: float, net_kw: float) -> dict:
    """Compute dashboard SI pillars from current live telemetry."""
    usable_kwh = soc_pct / 100.0 * 37.5
    daily_draw = max(abs(net_kw) if net_kw < 0 else 0.01, 0.01) * 24
    bat_days = usable_kwh / daily_draw
    cov = min(1.0, solar_kw / max(load_kw, 0.01))

    p1 = min(3.5, (bat_days / 14.0) * 3.5)
    p2 = min(3.0, cov * 3.0)
    p3 = min(2.0, max(0.0, (1.0 - load_kw / 5.0)) * 2.0)
    p4 = min(1.5, max(0.0, (soc_pct - 20.0) / 80.0) * 1.5)

    score = round(p1 + p2 + p3 + p4, 2)
    grade = ("S" if score >= 9 else "A" if score >= 8 else "B" if score >= 7 else
             "C" if score >= 6 else "D" if score >= 5 else "F")

    return {"score": score, "grade": grade, "p1": round(p1, 2), "p2": round(p2, 2), "p3": round(p3, 2), "p4": round(p4, 2)}


def _snapshot_dashboard_status() -> dict:
    latest = _dashboard_state["latest"]
    return dict(latest)


def _record_live_step(step_data: dict):
    """Update merged dashboard buffers from one simulation step."""
    ts = step_data.get("ts", "")
    soc = float(step_data.get("soc_pct", 0))
    kwh = float(step_data.get("battery_kwh", 0))
    net = float(step_data.get("net_kw", 0))
    solar_kw = float(step_data.get("solar_kw", 0))
    load_kw = float(step_data.get("load_kw", 0))
    reserve_hit = bool(step_data.get("reserve_hit", False))

    latest = _dashboard_state["latest"]
    latest.update({
        "soc_pct": soc,
        "battery_kwh": kwh,
        "solar_kw": solar_kw,
        "load_kw": load_kw,
        "net_kw": net,
        "reserve_hit": reserve_hit,
        "last_update": ts,
        "connected": True,
    })

    si = _compute_live_si(soc, solar_kw, load_kw, net)
    latest["si_score"] = si["score"]
    latest["si_grade"] = si["grade"]

    _dashboard_state["battery_history"].append({"ts": ts, "soc": soc, "kwh": kwh, "net_kw": net})
    _dashboard_state["solar_history"].append({"ts": ts, "solar_kw": solar_kw, "load_kw": load_kw})
    _dashboard_state["summary_history"].append({
        "ts": ts,
        "soc_pct": soc,
        "solar_kw": solar_kw,
        "load_kw": load_kw,
        "si_score": si["score"],
        "si_grade": si["grade"],
    })

    for aid, data in step_data.get("appliances", {}).items():
        aid_str = str(aid)
        meta = _sim_state["app_meta"].get(aid_str, {"name": f"App {aid_str}", "icon": "🔌", "clr": "#0A84FF", "cat": "medium"})
        _dashboard_state["appliance_history"][aid_str].append({
            "ts": ts,
            "v": float(data.get("v", 0)),
            "a": float(data.get("a", 0)),
            "w": float(data.get("w", 0)),
            "name": meta.get("name"),
            "icon": meta.get("icon"),
            "clr": meta.get("clr"),
            "cat": meta.get("cat"),
        })


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_sim_db()
    # Seed merged dashboard metadata from DB at boot.
    async with get_sim_db() as db:
        app_rows = await (await db.execute("SELECT * FROM sim_appliances ORDER BY id")).fetchall()
    _sync_app_meta({str(dict(r)["id"]): row_to_dict(r) for r in app_rows})
    logger.info("RV Simulator ready — http://localhost:5001")
    yield
    if _sim_state["task"] and not _sim_state["task"].done():
        _sim_state["task"].cancel()
    if _sim_state["publisher"]:
        _sim_state["publisher"].disconnect()


app = FastAPI(title="RV Energy Simulator", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_static = Path(__file__).parent / "static"
_static.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static)), name="static")

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["tojson"] = lambda v: Markup(json.dumps(v, ensure_ascii=False, default=str))

api = APIRouter(prefix="/api")


# ════════════════════════════════════════════════════════════════════════════
# APPLIANCE CRUD
# ════════════════════════════════════════════════════════════════════════════

@api.get("/appliances")
async def list_appliances():
    async with get_sim_db() as db:
        rows = await (await db.execute(
            "SELECT * FROM sim_appliances ORDER BY always_on DESC, cat, name"
        )).fetchall()
        apps = [row_to_dict(r) for r in rows]
        for a in apps:
            srows = await (await db.execute(
                "SELECT * FROM sim_schedules WHERE appliance_id=? ORDER BY start_hhmm", (a["id"],)
            )).fetchall()
            a["schedules"] = [dict(s) for s in srows]
    return apps


@api.post("/appliances")
async def create_appliance(req: Request):
    data = await req.json()
    v   = float(data.get("voltage_v", 120))
    a   = float(data.get("current_a", 1))
    pf  = float(data.get("power_factor", 0.95))
    eff = float(data.get("efficiency_pct", 90))
    dc  = float(data.get("duty_cycle_pct", 100))
    avg_w = round(v * a * pf / max(eff / 100, 0.01) * dc / 100, 1)
    async with get_sim_db() as db:
        cur = await db.execute(
            """INSERT INTO sim_appliances
               (name,cat,icon,clr,voltage_v,current_a,power_factor,efficiency_pct,
                duty_cycle_pct,avg_power_w,always_on,cycle_pattern,on_state)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)""",
            (data.get("name", "New Appliance"), data.get("cat", "medium"),
             data.get("icon", "🔌"), data.get("clr", "#0A84FF"),
             v, a, pf, eff, dc, avg_w,
             int(data.get("always_on", 0)), data.get("cycle_pattern", "constant")))
        await db.commit()
        aid = cur.lastrowid
        row = await (await db.execute("SELECT * FROM sim_appliances WHERE id=?", (aid,))).fetchone()
    result = row_to_dict(row)
    result["schedules"] = []
    _sync_app_meta({str(aid): result})
    return result


@api.put("/appliances/{aid}")
async def update_appliance(aid: int, req: Request):
    data = await req.json()
    v   = float(data.get("voltage_v", 120))
    a   = float(data.get("current_a", 1))
    pf  = float(data.get("power_factor", 0.95))
    eff = float(data.get("efficiency_pct", 90))
    dc  = float(data.get("duty_cycle_pct", 100))
    avg_w = round(v * a * pf / max(eff / 100, 0.01) * dc / 100, 1)
    async with get_sim_db() as db:
        await db.execute(
            """UPDATE sim_appliances SET name=?,cat=?,icon=?,clr=?,
               voltage_v=?,current_a=?,power_factor=?,efficiency_pct=?,
               duty_cycle_pct=?,avg_power_w=?,always_on=?,cycle_pattern=?,on_state=?
               WHERE id=?""",
            (data.get("name"), data.get("cat", "medium"), data.get("icon", "🔌"),
             data.get("clr", "#0A84FF"), v, a, pf, eff, dc, avg_w,
             int(data.get("always_on", 0)), data.get("cycle_pattern", "constant"),
             int(data.get("on_state", 1)), aid))
        await db.commit()
        row = await (await db.execute("SELECT * FROM sim_appliances WHERE id=?", (aid,))).fetchone()
    if not row:
        return JSONResponse({"error": "not found"}, 404)
    result = row_to_dict(row)
    _sync_app_meta({str(aid): result})
    return result


@api.patch("/appliances/{aid}/rename")
async def rename_appliance(aid: int, req: Request):
    """Rename-only endpoint — minimal DB write for inline name editing."""
    data = await req.json()
    name = data.get("name", "").strip()
    if not name:
        return JSONResponse({"error": "name required"}, 422)
    async with get_sim_db() as db:
        await db.execute("UPDATE sim_appliances SET name=? WHERE id=?", (name, aid))
        await db.commit()
    if str(aid) in _sim_state["app_meta"]:
        _sim_state["app_meta"][str(aid)]["name"] = name
    return {"id": aid, "name": name}


@api.delete("/appliances/{aid}")
async def delete_appliance(aid: int):
    async with get_sim_db() as db:
        await db.execute("DELETE FROM sim_appliances WHERE id=?", (aid,))
        await db.commit()
    _sim_state["app_meta"].pop(str(aid), None)
    return {"deleted": aid}


@api.post("/appliances/{aid}/toggle")
async def toggle_appliance(aid: int, req: Request):
    body = await req.json()
    async with get_sim_db() as db:
        row = await (await db.execute("SELECT on_state FROM sim_appliances WHERE id=?", (aid,))).fetchone()
        if not row:
            return JSONResponse({"error": "not found"}, 404)
        # body.on can be True/False/None (None = flip)
        new_state = body.get("on")
        if new_state is None:
            new_state = not bool(row["on_state"])
        await db.execute("UPDATE sim_appliances SET on_state=? WHERE id=?", (int(new_state), aid))
        await db.commit()
    return {"id": aid, "on": new_state}


def _sync_app_meta(apps_dict: dict):
    """Update in-memory meta from an {str_id: app_dict} mapping."""
    for aid_str, a in apps_dict.items():
        _sim_state["app_meta"][str(aid_str)] = {
            "name": a.get("name", f"App {aid_str}"),
            "icon": a.get("icon", "🔌"),
            "clr":  a.get("clr", "#0A84FF"),
            "cat":  a.get("cat", "medium"),
        }


# ── Schedules ─────────────────────────────────────────────────────────────────

@api.get("/appliances/{aid}/schedules")
async def list_schedules(aid: int):
    async with get_sim_db() as db:
        rows = await (await db.execute(
            "SELECT * FROM sim_schedules WHERE appliance_id=? ORDER BY start_hhmm", (aid,)
        )).fetchall()
    return [dict(r) for r in rows]


@api.post("/appliances/{aid}/schedules")
async def add_schedule(aid: int, req: Request):
    d = await req.json()
    async with get_sim_db() as db:
        cur = await db.execute(
            "INSERT INTO sim_schedules (appliance_id,start_hhmm,end_hhmm,active_minutes,days_of_week,label) VALUES (?,?,?,?,?,?)",
            (aid, d.get("start_hhmm", "08:00"), d.get("end_hhmm", "09:00"),
             float(d.get("active_minutes", 30)), d.get("days_of_week", "1111111"),
             d.get("label", "")))
        await db.commit()
        row = await (await db.execute("SELECT * FROM sim_schedules WHERE id=?", (cur.lastrowid,))).fetchone()
    return dict(row)


@api.put("/schedules/{sid}")
async def update_schedule(sid: int, req: Request):
    d = await req.json()
    async with get_sim_db() as db:
        await db.execute(
            "UPDATE sim_schedules SET start_hhmm=?,end_hhmm=?,active_minutes=?,days_of_week=?,label=? WHERE id=?",
            (d.get("start_hhmm", "08:00"), d.get("end_hhmm", "09:00"),
             float(d.get("active_minutes", 30)), d.get("days_of_week", "1111111"),
             d.get("label", ""), sid))
        await db.commit()
        row = await (await db.execute("SELECT * FROM sim_schedules WHERE id=?", (sid,))).fetchone()
    return dict(row) if row else JSONResponse({"error": "not found"}, 404)


@api.delete("/schedules/{sid}")
async def delete_schedule(sid: int):
    async with get_sim_db() as db:
        await db.execute("DELETE FROM sim_schedules WHERE id=?", (sid,))
        await db.commit()
    return {"deleted": sid}


# ── Weather plans ─────────────────────────────────────────────────────────────

@api.get("/weather/{plan_name}")
async def get_weather_plan(plan_name: str):
    async with get_sim_db() as db:
        rows = await (await db.execute(
            "SELECT * FROM sim_weather_plans WHERE plan_name=? ORDER BY day_index", (plan_name,)
        )).fetchall()
    return [dict(r) for r in rows]


@api.put("/weather/{plan_name}/{day_index}")
async def update_weather_day(plan_name: str, day_index: int, req: Request):
    d = await req.json()
    cloud_by_cond = {"sunny": 5, "partly": 40, "overcast": 75, "rainy": 90}
    cond = d.get("condition", "sunny")
    async with get_sim_db() as db:
        await db.execute(
            """INSERT OR REPLACE INTO sim_weather_plans
               (plan_name,day_index,condition,temp_c,cloud_pct,sunrise_hhmm,sunset_hhmm)
               VALUES (?,?,?,?,?,?,?)""",
            (plan_name, day_index, cond,
             float(d.get("temp_c", 22)),
             float(d.get("cloud_pct", cloud_by_cond.get(cond, 10))),
             d.get("sunrise_hhmm", "06:30"), d.get("sunset_hhmm", "19:30")))
        await db.commit()
    return {"ok": True}


# ── User Profiles ─────────────────────────────────────────────────────────────

@api.get("/profiles")
async def list_profiles():
    async with get_sim_db() as db:
        rows = await (await db.execute(
            "SELECT * FROM sim_profiles ORDER BY is_preset DESC, name"
        )).fetchall()
    return [dict(r) for r in rows]


@api.get("/profiles/{profile_name}")
async def get_profile(profile_name: str):
    async with get_sim_db() as db:
        row = await (await db.execute(
            "SELECT * FROM sim_profiles WHERE name=?", (profile_name,)
        )).fetchone()
    return dict(row) if row else JSONResponse({"error": "not found"}, 404)


@api.post("/profiles")
async def create_profile(req: Request):
    data = await req.json()
    name = data.get("name", "").strip().lower().replace(" ", "_")
    if not name:
        return JSONResponse({"error": "name required"}, 422)
    app_overrides = json.dumps(data.get("app_overrides", []))
    async with get_sim_db() as db:
        try:
            cur = await db.execute(
                """INSERT INTO sim_profiles
                   (name,label,description,icon,load_factor,app_overrides,
                    experience,occupants,battery_cap_kwh,panel_kwp,is_preset)
                   VALUES (?,?,?,?,?,?,?,?,?,?,0)""",
                (name, data.get("label", name.replace("_", " ").title()),
                 data.get("description", ""), data.get("icon", "👤"),
                 float(data.get("load_factor", 1.0)), app_overrides,
                 data.get("experience", "normal"), int(data.get("occupants", 2)),
                 float(data.get("battery_cap_kwh", 45.0)),
                 float(data.get("panel_kwp", 0.8))))
            await db.commit()
            row = await (await db.execute("SELECT * FROM sim_profiles WHERE id=?", (cur.lastrowid,))).fetchone()
            return dict(row)
        except Exception as e:
            return JSONResponse({"error": str(e)}, 409)


@api.put("/profiles/{profile_name}")
async def update_profile(profile_name: str, req: Request):
    data = await req.json()
    app_overrides = json.dumps(data.get("app_overrides", []))
    async with get_sim_db() as db:
        await db.execute(
            """UPDATE sim_profiles SET label=?,description=?,icon=?,load_factor=?,
               app_overrides=?,experience=?,occupants=?,battery_cap_kwh=?,panel_kwp=?
               WHERE name=? AND is_preset=0""",
            (data.get("label"), data.get("description", ""), data.get("icon", "👤"),
             float(data.get("load_factor", 1.0)), app_overrides,
             data.get("experience", "normal"), int(data.get("occupants", 2)),
             float(data.get("battery_cap_kwh", 45.0)), float(data.get("panel_kwp", 0.8)),
             profile_name))
        await db.commit()
        row = await (await db.execute("SELECT * FROM sim_profiles WHERE name=?", (profile_name,))).fetchone()
    return dict(row) if row else JSONResponse({"error": "not found or preset (read-only)"}, 404)


@api.delete("/profiles/{profile_name}")
async def delete_profile(profile_name: str):
    async with get_sim_db() as db:
        row = await (await db.execute(
            "SELECT is_preset FROM sim_profiles WHERE name=?", (profile_name,)
        )).fetchone()
        if not row:
            return JSONResponse({"error": "not found"}, 404)
        if row["is_preset"]:
            return JSONResponse({"error": "cannot delete preset profiles"}, 403)
        await db.execute("DELETE FROM sim_profiles WHERE name=?", (profile_name,))
        await db.commit()
    return {"deleted": profile_name}


@api.post("/profiles/{profile_name}/apply")
async def apply_profile(profile_name: str):
    """Apply profile overrides to live appliance on/off states in DB."""
    async with get_sim_db() as db:
        prof_row = await (await db.execute(
            "SELECT * FROM sim_profiles WHERE name=?", (profile_name,)
        )).fetchone()
        if not prof_row:
            return JSONResponse({"error": "not found"}, 404)
        profile = dict(prof_row)
        overrides = json.loads(profile.get("app_overrides") or "[]")
        for ov in overrides:
            await db.execute(
                "UPDATE sim_appliances SET on_state=? WHERE id=?",
                (int(ov.get("on", True)), ov.get("id")))
        await db.commit()
    return {"applied": profile_name, "overrides_applied": len(overrides),
            "occupants": profile["occupants"], "load_factor": profile["load_factor"],
            "battery_cap_kwh": profile["battery_cap_kwh"], "panel_kwp": profile["panel_kwp"],
            "experience": profile["experience"]}


# ── MQTT Settings ─────────────────────────────────────────────────────────────

@api.get("/settings")
async def get_sim_settings():
    """Return the persisted global simulation settings (battery, panel, SOC)."""
    async with get_sim_db() as db:
        row = await (await db.execute("SELECT * FROM sim_settings WHERE id=1")).fetchone()
    return dict(row) if row else {"battery_cap_kwh": 45.0, "panel_kwp": 0.8, "starting_soc": 87.0, "load_factor": 1.0}


@api.put("/settings")
async def save_sim_settings(req: Request):
    """Persist simulation settings. Called automatically on every field change."""
    d = await req.json()
    battery_cap = float(d.get("battery_cap_kwh", 45.0))
    panel_kwp   = float(d.get("panel_kwp", 0.8))
    starting_soc = float(d.get("starting_soc", 87.0))   # as percent 0-100
    load_factor  = float(d.get("load_factor", 1.0))

    # Clamp values to safe ranges
    battery_cap  = max(5.0, min(500.0, battery_cap))
    panel_kwp    = max(0.1, min(50.0, panel_kwp))
    starting_soc = max(10.0, min(100.0, starting_soc))
    load_factor  = max(0.1, min(3.0, load_factor))

    async with get_sim_db() as db:
        await db.execute(
            """INSERT OR REPLACE INTO sim_settings (id, battery_cap_kwh, panel_kwp, starting_soc, load_factor, updated_at)
               VALUES (1, ?, ?, ?, ?, datetime('now'))""",
            (battery_cap, panel_kwp, starting_soc, load_factor))
        await db.commit()

    # Update live simulation state if running
    if _sim_state["running"]:
        _sim_state["soc_pct"] = starting_soc   # reflect immediately in status

    return {
        "battery_cap_kwh": battery_cap,
        "panel_kwp":       panel_kwp,
        "starting_soc":    starting_soc,
        "load_factor":     load_factor,
        # Derived values for immediate dashboard feedback
        "battery_kwh":     round(battery_cap * (starting_soc / 100.0) * 0.95, 2),
        "reserve_10_kwh":  round(battery_cap * 0.10, 2),
        "reserve_20_kwh":  round(battery_cap * 0.20, 2),
        "usable_kwh":      round(battery_cap * 0.95, 2),
    }



async def get_mqtt_settings():
    async with get_sim_db() as db:
        row = await (await db.execute("SELECT * FROM mqtt_settings WHERE id=1")).fetchone()
    return dict(row) if row else {}


@api.put("/mqtt/settings")
async def save_mqtt_settings(req: Request):
    d = await req.json()
    async with get_sim_db() as db:
        await db.execute(
            """UPDATE mqtt_settings SET broker_host=?,broker_port=?,username=?,
               password=?,client_id=?,base_topic=?,publish_rate_s=?,send_appliances=?,
               send_battery=?,send_solar=?,send_summary=?,send_weather=?,qos=?,retain=?
               WHERE id=1""",
            (d.get("broker_host", "localhost"), int(d.get("broker_port", 1883)),
             d.get("username", ""), d.get("password", ""),
             d.get("client_id", "rv_simulator"), d.get("base_topic", "rv/energy"),
             float(d.get("publish_rate_s", 1.0)),
             int(d.get("send_appliances", 1)), int(d.get("send_battery", 1)),
             int(d.get("send_solar", 1)), int(d.get("send_summary", 1)),
             int(d.get("send_weather", 1)), int(d.get("qos", 0)), int(d.get("retain", 0))))
        await db.commit()
    return {"ok": True}


# ── Simulation endpoints ───────────────────────────────────────────────────────

async def _load_sim_inputs(plan_name: str = "Default Plan") -> tuple:
    """Load appliances, schedules, and weather plan from DB. Returns (apps, scheds_by_id, weather)."""
    async with get_sim_db() as db:
        app_rows = await (await db.execute(
            "SELECT * FROM sim_appliances WHERE on_state=1"
        )).fetchall()
        apps = [row_to_dict(r) for r in app_rows]

        sched_rows = await (await db.execute("SELECT * FROM sim_schedules")).fetchall()
        schedules_by_id: dict[int, list] = {}
        for s in sched_rows:
            sd = dict(s)
            schedules_by_id.setdefault(sd["appliance_id"], []).append(sd)

        plan_rows = await (await db.execute(
            "SELECT * FROM sim_weather_plans WHERE plan_name=? ORDER BY day_index", (plan_name,)
        )).fetchall()
        weather_plan = [dict(r) for r in plan_rows]

        mqtt_row = await (await db.execute("SELECT * FROM mqtt_settings WHERE id=1")).fetchone()
        mqtt_cfg = dict(mqtt_row) if mqtt_row else {}

    if not weather_plan:
        weather_plan = [{"day_index": 0, "condition": "sunny", "temp_c": 22,
                         "cloud_pct": 5, "sunrise_hhmm": "06:30", "sunset_hhmm": "19:30"}]

    # Sync app metadata for MQTT enrichment
    _sync_app_meta({str(a["id"]): a for a in apps})

    return apps, schedules_by_id, weather_plan, mqtt_cfg


@api.post("/simulate")
async def simulate(req: Request):
    """Batch multi-day simulation (no streaming). Returns aggregated results."""
    body = await req.json()
    # Load persisted settings as defaults — body values take precedence
    async with get_sim_db() as db:
        cfg_row = await (await db.execute("SELECT * FROM sim_settings WHERE id=1")).fetchone()
    cfg = dict(cfg_row) if cfg_row else {}

    battery_cap  = float(body.get("battery_capacity_kwh", cfg.get("battery_cap_kwh", 45.0)))
    panel_kwp    = float(body.get("panel_kwp",            cfg.get("panel_kwp", 0.8)))
    start_soc    = float(body.get("start_soc",            cfg.get("starting_soc", 87.0) / 100.0))
    plan_name    = body.get("plan_name", "Default Plan")

    apps, schedules_by_id, weather_plan, _ = await _load_sim_inputs(plan_name)

    results = await asyncio.to_thread(
        run_multi_day, apps, schedules_by_id, weather_plan, battery_cap, panel_kwp, start_soc
    )
    for r in results:
        r.pop("timeseries", None)   # strip — too large for batch response

    return {
        "days":       results,
        "appliances": [{"id": a["id"], "name": a["name"]} for a in apps],
    }


@api.post("/simulate/live/start")
async def start_live_sim(req: Request):
    """Start live simulation loop with real-time MQTT publishing."""
    global _sim_state
    if _sim_state["running"]:
        return {"status": "already_running"}

    body = await req.json()
    # Load persisted settings as defaults
    async with get_sim_db() as db:
        cfg_row = await (await db.execute("SELECT * FROM sim_settings WHERE id=1")).fetchone()
    cfg = dict(cfg_row) if cfg_row else {}

    battery_cap  = float(body.get("battery_capacity_kwh", cfg.get("battery_cap_kwh", 45.0)))
    panel_kwp    = float(body.get("panel_kwp",            cfg.get("panel_kwp", 0.8)))
    start_soc    = float(body.get("start_soc",            cfg.get("starting_soc", 87.0) / 100.0))
    plan_name    = body.get("plan_name", "Default Plan")

    apps, schedules_by_id, weather_plan, mqtt_cfg = await _load_sim_inputs(plan_name)

    publisher = SimPublisher(mqtt_cfg)
    publisher.connect()

    # Immediately publish registry so dashboard gets names before first data step
    all_apps = await _get_all_apps_for_registry()
    publisher.publish_registry(all_apps)

    rate_s = float(mqtt_cfg.get("publish_rate_s", 1.0))
    _sim_state.update({
        "running": True, "day": 0, "step": 0,
        "soc_pct": start_soc * 100, "publisher": publisher, "rate_s": rate_s,
    })

    task = asyncio.ensure_future(
        _live_loop(apps, schedules_by_id, weather_plan, battery_cap, panel_kwp, start_soc)
    )
    _sim_state["task"] = task
    return {"status": "started"}


async def _get_all_apps_for_registry() -> list[dict]:
    """Return all appliances (on and off) for registry MQTT publish."""
    async with get_sim_db() as db:
        rows = await (await db.execute("SELECT * FROM sim_appliances")).fetchall()
    return [row_to_dict(r) for r in rows]


@api.post("/simulate/live/stop")
async def stop_live_sim():
    global _sim_state
    _sim_state["running"] = False
    if _sim_state["task"] and not _sim_state["task"].done():
        _sim_state["task"].cancel()
    if _sim_state["publisher"]:
        _sim_state["publisher"].disconnect()
        _sim_state["publisher"] = None
    return {"status": "stopped"}


@api.get("/simulate/live/status")
async def live_status():
    return {
        "running":    _sim_state["running"],
        "day":        _sim_state["day"],
        "step":       _sim_state["step"],
        "soc_pct":    _sim_state["soc_pct"],
        "solar_kw":   _sim_state.get("solar_kw", 0),
        "load_kw":    _sim_state.get("load_kw", 0),
        "si_score":   _sim_state.get("si_score", 0),
        "si_grade":   _sim_state.get("si_grade", "F"),
        "current_step": _sim_state.get("current_step"),
    }


@api.get("/status")
async def dashboard_status():
    return _snapshot_dashboard_status()


@api.get("/appliances/live")
async def list_appliances_with_latest():
    result = {}
    app_hist = _dashboard_state["appliance_history"]
    for aid, meta in _sim_state["app_meta"].items():
        hist = list(app_hist.get(aid, []))
        result[aid] = {
            **meta,
            "latest": hist[-1] if hist else None,
            "history_count": len(hist),
        }
    return result


@api.get("/history/battery")
async def history_battery(n: int = 300):
    return list(_dashboard_state["battery_history"])[-n:]


@api.get("/history/solar")
async def history_solar(n: int = 300):
    return list(_dashboard_state["solar_history"])[-n:]


@api.get("/history/appliance/{aid}")
async def history_appliance(aid: str, n: int = 200):
    return list(_dashboard_state["appliance_history"].get(aid, []))[-n:]


@api.get("/stability")
async def stability_now():
    latest = _dashboard_state["latest"]
    si = _compute_live_si(
        float(latest["soc_pct"]),
        float(latest["solar_kw"]),
        float(latest["load_kw"]),
        float(latest["net_kw"]),
    )
    return {
        "si_score": si["score"],
        "si_grade": si["grade"],
        "p1": si["p1"],
        "p2": si["p2"],
        "p3": si["p3"],
        "p4": si["p4"],
    }


@api.post("/connect")
async def reconnect(_: Request):
    """Backward-compatible no-op after single-app merge."""
    return {"ok": True}


async def _live_loop(apps, schedules_by_id, weather_plan, battery_cap, panel_kwp, start_soc):
    """Background task: runs day-by-day, emitting steps via MQTT + SSE."""
    soc = start_soc
    app_meta = _sim_state["app_meta"]

    for day_cfg in weather_plan:
        if not _sim_state["running"]:
            break
        _sim_state["day"] = day_cfg.get("day_index", 0)

        result = await asyncio.to_thread(
            run_day_simulation, apps, schedules_by_id, day_cfg,
            battery_cap, soc, panel_kwp, seed=day_cfg.get("day_index", 0) * 42
        )
        soc = result["end_soc_pct"] / 100.0
        _sim_state.update({
            "si_score": result["si_score"],
            "si_grade": result["si_grade"],
        })

        for step_data in result.get("timeseries", []):
            if not _sim_state["running"]:
                break

            _sim_state.update({
                "step":       step_data.get("step", 0),
                "soc_pct":    step_data.get("soc_pct", 0),
                "solar_kw":   step_data.get("solar_kw", 0),
                "load_kw":    step_data.get("load_kw", 0),
                "current_step": step_data,
            })
            _record_live_step(step_data)

            if _sim_state["publisher"]:
                _sim_state["publisher"].publish_step(step_data, app_meta)

            step_payload = {
                "type":     "step",
                "soc_pct":  step_data["soc_pct"],
                "solar_kw": step_data["solar_kw"],
                "load_kw":  step_data["load_kw"],
                "net_kw":   step_data["net_kw"],
                "step":     step_data["step"],
                "day":      _sim_state["day"],
                "si_score": _sim_state["si_score"],
                "si_grade": _sim_state["si_grade"],
                "ts":       step_data["ts"],
                "apps":     step_data["appliances"],
            }
            battery_payload = {
                "type": "battery",
                "soc_pct": step_data["soc_pct"],
                "kwh": step_data["battery_kwh"],
                "reserve_hit": step_data.get("reserve_hit", False),
                "net_kw": step_data["net_kw"],
                "si_score": _sim_state["si_score"],
                "si_grade": _sim_state["si_grade"],
                "ts": step_data["ts"],
            }
            solar_payload = {
                "type": "solar",
                "solar_kw": step_data["solar_kw"],
                "load_kw": step_data["load_kw"],
                "ts": step_data["ts"],
            }

            for q in _sse_queues:
                try:
                    q.put_nowait(json.dumps(step_payload, default=str))
                    q.put_nowait(json.dumps(battery_payload, default=str))
                    q.put_nowait(json.dumps(solar_payload, default=str))
                    for aid, av in step_data["appliances"].items():
                        m = app_meta.get(str(aid), {})
                        q.put_nowait(json.dumps({
                            "type": "appliance",
                            "id": str(aid),
                            "name": m.get("name", f"App {aid}"),
                            "icon": m.get("icon", "🔌"),
                            "clr": m.get("clr", "#0A84FF"),
                            "cat": m.get("cat", "medium"),
                            "v": av.get("v", 0),
                            "a": av.get("a", 0),
                            "w": av.get("w", 0),
                            "ts": step_data["ts"],
                        }, default=str))
                except asyncio.QueueFull:
                    pass

            await asyncio.sleep(_sim_state["rate_s"])

    _sim_state["running"] = False
    logger.info("Live simulation complete")


@api.get("/simulate/live/stream")
async def sse_stream():
    """Server-Sent Events — real-time simulator data."""
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    _sse_queues.append(q)

    async def event_generator():
        try:
            yield "data: {\"type\":\"connected\"}\n\n"
            yield f"data: {json.dumps({'type': 'registry', 'appliances': [{'id': aid, **meta} for aid, meta in _sim_state['app_meta'].items()]})}\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=20)
                    yield f"data: {msg}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            try:
                _sse_queues.remove(q)
            except ValueError:
                pass

    return StreamingResponse(event_generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@api.get("/stream")
async def dashboard_stream():
    """Unified dashboard stream endpoint."""
    return await sse_stream()


# ── Pages ──────────────────────────────────────────────────────────────────────

@app.get("/")
async def dashboard_home(request: Request):
    async with get_sim_db() as db:
        mqtt_row = await (await db.execute("SELECT * FROM mqtt_settings WHERE id=1")).fetchone()
    mqtt_cfg = dict(mqtt_row) if mqtt_row else {}
    return templates.TemplateResponse(request, "dashboard.html", {
        "status": _snapshot_dashboard_status(),
        "simulator_url": "/sim",
        "mqtt_host": mqtt_cfg.get("broker_host", "localhost"),
        "mqtt_port": mqtt_cfg.get("broker_port", 1883),
        "mqtt_topic": mqtt_cfg.get("base_topic", "rv/energy"),
    })


@app.get("/sim")
async def simulator_home(request: Request):
    async with get_sim_db() as db:
        mqtt_row  = await (await db.execute("SELECT * FROM mqtt_settings WHERE id=1")).fetchone()
        plan_rows = await (await db.execute(
            "SELECT * FROM sim_weather_plans WHERE plan_name='Default Plan' ORDER BY day_index"
        )).fetchall()
        prof_rows = await (await db.execute(
            "SELECT * FROM sim_profiles ORDER BY is_preset DESC, name"
        )).fetchall()
        cfg_row   = await (await db.execute("SELECT * FROM sim_settings WHERE id=1")).fetchone()
    mqtt_cfg  = dict(mqtt_row)  if mqtt_row  else {}
    weather   = [dict(r)       for r in plan_rows]
    profiles  = [dict(r)       for r in prof_rows]
    sim_cfg   = dict(cfg_row)  if cfg_row   else {"battery_cap_kwh":45.0,"panel_kwp":0.8,"starting_soc":87.0,"load_factor":1.0}
    return templates.TemplateResponse(request, "simulator.html", {
        "mqtt_cfg": mqtt_cfg,
        "weather":  weather,
        "profiles": profiles,
        "sim_cfg":  sim_cfg,
        "db_path":  SIM_DB,
    })


app.include_router(api)


if __name__ == "__main__":
    print("\n" + "═" * 58)
    print("  RV Energy Simulator  — Port 5001")
    print("  Appliance Configurator + Profiles + MQTT Publisher")
    print("═" * 58)
    print("  Dashboard→ http://localhost:5001")
    print("  Simulator→ http://localhost:5001/sim")
    print("  MQTT     → configurable external broker")
    print("═" * 58 + "\n")
    uvicorn.run("simulator:app", host="0.0.0.0", port=5001, reload=True, log_level="info")
