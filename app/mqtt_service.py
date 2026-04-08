"""
app/mqtt_service.py
─────────────────────────────────────────────────────────────────────────────
MQTT service — broker, publisher, and subscriber in one module.

  • Embedded MQTT broker via amqtt (port 1883, no external mosquitto needed)
  • Publisher: streams live simulation data on configurable topics
  • Subscriber: receives data and forwards to Dashboard via asyncio.Queue

MQTT Topic Structure:
  {base_topic}/appliances/{id}     → per-appliance V, A, W, status
  {base_topic}/battery             → SOC %, kWh, voltage, reserve status
  {base_topic}/solar               → kW generation, irradiance factor
  {base_topic}/weather             → condition, temp, cloud%, sunrise/sunset
  {base_topic}/summary             → stability score, total load, total solar
  {base_topic}/control             → inbound commands (start/stop/reset)
"""
from __future__ import annotations
import asyncio, json, logging, time
from datetime import datetime
from typing import Any, Callable

import paho.mqtt.client as paho

logger = logging.getLogger("rv.mqtt")

# ─── Global subscriber queue for dashboard ───────────────────────────────────
_dashboard_queues: list[asyncio.Queue] = []
_broker_task: asyncio.Task | None = None
_sim_task: asyncio.Task | None = None
_is_publishing = False

def add_dashboard_queue(q: asyncio.Queue):
    _dashboard_queues.append(q)

def remove_dashboard_queue(q: asyncio.Queue):
    try: _dashboard_queues.remove(q)
    except ValueError: pass

async def _broadcast(msg: dict):
    """Push a received MQTT message to all connected dashboard SSE clients."""
    for q in _dashboard_queues:
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass


# ─── MQTT Broker (amqtt) ──────────────────────────────────────────────────────
async def start_broker(host: str = "localhost", port: int = 1883):
    """Launch embedded MQTT broker. Safe to call multiple times."""
    global _broker_task
    if _broker_task and not _broker_task.done():
        return  # already running
    try:
        from amqtt.broker import Broker
        cfg = {
            "listeners": {"default": {"type": "tcp", "bind": f"0.0.0.0:{port}"}},
            "sys_interval": 10,
            "auth": {"allow-anonymous": True},
            "topic-check": {"enabled": False},
        }
        broker = Broker(cfg)
        await broker.start()
        logger.info("MQTT broker started on %s:%d", host, port)
        _broker_task = asyncio.ensure_future(_keep_broker_alive(broker))
    except Exception as e:
        logger.warning("Could not start embedded MQTT broker: %s. Use external mosquitto.", e)

async def _keep_broker_alive(broker):
    try:
        while True:
            await asyncio.sleep(60)
    except asyncio.CancelledError:
        await broker.shutdown()


