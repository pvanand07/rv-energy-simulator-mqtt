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
  compressor    - fridge: ON 22/90 steps (24%), OFF at 0.06x (fan only)
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
from typing import Any

# -----------------------------------------------------------------------------
# CONSTANTS
# -----------------------------------------------------------------------------
STEPS   = 2880          # 30-second steps per 24h
DT_H    = 30 / 3600     # hours per step
WEATHER_FACTOR = {"sunny": 1.00, "partly": 0.60, "overcast": 0.25, "rainy": 0.05}
RESERVE_LOW  = 0.10     # 10% - hard floor (battery protection)
RESERVE_HIGH = 0.20     # 20% - soft reserve warning threshold

# Patterns that internally model their own duty cycle; dc must NOT be
# applied as an additional multiplier for these (FIX #7).
_SELF_CYCLING_PATTERNS = frozenset({
    "compressor", "thermostat", "wifi_traffic", "display_sleep",
    "motion_sensor", "network_load", "dimmer", "wash_cycle",
})


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
def _pattern_compressor(step: int, rng: random.Random) -> float:
    """Fridge compressor: ON 22/90 steps (~24%), then fan-only 0.06x"""
    phase = step % 90
    if phase < 22:
        return 3.5 + rng.uniform(-0.2, 0.2)  # startup spike included
    return 0.06 + rng.uniform(0, 0.01)

def _pattern_thermostat(step: int, rng: random.Random) -> float:
    """Water heater / AC: stable 120-step thermostat cycle (50 ON, 70 OFF).
    FIX #10: deterministic phase from step - no per-step randint() jitter
    that previously caused erratic flicker instead of smooth cycling.
    """
    phase = step % 120
    return 1.0 if phase < 50 else 0.0

def _pattern_wifi(step: int, rng: random.Random) -> float:
    """Router: mostly idle, occasional burst"""
    if rng.random() < 0.05:     # 5% chance of data burst each step
        return 2.5 + rng.uniform(0, 1.5)
    return 0.35 + rng.uniform(0, 0.1)

def _pattern_display_sleep(step: int, rng: random.Random) -> float:
    """HMI tablet: night dim, day active, random wake events"""
    h = step * DT_H
    if 22 <= h or h < 6:
        return 0.05 + rng.uniform(0, 0.02)  # screen off
    if rng.random() < 0.1:
        return 1.5 + rng.uniform(0, 0.5)    # user interaction spike
    return 0.7 + rng.uniform(0, 0.1)

def _pattern_motion(step: int, rng: random.Random) -> float:
    """Security cameras: standby + motion-triggered active"""
    h = step * DT_H
    motion_prob = 0.25 if 7 <= h < 22 else 0.04
    if rng.random() < motion_prob:
        return 1.2 + rng.uniform(0, 0.3)   # recording
    return 0.12 + rng.uniform(0, 0.02)     # standby IR

def _pattern_starlink(step: int, rng: random.Random) -> float:
    """Starlink: base draw + usage spikes (video calls, downloads)"""
    h = step * DT_H
    base = 0.5 if 22 <= h or h < 6 else 0.75
    if 8 <= h < 18 and rng.random() < 0.08:
        return base + rng.uniform(0.3, 0.6)  # heavy usage
    return base + rng.uniform(0, 0.1)

def _pattern_dimmer(step: int, rng: random.Random) -> float:
    """LED lights: gradual fade, random brightness adjustments"""
    return max(0.1, min(1.1, 0.8 + rng.gauss(0, 0.12)))

def _pattern_wash(step: int, app_step: int, rng: random.Random) -> float:
    """Washer: wash (high), rinse (medium), spin (very high), done.
    FIX #11: app_step is schedule-relative so cycle phases are independent
    of where in the simulation day the schedule window falls.
    """
    cycle_step = app_step % 300  # ~150 min full cycle in 30s steps
    if cycle_step < 120:   return 0.8 + rng.uniform(-0.05, 0.05)  # wash
    if cycle_step < 200:   return 0.4 + rng.uniform(-0.03, 0.03)  # rinse
    if cycle_step < 270:   return 1.3 + rng.uniform(-0.1, 0.1)    # spin
    return 0.0  # done

# FIX #11: all pattern functions now share a consistent 3-arg signature
# (step, app_step, rng). Patterns that don't need app_step ignore it.
_CYCLE_FNS = {
    "compressor":    lambda s, _a, r: _pattern_compressor(s, r),
    "thermostat":    lambda s, _a, r: _pattern_thermostat(s, r),
    "wifi_traffic":  lambda s, _a, r: _pattern_wifi(s, r),
    "display_sleep": lambda s, _a, r: _pattern_display_sleep(s, r),
    "motion_sensor": lambda s, _a, r: _pattern_motion(s, r),
    "network_load":  lambda s, _a, r: _pattern_starlink(s, r),
    "dimmer":        lambda s, _a, r: _pattern_dimmer(s, r),
    "wash_cycle":    lambda s, a,  r: _pattern_wash(s, a, r),
    "constant":      lambda s, _a, r: 1.0 + r.gauss(0, 0.02),
}

def _cycle_multiplier(
    pattern: str, step: int, app_step: int, rng: random.Random
) -> float:
    fn = _CYCLE_FNS.get(pattern, _CYCLE_FNS["constant"])
    return max(0.0, fn(step, app_step, rng))


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
    # FIX #11: per-appliance schedule-relative step counter for wash_cycle
    app_sched_step: dict[int, int] = {a["id"]: 0 for a in appliances}
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

            # FIX #11: increment schedule-relative counter only while active
            app_sched_step[aid] += 1
            app_s = app_sched_step[aid]

            raw_mult = _cycle_multiplier(pattern, s, app_s, rng)

            # FIX #7: self-cycling patterns already encode their duty ratio
            # internally - multiplying by dc again double-penalises them.
            # Only apply dc for "constant" pattern appliances.
            if pattern in _SELF_CYCLING_PATTERNS:
                mult = raw_mult * frac
            else:
                mult = raw_mult * frac * dc

            inst_w = eff_w * mult + rng.gauss(0, eff_w * 0.01)
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