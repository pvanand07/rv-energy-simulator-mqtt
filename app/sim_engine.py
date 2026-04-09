"""
app/sim_engine.py
-----------------------------------------------------------------------------
Realistic RV appliance simulation engine.

Each 30-second step computes:
  - Per-appliance instantaneous V, A, W based on cycle pattern + schedule
  - Solar generation from sunrise/sunset + cloud cover + irradiance model
  - Battery SOC with LiFePO4 derating + 10-20% reserve floor protection
  - Stability Score (0-10) across 4 pillars

CYCLE PATTERNS (simulate realistic power modes)
-------------------------------------------------
  constant      - uniform draw when active
  compressor    - mini-fridge: ON 18/90 steps (20%), OFF at 0.10x (controls)
  thermostat    - water heater/AC: cycles to maintain setpoint, stable phase
  wifi_traffic  - router: 90% idle (0.5x), 10% burst (3x) random
  display_sleep - HMI: 22:00-06:00 at 0.08x, rest at 1.0x
  motion_sensor - camera: 80% at 0.15x, 20% at 1.0x (motion detected)
  network_load  - Starlink: base 0.4x, idle/burst based on time of day
  dimmer        - lights: fade in/out with brightness variation
  wash_cycle    - washer: variable load through wash/rinse/spin phases

SOLAR MODEL (sunrise-to-sunset sine arch)
-----------------------------------------
  irr(h) = sin(pi x (h - sunrise) / (sunset - sunrise))
  daily_kwh = panel_kw x weather_factor x numerical_integral(irr)
"""

from __future__ import annotations
import math, random, time as _time
from datetime import datetime, date, timedelta

# -----------------------------------------------------------------------------
# CONSTANTS
# -----------------------------------------------------------------------------
STEPS   = 2880          # 30-second steps per 24h
DT_H    = 30 / 3600     # hours per step
WEATHER_FACTOR = {"sunny": 1.00, "partly": 0.60, "overcast": 0.25, "rainy": 0.05}
RESERVE_LOW  = 0.10     # 10% - hard floor (battery protection)
RESERVE_HIGH = 0.20     # 20% - soft reserve warning threshold

# -----------------------------------------------------------------------------
# HELPERS
# -----------------------------------------------------------------------------
def hhmm_to_h(hhmm: str) -> float:
    """'06:30' -> 6.5"""
    h, m = map(int, hhmm.split(":"))
    return h + m / 60.0

def solar_irr(h: float, sunrise_h: float, sunset_h: float) -> float:
    if h < sunrise_h or h > sunset_h or sunset_h <= sunrise_h:
        return 0.0
    span = sunset_h - sunrise_h
    return math.sin(math.pi * (h - sunrise_h) / span)


# -----------------------------------------------------------------------------
# PER-STEP CYCLE PATTERN FUNCTIONS
# Returns a multiplier 0.0 - N that scales the rated power
# -----------------------------------------------------------------------------
def _smooth_sine(step: int, period: int, lo: float, hi: float) -> float:
    """Smooth sine wave between lo and hi over one period."""
    t = (step % period) / period
    return lo + (hi - lo) * (0.5 - 0.5 * math.cos(2 * math.pi * t))

def _pattern_compressor(step: int, _rng: random.Random) -> float:
    """Fridge compressor: smooth ON/OFF cycle using a cosine envelope."""
    phase = step % 90
    if phase < 22:
        # smooth start/end over the ON window
        return 0.08 + 3.22 * (0.5 - 0.5 * math.cos(2 * math.pi * phase / 22))
    # fan-only idle drift
    return 0.07 + 0.015 * math.sin((phase - 22) * 0.09)

def _pattern_thermostat(step: int, _rng: random.Random) -> float:
    """Water heater / AC: deterministic thermostat cycle with soft edges."""
    phase = step % 120
    if phase < 50:
        if phase < 6:
            return phase / 6.0
        if phase > 44:
            return (50 - phase) / 6.0
        return 1.0
    return 0.0

