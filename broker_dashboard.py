"""
broker_dashboard.py  —  RV Energy Dashboard  (port 5002)
─────────────────────────────────────────────────────────────────────────────
Subscribes to MQTT from the simulator. Renders real-time charts:
  • Per-appliance V, A, W line charts
  • Battery SOC with reserve markers
  • Stability Score (live computation)
  • 24h rolling history

FIX: On startup the dashboard fetches appliance names from the simulator
     API (localhost:5001/api/appliances) to seed appliance_meta so chart
     labels and cards appear before the first MQTT appliance message.
     MQTT registry messages (rv/energy/registry) also update the metadata.
"""
from __future__ import annotations
import asyncio, json, logging, time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from markupsafe import Markup

import uvicorn
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates

from app.mqtt_service import add_dashboard_queue, remove_dashboard_queue, start_subscriber, stop_subscriber

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s %(message)s")
logger = logging.getLogger("rv.dashboard")

# ── In-memory time-series buffers ─────────────────────────────────────────────
MAX_HISTORY = 1440   # 24 h @ 1 pt/min

appliance_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=MAX_HISTORY))
appliance_meta:    dict[str, dict]  = {}   # {aid_str: {name, icon, clr, cat, ...}}
battery_history:   deque = deque(maxlen=MAX_HISTORY)
solar_history:     deque = deque(maxlen=MAX_HISTORY)
summary_history:   deque = deque(maxlen=MAX_HISTORY)

latest: dict = {
    "soc_pct": 0, "battery_kwh": 0, "solar_kw": 0, "load_kw": 0,
    "net_kw": 0, "si_score": 0.0, "si_grade": "—", "reserve_hit": False,
    "last_update": "—", "connected": False,
}

SIMULATOR_URL = "http://localhost:5001"

# ── Stability Score ───────────────────────────────────────────────────────────
def _compute_si(soc_pct: float, solar_kw: float, load_kw: float, net_kw: float) -> dict:
    """Compute Stability Score from live telemetry (same 4-pillar formula as main app)."""
    usable_kwh  = soc_pct / 100.0 * 37.5   # assume 45 kWh × 0.95 × (soc/100)
    daily_draw  = max(abs(net_kw) if net_kw < 0 else 0.01, 0.01) * 24   # rough daily kWh
    bat_days    = usable_kwh / daily_draw
    cov         = min(1.0, solar_kw / max(load_kw, 0.01))

    p1 = min(3.5, (bat_days / 14.0) * 3.5)
    p2 = min(3.0, cov * 3.0)
    p3 = min(2.0, max(0.0, (1.0 - load_kw / 5.0)) * 2.0)
    p4 = min(1.5, max(0.0, (soc_pct - 20.0) / 80.0) * 1.5)

    score = round(p1 + p2 + p3 + p4, 2)
    grade = ("S" if score >= 9 else "A" if score >= 8 else "B" if score >= 7 else
             "C" if score >= 6 else "D" if score >= 5 else "F")

    return {"score": score, "grade": grade,
            "p1": round(p1, 2), "p2": round(p2, 2),
            "p3": round(p3, 2), "p4": round(p4, 2)}


# ── Fetch appliance names from simulator on startup ──────────────────────────
async def _seed_appliance_meta():
    """
    Call GET /api/appliances on the simulator to pre-populate appliance_meta.
    This ensures the dashboard shows appliance names/icons immediately when
    the first MQTT data arrives, without waiting for a registry message.
    Retries silently if simulator is not yet running.
    """
    for attempt in range(6):  # try for up to 30 s
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get(f"{SIMULATOR_URL}/api/appliances")
            if r.status_code == 200:
                apps = r.json()
                for a in apps:
                    aid = str(a["id"])
                    appliance_meta[aid] = {
                        "name": a.get("name", f"App {aid}"),
                        "icon": a.get("icon", "🔌"),
                        "clr":  a.get("clr", "#0A84FF"),
                        "cat":  a.get("cat", "medium"),
                        "always_on": a.get("always_on", False),
                        "cycle_pattern": a.get("cycle_pattern", "constant"),
                    }
                logger.info("Seeded %d appliances from simulator API", len(apps))
                return
        except Exception:
            pass
        await asyncio.sleep(5)
    logger.warning("Could not fetch appliance list from simulator — waiting for MQTT registry")


