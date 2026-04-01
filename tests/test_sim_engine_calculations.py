import unittest

from app.sim_engine import (
    hhmm_to_h,
    is_in_schedule,
    run_day_simulation,
    run_multi_day,
    solar_irr,
)


class TestSimEngineCalculations(unittest.TestCase):
    def test_hhmm_to_h(self):
        self.assertEqual(hhmm_to_h("00:00"), 0.0)
        self.assertEqual(hhmm_to_h("06:30"), 6.5)
        self.assertEqual(hhmm_to_h("23:45"), 23.75)

    def test_solar_irr_curve(self):
        sunrise = 6.0
        sunset = 18.0

        self.assertEqual(solar_irr(5.99, sunrise, sunset), 0.0)
        self.assertEqual(solar_irr(18.01, sunrise, sunset), 0.0)
        self.assertAlmostEqual(solar_irr(12.0, sunrise, sunset), 1.0, places=6)
        self.assertAlmostEqual(solar_irr(9.0, sunrise, sunset), solar_irr(15.0, sunrise, sunset), places=6)

    def test_is_in_schedule_with_fraction_and_day_filter(self):
        schedules = [
            {
                "start_hhmm": "08:00",
                "end_hhmm": "10:00",
                "active_minutes": 60,
                "days_of_week": "0100000",  # only Tuesday
            }
        ]

        active_monday, frac_monday = is_in_schedule(9.0, schedules, day_of_week=0)
        self.assertFalse(active_monday)
        self.assertEqual(frac_monday, 0.0)

        active_tuesday, frac_tuesday = is_in_schedule(9.0, schedules, day_of_week=1)
        self.assertTrue(active_tuesday)
        self.assertAlmostEqual(frac_tuesday, 0.5, places=6)

    def test_is_in_schedule_overnight_window(self):
        schedules = [
            {
                "start_hhmm": "23:00",
                "end_hhmm": "01:00",
                "active_minutes": 120,
                "days_of_week": "1111111",
            }
        ]
        active, frac = is_in_schedule(23.5, schedules, day_of_week=0)
        self.assertTrue(active)
        self.assertAlmostEqual(frac, 1.0, places=6)

    def test_run_day_simulation_enforces_reserve_floor(self):
        appliances = [
            {
                "id": 1,
                "name": "Heavy Load",
                "on": True,
                "always_on": True,
                "voltage_v": 120,
                "current_a": 25,
                "power_factor": 1.0,
                "efficiency_pct": 100,
                "duty_cycle_pct": 100,
                "cycle_pattern": "constant",
            }
        ]
        schedules = {1: []}
        day_config = {
            "day_index": 0,
            "sunrise_hhmm": "06:00",
            "sunset_hhmm": "18:00",
            "temp_c": 25,
            "condition": "rainy",
            "cloud_pct": 100,
        }

        result = run_day_simulation(
            appliances=appliances,
            schedules_by_id=schedules,
            day_config=day_config,
            battery_cap_kwh=10.0,
            start_soc=0.30,
            panel_kwp=0.0,
            seed=42,
        )

        self.assertTrue(result["reserve_hit"])
        self.assertGreaterEqual(result["min_soc_pct"], 10.0)
        self.assertAlmostEqual(result["end_soc_raw"], 0.10, places=6)

    def test_run_multi_day_carries_soc_forward(self):
        appliances = []
        schedules = {}
        weather = [
            {"day_index": 0, "condition": "sunny", "temp_c": 25, "sunrise_hhmm": "06:00", "sunset_hhmm": "18:00"},
            {"day_index": 1, "condition": "overcast", "temp_c": 5, "sunrise_hhmm": "06:00", "sunset_hhmm": "18:00"},
        ]

        results = run_multi_day(
            appliances=appliances,
            schedules_by_id=schedules,
            weather_plan=weather,
            battery_cap_kwh=20.0,
            panel_kwp=0.0,
            start_soc=0.5,
        )

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["day_index"], 0)
        self.assertEqual(results[1]["day_index"], 1)
        self.assertAlmostEqual(results[0]["end_soc_raw"], results[1]["end_soc_raw"], places=6)


if __name__ == "__main__":
    unittest.main()