def _pattern_wifi(step: int, _rng: random.Random) -> float:
    """Router: smooth traffic wave by time-of-day.

    Keep baseline draw realistic for always-on networking gear and add
    moderate diurnal peaks for daytime traffic.
    """
    h = step * DT_H
    if h < 7:
        base = 0.58
    elif h < 9:
        base = 0.58 + 0.27 * (h - 7) / 2
    elif h < 17:
        base = 0.85 + 0.22 * math.sin((h - 9) / 8 * math.pi)
    elif h < 22:
        base = 0.85 - 0.27 * (h - 17) / 5
    else:
        base = 0.58
    return base + 0.06 * math.sin(step * 0.03)

def _pattern_display_sleep(step: int, _rng: random.Random) -> float:
    """HMI tablet: smooth wake/sleep profile."""
    h = step * DT_H
    if h < 6 or h >= 23:
        return 0.06
    if h < 7:
        return 0.06 + 0.64 * (h - 6)
    if h >= 22:
        return 0.70 - 0.64 * (h - 22)
    return 0.70 + 0.08 * math.sin(step * 0.05)

def _pattern_motion(step: int, _rng: random.Random) -> float:
    """Security cameras: smooth activity envelope."""
    h = step * DT_H
    if h < 6 or h >= 23:
        envelope = 0.12
    elif h < 8:
        envelope = 0.12 + 0.25 * (h - 6) / 2
    elif h < 20:
        envelope = 0.37 + 0.18 * math.sin((h - 8) / 12 * math.pi)
    else:
        envelope = 0.37 - 0.25 * (h - 20) / 3
    return max(0.10, envelope + 0.04 * math.sin(step * 0.08))

def _pattern_starlink(step: int, _rng: random.Random) -> float:
    """Starlink: smooth usage curve through the day."""
    h = step * DT_H
    if h < 6 or h >= 23:
        return 0.42
    if h < 8:
        return 0.42 + 0.23 * (h - 6) / 2
    if h < 20:
        return 0.65 + 0.25 * math.sin((h - 8) / 12 * math.pi)
    return 0.65 - 0.23 * (h - 20) / 3

def _pattern_dimmer(step: int, _rng: random.Random) -> float:
    """LED lights: slow deterministic dimmer wave."""
    return 0.80 + 0.10 * math.sin(step * 0.02)

def _pattern_wash(step: int, app_step: int, _rng: random.Random) -> float:
    """Washer: smooth phase transitions for wash/rinse/spin."""
    cycle_step = app_step % 300
    if cycle_step < 120:
        if cycle_step < 8:
            return _smooth_sine(cycle_step, 16, 0.0, 0.80)
        if cycle_step > 112:
            return _smooth_sine(cycle_step - 112, 16, 0.80, 0.45)
        return 0.80
    if cycle_step < 200:
        return 0.40 + 0.02 * math.sin(cycle_step * 0.15)
    if cycle_step < 270:
        sp = cycle_step - 200
        if sp < 10:
            return 0.40 + 0.90 * sp / 10
        if sp > 60:
            return 1.30 - 0.90 * (sp - 60) / 10
        return 1.30 + 0.05 * math.sin(sp * 0.3)
    return 0.0

_CYCLE_FNS = {
    "compressor":    _pattern_compressor,
    "thermostat":    _pattern_thermostat,
    "wifi_traffic":  _pattern_wifi,
    "display_sleep": _pattern_display_sleep,
    "motion_sensor": _pattern_motion,
    "network_load":  _pattern_starlink,
    "dimmer":        _pattern_dimmer,
    "wash_cycle":    lambda s, r: _pattern_wash(s, s, r),
    "constant":      lambda s, _r: 1.0 + 0.015 * math.sin(s * 0.07),
}

def _cycle_multiplier(pattern: str, step: int, rng: random.Random) -> float:
    fn = _CYCLE_FNS.get(pattern, _CYCLE_FNS["constant"])
    return max(0.0, fn(step, rng))


