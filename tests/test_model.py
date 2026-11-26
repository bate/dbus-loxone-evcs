import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from evcs_model import (  # noqa: E402
    MODE_AUTO, MODE_MANUAL, STATUS_CHARGED, STATUS_CHARGING, STATUS_CONNECTED,
    STATUS_DISCONNECTED, STATUS_WAIT_START, STATUS_WAIT_SUN, EvcsModel, parse_value,
)


class ParseTest(unittest.TestCase):
    def test_decimal_comma_and_ints(self):
        self.assertEqual(parse_value("power", "3,7"), 3.7)
        self.assertEqual(parse_value("connected", "1.0"), 1)
        self.assertEqual(parse_value("mode", " 2 "), 2)

    def test_rejects_garbage(self):
        with self.assertRaises(ValueError):
            parse_value("power", "abc")


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.m = EvcsModel(auto_modes=(1, 2))

    def test_disconnected(self):
        self.assertEqual(self.m.status(), STATUS_DISCONNECTED)

    def test_charging(self):
        self.m.apply({"connected": 1, "active": 1, "allowed": 1})
        self.assertEqual(self.m.status(), STATUS_CHARGING)

    def test_waiting_for_sun_in_auto_mode(self):
        self.m.apply({"connected": 1, "allowed": 0, "mode": 2})
        self.assertEqual(self.m.status(), STATUS_WAIT_SUN)

    def test_waiting_for_start_in_manual_mode(self):
        self.m.apply({"connected": 1, "allowed": 0, "mode": 3})
        self.assertEqual(self.m.status(), STATUS_WAIT_START)

    def test_connected_then_charged(self):
        self.m.apply({"connected": 1, "allowed": 1})
        self.assertEqual(self.m.status(), STATUS_CONNECTED)
        self.m.apply({"session": 4.2})
        self.assertEqual(self.m.status(), STATUS_CHARGED)


class DbusValuesTest(unittest.TestCase):
    def test_power_split_three_phases(self):
        m = EvcsModel(phases=3, voltage=230)
        m.apply({"power": 11.04, "connected": 1, "active": 1, "allowed": 1, "limit": 11.04})
        d = m.dbus_values()
        self.assertEqual(d["/Ac/Power"], 11040.0)
        self.assertEqual(d["/Ac/L1/Power"], 3680.0)
        self.assertEqual(d["/Ac/L3/Power"], 3680.0)
        self.assertEqual(d["/Current"], 16.0)
        self.assertEqual(d["/SetCurrent"], 16.0)

    def test_single_phase(self):
        m = EvcsModel(phases=1)
        m.apply({"power": 3.68})
        d = m.dbus_values()
        self.assertEqual(d["/Ac/L1/Power"], 3680.0)
        self.assertEqual(d["/Ac/L2/Power"], 0.0)

    def test_energy_source(self):
        m = EvcsModel(energy_source="meter")
        m.apply({"meter": 63.097, "session": 1.5})
        self.assertEqual(m.dbus_values()["/Ac/Energy/Forward"], 63.097)
        m2 = EvcsModel(energy_source="session")
        m2.apply({"meter": 63.097, "session": 1.5})
        self.assertEqual(m2.dbus_values()["/Ac/Energy/Forward"], 1.5)
        # the session path always carries the session energy
        self.assertEqual(m.dbus_values()["/Session/Energy"], 1.5)

    def test_mode_mapping(self):
        m = EvcsModel(auto_modes=(1, 2))
        m.apply({"mode": 1})
        self.assertEqual(m.dbus_values()["/Mode"], MODE_AUTO)
        m.apply({"mode": 3})
        self.assertEqual(m.dbus_values()["/Mode"], MODE_MANUAL)


class ChargingTimeTest(unittest.TestCase):
    def test_counts_only_while_charging_and_resets_on_unplug(self):
        m = EvcsModel()
        m.apply({"connected": 1, "active": 1}, now=1000)
        self.assertEqual(m.charging_time(now=1060), 60)
        m.apply({"active": 0}, now=1100)
        self.assertEqual(m.charging_time(now=2000), 100)
        m.apply({"active": 1}, now=2000)
        self.assertEqual(m.charging_time(now=2050), 150)
        m.apply({"connected": 0, "active": 0}, now=2100)
        self.assertEqual(m.charging_time(now=2200), 0)
        self.assertEqual(m.dbus_values(now=2200)["/ChargingTime"], 0)

    def test_new_session_starts_at_zero(self):
        m = EvcsModel()
        m.apply({"connected": 1, "active": 1}, now=0)
        m.apply({"connected": 0, "active": 0}, now=50)
        m.apply({"connected": 1}, now=100)
        self.assertEqual(m.charging_time(now=200), 0)

    def test_charging_clock_stops_when_values_expire(self):
        m = EvcsModel()
        m.apply({"connected": 1, "active": 1}, now=0)
        m.expire(700, now=800)
        self.assertEqual(m.status(), STATUS_DISCONNECTED)
        self.assertEqual(m.charging_time(now=900), 0)

    def test_persistence_roundtrip(self):
        m = EvcsModel()
        m.apply({"connected": 1, "active": 1, "power": 7.2, "session": 3.3}, now=0)
        saved = m.to_dict(now=120)
        restored = EvcsModel()
        restored.load_dict(saved, now=500)
        self.assertEqual(restored.values["power"], 7.2)
        self.assertEqual(restored.charging_time(now=530), 150)


class ExpireTest(unittest.TestCase):
    def test_unrepeated_live_values_fall_back_to_zero(self):
        m = EvcsModel()
        m.apply({"connected": 1, "active": 1, "allowed": 1, "power": 7.2, "meter": 140.7, "mode": 3}, now=0)
        # Loxone repeats non-zero values every 300 s; power stopped being repeated
        m.apply({"connected": 1, "active": 1, "allowed": 1, "meter": 140.7, "mode": 3}, now=600)
        self.assertTrue(m.expire(700, now=750))
        self.assertEqual(m.values["power"], 0.0)
        self.assertEqual(m.values["connected"], 1)

    def test_meter_and_mode_are_kept(self):
        m = EvcsModel()
        m.apply({"meter": 140.7, "mode": 3, "connected": 1}, now=0)
        m.expire(700, now=5000)
        self.assertEqual(m.values["meter"], 140.7)
        self.assertEqual(m.values["mode"], 3)
        self.assertEqual(m.values["connected"], 0)

    def test_disabled_with_zero(self):
        m = EvcsModel()
        m.apply({"power": 3.0}, now=0)
        self.assertFalse(m.expire(0, now=10_000))
        self.assertEqual(m.values["power"], 3.0)

    def test_restored_state_gets_a_fresh_window(self):
        m = EvcsModel()
        m.apply({"connected": 1, "power": 2.0}, now=0)
        restored = EvcsModel()
        restored.load_dict(m.to_dict(now=0), now=10_000)
        self.assertFalse(restored.expire(700, now=10_500))
        self.assertTrue(restored.expire(700, now=10_800))


if __name__ == "__main__":
    unittest.main()