# ── MQTT Message Handler ──────────────────────────────────────────────────────
async def handle_mqtt_message(msg: dict):
    """Decode incoming MQTT message and update buffers + broadcast SSE."""
    topic = msg.get("_topic", "")
    ts    = msg.get("ts", msg.get("_received", datetime.now(timezone.utc).isoformat()))
    latest["last_update"] = ts
    latest["connected"]   = True

    # ── Registry (published by simulator at sim start or when appliances change) ──
    if topic.endswith("/registry"):
        for a in msg.get("appliances", []):
            aid = str(a.get("id", "?"))
            appliance_meta[aid] = {
                "name": a.get("name", f"App {aid}"),
                "icon": a.get("icon", "🔌"),
                "clr":  a.get("clr", "#0A84FF"),
                "cat":  a.get("cat", "medium"),
                "always_on": a.get("always_on", False),
                "cycle_pattern": a.get("cycle_pattern", "constant"),
            }
        logger.debug("Registry updated: %d appliances", len(appliance_meta))
        await _broadcast_sse({"type": "registry",
                              "appliances": [{"id": k, **v} for k, v in appliance_meta.items()]})
        return

    # ── Per-appliance telemetry ──────────────────────────────────────────────
    if "/appliances/" in topic:
        parts = topic.split("/")
        aid   = parts[-1]
        v     = float(msg.get("voltage_v", 0))
        a_val = float(msg.get("current_a", 0))
        w     = float(msg.get("power_w", 0))

        # Enrich metadata from message if available
        if msg.get("name") and aid not in appliance_meta:
            appliance_meta[aid] = {
                "name": msg["name"],
                "icon": msg.get("icon", "🔌"),
                "clr":  msg.get("clr", "#0A84FF"),
                "cat":  msg.get("cat", "medium"),
            }
        elif msg.get("name"):
            # Update existing meta
            appliance_meta[aid]["name"] = msg["name"]
            if msg.get("icon"): appliance_meta[aid]["icon"] = msg["icon"]
            if msg.get("clr"):  appliance_meta[aid]["clr"]  = msg["clr"]

        point = {"ts": ts, "v": v, "a": a_val, "w": w}
        appliance_history[aid].append(point)
        meta = appliance_meta.get(aid, {"name": f"App {aid}", "icon": "🔌", "clr": "#0A84FF", "cat": "medium"})

        await _broadcast_sse({
            "type": "appliance",
            "id":   aid,
            "name": meta["name"],
            "icon": meta["icon"],
            "clr":  meta["clr"],
            "v": v, "a": a_val, "w": w,
            "ts": ts,
        })
        return

    # ── Battery ──────────────────────────────────────────────────────────────
    if "/battery" in topic:
        soc = float(msg.get("soc_pct", 0))
        kwh = float(msg.get("kwh", 0))
        net = float(msg.get("net_kw", 0))
        latest.update({
            "soc_pct":    soc,
            "battery_kwh": kwh,
            "reserve_hit": bool(msg.get("reserve_hit", False)),
            "net_kw":     net,
        })
        battery_history.append({"ts": ts, "soc": soc, "kwh": kwh, "net_kw": net})
        si = _compute_si(soc, latest["solar_kw"], latest["load_kw"], net)
        latest.update({"si_score": si["score"], "si_grade": si["grade"]})
        await _broadcast_sse({"type": "battery", "soc_pct": soc, "kwh": kwh,
                              "reserve_hit": latest["reserve_hit"], "net_kw": net,
                              "si_score": si["score"], "si_grade": si["grade"], "ts": ts})
        return

    # ── Solar / load ─────────────────────────────────────────────────────────
    if "/solar" in topic:
        s_kw = float(msg.get("solar_kw", 0))
        l_kw = float(msg.get("load_kw", 0))
        latest.update({"solar_kw": s_kw, "load_kw": l_kw})
        solar_history.append({"ts": ts, "solar_kw": s_kw, "load_kw": l_kw})
        await _broadcast_sse({"type": "solar", "solar_kw": s_kw, "load_kw": l_kw, "ts": ts})
        return

    # ── Summary ───────────────────────────────────────────────────────────────
    if "/summary" in topic:
        summary_history.append({"ts": ts, **msg})