# -----------------------------------------------------------------------------
# SCHEDULE EVALUATION
# -----------------------------------------------------------------------------
def is_in_schedule(
    h: float, schedules: list[dict], day_of_week: int = 0
) -> tuple[bool, float]:
    """
    Returns (is_active, fraction_of_window_used).
    fraction < 1.0 for windows where active_minutes < full window duration.
    """
    for sched in schedules:
        dow = sched.get("days_of_week", "1111111")
        if len(dow) > day_of_week and dow[day_of_week] == "0":
            continue
        s = hhmm_to_h(sched["start_hhmm"])
        e = hhmm_to_h(sched["end_hhmm"])
        if e <= s:
            e += 24  # overnight schedule
        if s <= h < e:
            window_h = e - s
            active_h = sched["active_minutes"] / 60.0
            frac = min(1.0, active_h / max(window_h, 0.01))
            return True, frac
    return False, 0.0


# -----------------------------------------------------------------------------
# MAIN SIMULATION
# -----------------------------------------------------------------------------
def run_day_simulation(
    appliances: list[dict],
    schedules_by_id: dict[int, list[dict]],
    day_config: dict,
    battery_cap_kwh: float = 45.0,
    start_soc: float = 0.87,
    panel_kwp: float = 0.8,   # kWp (peak kW of panels)
    seed: int | None = None,
) -> dict:
    """
    Simulate one 24-hour day at 30-second resolution.

    Returns per-step timeseries + daily aggregates.
    Each step includes V, A, W for every appliance.

    start_soc is a fraction of raw battery_cap_kwh (0.0-1.0).
    The result includes end_soc_raw using the same convention so multi-day
    runs can chain days without bat_tf energy inflation (FIX #3).
    """
    t0 = _time.monotonic()
    rng = random.Random(seed or int(_time.time()))

    sunrise_h = hhmm_to_h(day_config.get("sunrise_hhmm", "06:30"))
    sunset_h  = hhmm_to_h(day_config.get("sunset_hhmm", "19:30"))
    temp_c    = float(day_config.get("temp_c", 22.0))
    cond      = day_config.get("condition", "sunny")
    cloud_pct = float(day_config.get("cloud_pct", 10.0))  # kept for display
    day_idx   = int(day_config.get("day_index", 0))

    # FIX #1 + #2: use wx_factor alone - it already encodes cloud/condition.
    # The old code multiplied wx_factor x irr_factor(cloud_pct), double-
    # penalising clouds and pushing clear-sky irr_factor above 1.0 (+0.05).
    total_irr_f = WEATHER_FACTOR.get(cond, 1.0)

    # LiFePO4 temperature derating
    if   temp_c < 0:  bat_tf = 0.70
    elif temp_c < 10: bat_tf = 0.85
    elif temp_c < 20: bat_tf = 0.95
    else:             bat_tf = 1.00

    max_kwh     = battery_cap_kwh * 0.95 * bat_tf
    floor_kwh   = battery_cap_kwh * RESERVE_LOW    # hard floor (10%)
    warning_kwh = battery_cap_kwh * RESERVE_HIGH   # warning (20%)
    # FIX #3: start_soc is fraction of raw cap; convert to usable kWh correctly
    kwh = min(start_soc * battery_cap_kwh, max_kwh)

    # FIX #9: use the numerically integrated irradiance curve as normaliser
    # instead of the hardcoded 0.6 approximation (~2/pi ~ 0.637, not 0.6).
    cs = sum(solar_irr(s * DT_H, sunrise_h, sunset_h) for s in range(STEPS)) * DT_H
    daily_sol_kwh = panel_kwp * cs * total_irr_f

    # Per-step arrays
    steps_ts   = []        # list of per-step dicts (sampled to ~1/min for MQTT)
    solar_arr  = []
    load_arr   = []
    soc_arr    = []
    app_energy: dict[int, float] = {a["id"]: 0.0 for a in appliances}
    reserve_hit = False

    # FIX #12: symmetric noise ceiling - instantaneous solar cannot exceed
    # the per-step peak (daily budget / cs x 1.0 at solar noon).
    sol_kw_ceil = (daily_sol_kwh / cs) if cs > 0 else 0.0

    for s in range(STEPS):
        h = s * DT_H

        # -- Solar this step ----------------------------------------------
        irr    = solar_irr(h, sunrise_h, sunset_h)
        sol_kw = (daily_sol_kwh * irr / cs) if cs > 0 else 0.0
        # FIX #12: clip symmetrically so positive noise cannot exceed the peak
        sol_kw = max(0.0, min(sol_kw_ceil, sol_kw + rng.gauss(0, sol_kw * 0.05)))

        # -- Appliance loads this step ------------------------------------
        step_apps: dict[int, dict] = {}
        total_kw  = 0.0
        dow = day_idx % 7

        for app in appliances:
            if not app.get("on", True):
                step_apps[app["id"]] = {"v": 0, "a": 0, "w": 0}
                continue

            aid     = app["id"]
            rated_w = (float(app.get("voltage_v", 120))
                       * float(app.get("current_a", 1))
                       * float(app.get("power_factor", 1)))
            eff     = float(app.get("efficiency_pct", 90)) / 100.0
            eff_w   = rated_w / max(eff, 0.01)
            dc      = float(app.get("duty_cycle_pct", 100)) / 100.0
            pattern = app.get("cycle_pattern", "constant")
            always  = bool(app.get("always_on", False))
            scheds  = schedules_by_id.get(aid, [])

            # Determine if active this step
            if always:
                active, frac = True, 1.0
            else:
                active, frac = is_in_schedule(h, scheds, dow)

            if not active:
                step_apps[aid] = {"v": 0, "a": 0, "w": 0}
                continue

            mult = _cycle_multiplier(pattern, s, rng) * frac * dc
            inst_w = eff_w * mult
            inst_w = max(0.0, inst_w)

            # Voltage sag under heavy load (realistic)
            v_nom  = float(app.get("voltage_v", 120))
            v_sag  = v_nom * (1.0 - 0.01 * min(1.0, inst_w / 3000))
            inst_a = inst_w / max(v_sag, 1.0)

            step_apps[aid] = {
                "v": round(v_sag, 2),
                "a": round(inst_a, 3),
                "w": round(inst_w, 1),
            }
            total_kw += inst_w / 1000.0
            app_energy[aid] = app_energy.get(aid, 0.0) + inst_w / 1000.0 * DT_H

        # -- Battery update -----------------------------------------------
        net_kw  = sol_kw - total_kw
        kwh_new = kwh + net_kw * DT_H
        kwh_new = min(max_kwh, kwh_new)   # cap at usable max
        if kwh_new < floor_kwh:
            kwh_new = floor_kwh            # hard protection floor
            reserve_hit = True
        kwh = kwh_new
        soc = kwh / max_kwh * 100.0

        solar_arr.append(round(sol_kw, 4))
        load_arr.append(round(total_kw, 4))
        soc_arr.append(round(soc, 2))

        # Sample every 2 steps (~1 min) for time-series output
        if s % 2 == 0:
            ts = (datetime.combine(date.today() + timedelta(days=day_idx),
                                   datetime.min.time())
                  + timedelta(seconds=s * 30))
            steps_ts.append({
                "ts":          ts.isoformat(),
                "step":        s,
                "h":           round(h, 4),
                "solar_kw":    round(sol_kw, 4),
                "load_kw":     round(total_kw, 4),
                "soc_pct":     round(soc, 2),
                "battery_kwh": round(kwh, 3),
                "net_kw":      round(net_kw, 4),
                "reserve_hit": reserve_hit,
                "appliances":  {str(k): v for k, v in step_apps.items()},
            })

    # -- Hourly aggregates ----------------------------------------------------
    def h24(arr: list) -> list:
        return [round(sum(arr[h * 120:(h + 1) * 120]) / 120, 3) for h in range(24)]

    sol_h = h24(solar_arr)
    ld_h  = h24(load_arr)
    soc_h = [soc_arr[h * 120] for h in range(24)]
    net_h = [round(sol_h[h] - ld_h[h], 3) for h in range(24)]

    tl   = sum(load_arr) * DT_H
    ts_s = sum(solar_arr) * DT_H
    bd   = max(0.0, tl - ts_s)
    cov  = min(1.0, ts_s / tl) if tl > 0 else 1.0
    mn   = min(soc_arr)
    pk   = max(load_arr)

    # FIX #8: autonomy from remaining kWh at end of day, not initial start_soc
    days = (kwh / bd) if bd > 0.01 else 999.0

    # Stability Score
    p1 = min(3.5, (days / 14.0) * 3.5)
    p2 = min(3.0, cov * 3.0)
    p3 = min(2.0, max(0.0, (1.0 - pk / 5.0)) * 2.0)
    p4 = min(1.5, max(0.0, (mn - 20.0) / 80.0) * 1.5)
    si  = round(p1 + p2 + p3 + p4, 2)
    grade = ("S" if si >= 9 else "A" if si >= 8 else "B" if si >= 7 else
             "C" if si >= 6 else "D" if si >= 5 else "F")

    # FIX #3: end_soc_raw is fraction of raw (underated) cap so the next
    # day's init is bat_tf-agnostic.
    end_soc_raw = kwh / battery_cap_kwh

    return {
        "day_index":        day_idx,
        "condition":        cond,
        "temp_c":           temp_c,
        "sunrise":          day_config.get("sunrise_hhmm", "06:30"),
        "sunset":           day_config.get("sunset_hhmm", "19:30"),
        "bat_temp_factor":  round(bat_tf, 2),
        "total_load_kwh":   round(tl, 3),
        "total_solar_kwh":  round(ts_s, 3),
        "bat_draw_kwh":     round(bd, 3),
        "solar_coverage":   round(cov * 100, 1),
        "end_soc_pct":      round(soc_arr[-1], 1),
        "end_soc_raw":      round(end_soc_raw, 6),   # carry-forward for multi-day
        "min_soc_pct":      round(mn, 1),
        "peak_load_kw":     round(pk, 3),
        "reserve_hit":      reserve_hit,
        "days_autonomy":    round(min(days, 999), 1),
        "si_score":         si,
        "si_grade":         grade,
        "si_pillars":       {
            "p1": round(p1, 2), "p2": round(p2, 2),
            "p3": round(p3, 2), "p4": round(p4, 2),
        },
        "sol_hourly":       sol_h,
        "load_hourly":      ld_h,
        "soc_hourly":       soc_h,
        "net_hourly":       net_h,
        "app_energy_kwh":   {str(k): round(v, 3) for k, v in app_energy.items()},
        "timeseries":       steps_ts,   # ~1440 pts/day (1 per min)
        "ms":               round((_time.monotonic() - t0) * 1000, 1),
    }


def run_multi_day(
    appliances: list[dict],
    schedules_by_id: dict[int, list[dict]],
    weather_plan: list[dict],
    battery_cap_kwh: float,
    panel_kwp: float,
    start_soc: float = 0.87,
) -> list[dict]:
    """Run N consecutive days, carrying SOC forward.

    FIX #3: uses end_soc_raw (fraction of raw battery capacity) to chain
    days so temperature derating on one day does not inflate the starting
    energy of the next day.
    """
    results = []
    soc = start_soc   # fraction of raw battery_cap_kwh
    for day_cfg in weather_plan:
        r = run_day_simulation(
            appliances, schedules_by_id, day_cfg,
            battery_cap_kwh, soc, panel_kwp,
            seed=day_cfg.get("day_index", 0) * 1337,
        )
        results.append(r)
        # end_soc_raw is fraction of raw cap - safe across varying bat_tf
        soc = r["end_soc_raw"]
    return results