# ─── MQTT Publisher (paho synchronous, run in thread) ────────────────────────
class SimPublisher:
    """Wraps paho client. Call .connect(), then publish()."""

    def __init__(self, settings: dict):
        self.host     = settings.get("broker_host", "localhost")
        self.port     = settings.get("broker_port", 1883)
        self.user     = settings.get("username", "")
        self.pw       = settings.get("password", "")
        self.client_id = settings.get("client_id", "rv_simulator")
        self.base     = settings.get("base_topic", "rv/energy").rstrip("/")
        self.qos      = int(settings.get("qos", 0))
        self.retain   = bool(settings.get("retain", False))
        self.send_cfg = {
            "appliances": bool(settings.get("send_appliances", True)),
            "battery":    bool(settings.get("send_battery", True)),
            "solar":      bool(settings.get("send_solar", True)),
            "summary":    bool(settings.get("send_summary", True)),
            "weather":    bool(settings.get("send_weather", True)),
        }
        self._client = paho.Client(client_id=self.client_id)
        if self.user:
            self._client.username_pw_set(self.user, self.pw)
        self._connected = False

    def connect(self) -> bool:
        try:
            self._client.connect(self.host, self.port, keepalive=30)
            self._client.loop_start()
            self._connected = True
            logger.info("MQTT publisher connected to %s:%d", self.host, self.port)
            return True
        except Exception as e:
            logger.warning("MQTT publisher connect failed: %s", e)
            return False

    def disconnect(self):
        if self._connected:
            self._client.loop_stop()
            self._client.disconnect()
            self._connected = False

    def _pub(self, topic: str, payload: dict):
        if not self._connected:
            return
        try:
            self._client.publish(
                f"{self.base}/{topic}",
                json.dumps(payload, default=str),
                qos=self.qos,
                retain=self.retain,
            )
        except Exception as e:
            logger.debug("Publish error: %s", e)

    def publish_registry(self, appliances: list[dict]):
        """Publish appliance registry (name/icon/clr/cat) once so dashboard can seed metadata.
        Called once at simulation start and whenever appliances change.
        Topic: {base}/registry  → {appliances: [{id, name, icon, clr, cat, always_on, cycle_pattern}]}
        """
        registry = [
            {
                "id":            str(a.get("id", a.get("appliance_id", 0))),
                "name":          a.get("name", "Appliance"),
                "icon":          a.get("icon", "🔌"),
                "clr":           a.get("clr", "#0A84FF"),
                "cat":           a.get("cat", "medium"),
                "always_on":     bool(a.get("always_on", False)),
                "cycle_pattern": a.get("cycle_pattern", "constant"),
            }
            for a in appliances
        ]
        self._pub("registry", {"ts": datetime.utcnow().isoformat(), "appliances": registry})

    def publish_step(self, step_data: dict, appliance_meta: dict | None = None):
        """Publish one simulation step's data to the configured topics.
        
        appliance_meta: optional dict {aid_str: {name, icon, clr, cat}} to enrich
                        per-appliance MQTT messages so dashboard can display names.
        """
        ts  = step_data.get("ts", datetime.utcnow().isoformat())
        soc = step_data.get("soc_pct", 0)
        kwh = step_data.get("battery_kwh", 0)
        meta = appliance_meta or {}

        # Battery
        if self.send_cfg["battery"]:
            self._pub("battery", {
                "ts": ts, "soc_pct": soc, "kwh": kwh,
                "reserve_hit": step_data.get("reserve_hit", False),
                "net_kw": step_data.get("net_kw", 0),
            })

        # Solar
        if self.send_cfg["solar"]:
            self._pub("solar", {
                "ts": ts,
                "solar_kw": step_data.get("solar_kw", 0),
                "load_kw":  step_data.get("load_kw", 0),
            })

        # Per-appliance — include name/icon/clr so dashboard can build metadata
        if self.send_cfg["appliances"]:
            for aid, data in step_data.get("appliances", {}).items():
                m = meta.get(str(aid), {})
                self._pub(f"appliances/{aid}", {
                    "ts":        ts,
                    "id":        aid,
                    "name":      m.get("name", f"App {aid}"),
                    "icon":      m.get("icon", "🔌"),
                    "clr":       m.get("clr", "#0A84FF"),
                    "cat":       m.get("cat", "medium"),
                    "voltage_v": data.get("v", 0),
                    "current_a": data.get("a", 0),
                    "power_w":   data.get("w", 0),
                })

        # Summary (every 10th step to reduce rate)
        if self.send_cfg["summary"] and int(step_data.get("step", 0)) % 10 == 0:
            self._pub("summary", {
                "ts": ts, "soc_pct": soc,
                "load_kw":  step_data.get("load_kw", 0),
                "solar_kw": step_data.get("solar_kw", 0),
            })


# ─── MQTT Subscriber (paho) — feeds dashboard queue ──────────────────────────
_sub_client: paho.Client | None = None

def start_subscriber(settings: dict, loop: asyncio.AbstractEventLoop):
    """Connect a paho subscriber that forwards messages to _broadcast."""
    global _sub_client
    if _sub_client:
        try:
            _sub_client.disconnect()
        except Exception:
            pass

    base = settings.get("base_topic", "rv/energy").rstrip("/")
    host = settings.get("broker_host", "localhost")
    port = settings.get("broker_port", 1883)

    client = paho.Client(client_id="rv_dashboard_sub")
    user = settings.get("username", "")
    if user:
        client.username_pw_set(user, settings.get("password", ""))

    def on_message(c, ud, msg):
        try:
            payload = json.loads(msg.payload.decode())
            payload["_topic"] = msg.topic
            payload["_received"] = datetime.utcnow().isoformat()
            asyncio.run_coroutine_threadsafe(_broadcast(payload), loop)
        except Exception:
            pass

    def on_connect(c, ud, flags, rc):
        c.subscribe(f"{base}/#", qos=0)
        logger.info("Dashboard subscriber connected, listening on %s/#", base)

    client.on_message  = on_message
    client.on_connect  = on_connect
    try:
        client.connect(host, port, keepalive=30)
        client.loop_start()
        _sub_client = client
        logger.info("Dashboard subscriber started")
    except Exception as e:
        logger.warning("Dashboard subscriber connect failed: %s", e)

def stop_subscriber():
    global _sub_client
    if _sub_client:
        try:
            _sub_client.loop_stop()
            _sub_client.disconnect()
        except Exception:
            pass
        _sub_client = None