# ── SSE broadcast ─────────────────────────────────────────────────────────────
_sse_queues: list[asyncio.Queue] = []

async def _broadcast_sse(data: dict):
    dead = []
    for q in _sse_queues:
        try:
            q.put_nowait(json.dumps(data, default=str))
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        try:
            _sse_queues.remove(q)
        except ValueError:
            pass


# ── Override mqtt_service._broadcast to route MQTT messages to our handler ─────
# The subscriber's on_message runs in a paho thread and calls:
#   asyncio.run_coroutine_threadsafe(_broadcast(payload), loop)
# We patch _broadcast so all MQTT messages flow through handle_mqtt_message.
import app.mqtt_service as _mqtt_svc
_mqtt_svc._broadcast = handle_mqtt_message  # patch module-level coroutine

# Also add dashboard SSE queues to mqtt_service's queue list so registry
# broadcasts from the simulator reach the dashboard directly.
def _fwd_to_dashboard(q: asyncio.Queue):
    _mqtt_svc.add_dashboard_queue(q)
def _rem_from_dashboard(q: asyncio.Queue):
    _mqtt_svc.remove_dashboard_queue(q)


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = {"broker_host": "localhost", "broker_port": 1883,
           "username": "", "password": "", "base_topic": "rv/energy"}
    loop = asyncio.get_event_loop()
    start_subscriber(cfg, loop)
    # Seed appliance metadata from simulator API
    asyncio.ensure_future(_seed_appliance_meta())
    logger.info("Dashboard ready — http://localhost:5002")
    yield
    stop_subscriber()


dash = FastAPI(title="RV Energy Dashboard", version="1.0.0", lifespan=lifespan)
dash.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["tojson"] = lambda v: Markup(json.dumps(v, ensure_ascii=False, default=str))


# ── REST API ──────────────────────────────────────────────────────────────────

@dash.get("/api/status")
async def status():
    return latest


@dash.get("/api/appliances")
async def list_appliances_with_latest():
    result = {}
    for aid, meta in appliance_meta.items():
        hist = list(appliance_history.get(aid, []))
        result[aid] = {
            **meta,
            "latest":  hist[-1] if hist else None,
            "history_count": len(hist),
        }
    # If no appliances from MQTT yet, try to fetch from simulator
    if not result:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                r = await client.get(f"{SIMULATOR_URL}/api/appliances")
            if r.status_code == 200:
                for a in r.json():
                    aid = str(a["id"])
                    appliance_meta[aid] = {
                        "name": a.get("name", f"App {aid}"),
                        "icon": a.get("icon", "🔌"),
                        "clr":  a.get("clr", "#0A84FF"),
                        "cat":  a.get("cat", "medium"),
                    }
                    result[aid] = {**appliance_meta[aid], "latest": None, "history_count": 0}
        except Exception:
            pass
    return result


@dash.get("/api/history/battery")
async def history_battery(n: int = 300):
    return list(battery_history)[-n:]


@dash.get("/api/history/solar")
async def history_solar(n: int = 300):
    return list(solar_history)[-n:]


@dash.get("/api/history/appliance/{aid}")
async def history_appliance(aid: str, n: int = 200):
    return list(appliance_history.get(aid, []))[-n:]


@dash.get("/api/stability")
async def stability_now():
    si = _compute_si(latest["soc_pct"], latest["solar_kw"], latest["load_kw"], latest["net_kw"])
    si["si_grade"] = si.pop("grade")
    si["si_score"] = si.pop("score")
    return si


@dash.post("/api/connect")
async def reconnect(request: Request):
    body = await request.json()
    loop = asyncio.get_event_loop()
    stop_subscriber()
    start_subscriber(body, loop)
    # Re-seed appliance meta from simulator
    asyncio.ensure_future(_seed_appliance_meta())
    return {"ok": True}


@dash.get("/api/stream")
async def sse_stream():
    """Server-Sent Events — pushes appliance/battery/solar updates to browser."""
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    _sse_queues.append(q)

    async def gen():
        try:
            # Push current registry + status immediately on client connect
            registry_msg = json.dumps({
                "type": "registry",
                "appliances": [{"id": k, **v} for k, v in appliance_meta.items()],
            })
            yield f"data: {registry_msg}\n\n"

            # Push latest battery/solar if we have data
            if latest["connected"] or any(len(b) > 0 for b in [battery_history, solar_history]):
                yield f"data: {json.dumps({'type':'battery', **{k:latest[k] for k in ['soc_pct','battery_kwh','reserve_hit','net_kw','si_score','si_grade']},'ts':latest['last_update']})}\n\n"

            yield f"data: {json.dumps({'type':'connected'})}\n\n"

            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=25)
                    yield f"data: {msg}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            try:
                _sse_queues.remove(q)
            except ValueError:
                pass

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@dash.get("/api/sim-stream-proxy")
async def sim_stream_proxy():
    """
    Proxy the simulator's live-stream SSE to the dashboard browser.
    This provides a direct data path (no MQTT hop) so SOC changes in the
    simulator appear in the dashboard in real time even if MQTT is slow.
    """
    import httpx

    async def gen():
        yield f"data: {json.dumps({'type':'proxy_connected'})}\n\n"
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("GET", "http://localhost:5001/api/simulate/live/stream") as resp:
                    async for line in resp.aiter_lines():
                        if line.startswith("data: "):
                            payload = line[6:]
                            try:
                                d = json.loads(payload)
                                # Convert simulator step format to dashboard battery/solar format
                                if d.get("type") == "step":
                                    soc   = d.get("soc_pct", 0)
                                    s_kw  = d.get("solar_kw", 0)
                                    l_kw  = d.get("load_kw", 0)
                                    net   = d.get("net_kw", 0)
                                    ts    = d.get("ts", "")
                                    # Update in-memory state
                                    latest.update({"soc_pct": soc, "solar_kw": s_kw,
                                                   "load_kw": l_kw, "net_kw": net,
                                                   "connected": True, "last_update": ts})
                                    battery_history.append({"ts": ts, "soc": soc, "kwh": soc/100*37.5, "net_kw": net})
                                    solar_history.append({"ts": ts, "solar_kw": s_kw, "load_kw": l_kw})
                                    # Forward as battery + solar events
                                    yield f"data: {json.dumps({'type':'battery','soc_pct':soc,'kwh':round(soc/100*37.5,2),'reserve_hit':soc<10,'net_kw':net,'ts':ts})}\n\n"
                                    yield f"data: {json.dumps({'type':'solar','solar_kw':s_kw,'load_kw':l_kw,'ts':ts})}\n\n"
                                    # Forward appliance data
                                    for aid, av in d.get("apps", {}).items():
                                        am = appliance_meta.get(str(aid), {})
                                        yield f"data: {json.dumps({'type':'appliance','id':aid,'name':am.get('name','App '+str(aid)),'icon':am.get('icon',''),'clr':am.get('clr','#0A84FF'),'cat':am.get('cat',''),'voltage_v':av.get('v',0),'current_a':av.get('a',0),'power_w':av.get('w',0),'ts':ts})}\n\n"
                                elif d.get("type") == "connected":
                                    yield f"data: {json.dumps({'type':'sim_connected'})}\n\n"
                            except Exception:
                                pass
                        elif line == ": ping":
                            yield ": ping\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type':'proxy_error','msg':str(e)})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Home page ─────────────────────────────────────────────────────────────────

@dash.get("/")
async def home(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", {"status": latest})


if __name__ == "__main__":
    print("\n" + "═" * 58)
    print("  RV Energy Dashboard  — Port 5002")
    print("  MQTT Subscriber + Real-Time Charts + Stability Score")
    print("═" * 58)
    print("  Open  → http://localhost:5002")
    print("  Data  ← MQTT localhost:1883  rv/energy/#")
    print("  Names ← GET  localhost:5001/api/appliances")
    print("═" * 58 + "\n")
    uvicorn.run("broker_dashboard:dash", host="0.0.0.0", port=5002, reload=False, log_level="info")
