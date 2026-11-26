"""Pure state model: Loxone Wallbox values in, Victron evcharger D-Bus values out.

No D-Bus or GLib imports here, so the mapping can be tested on any machine.
"""

import time

# Victron evcharger /Status codes
STATUS_DISCONNECTED = 0
STATUS_CONNECTED = 1
STATUS_CHARGING = 2
STATUS_CHARGED = 3
STATUS_WAIT_SUN = 4
STATUS_WAIT_START = 6

# Victron evcharger /Mode codes
MODE_MANUAL = 0
MODE_AUTO = 1

# Query parameters Loxone may send, with the converter applied to each.
# Units follow the Loxone Wallbox block: power in kW, energy in kWh.
FIELDS = {
    "power": float,      # Cp  - current charging power [kW]
    "meter": float,      # Mr  - meter reading [kWh]
    "session": float,    # Ccc - consumption of the current charge [kWh]
    "connected": int,    # Vc  - vehicle connected [0/1]
    "active": int,       # Cac - charging active [0/1]
    "allowed": int,      # Ca  - charging allowed [0/1]
    "mode": int,         # M   - current charging mode [1..5]
    "limit": float,      # Tp  - target charging power [kW]
}

# Fields that fall back to 0 when Loxone stops repeating them (see EvcsModel.expire).
# meter and mode are kept: they are not 0 in a quiet state and must survive Miniserver outages.
VOLATILE = ("power", "session", "connected", "active", "allowed", "limit")

DEFAULTS = {
    "power": 0.0,
    "meter": 0.0,
    "session": 0.0,
    "connected": 0,
    "active": 0,
    "allowed": 0,
    "mode": 0,
    "limit": 0.0,
}


def parse_value(name, raw):
    """Convert one Loxone value. Accepts '1', '1.0', '1,5' (German decimal comma)."""
    conv = FIELDS[name]
    text = str(raw).strip().replace(",", ".")
    number = float(text)
    if conv is int:
        return int(round(number))
    return number


class EvcsModel:
    def __init__(self, phases=3, voltage=230.0, auto_modes=(1, 2), energy_source="session"):
        if phases not in (1, 3):
            raise ValueError("phases must be 1 or 3")
        if energy_source not in ("session", "meter"):
            raise ValueError("energy_source must be 'session' or 'meter'")
        self.phases = phases
        self.voltage = float(voltage)
        self.auto_modes = set(auto_modes)
        self.energy_source = energy_source
        self.values = dict(DEFAULTS)
        self.updated = {}  # field -> time it was last received
        self.last_received = 0.0
        # charging time bookkeeping
        self.charge_seconds = 0.0
        self._charge_since = None

    # ---- input ---------------------------------------------------------

    def apply(self, updates, now=None):
        """Apply a dict of already parsed values. Returns True if anything changed."""
        now = time.time() if now is None else now
        for key in updates:
            if key in FIELDS:
                self.updated[key] = now
        self.last_received = now
        return self._set(updates, now)

    def expire(self, stale_seconds, now=None):
        """Reset live fields Loxone has not repeated for stale_seconds to 0.

        A Loxone analog Virtual Output command sends every change, but repeats only
        values other than 0. A non-zero value that stops being repeated therefore
        means the 0 that should have followed got lost. Returns True if anything changed.
        """
        if not stale_seconds:
            return False
        now = time.time() if now is None else now
        stale = {key: DEFAULTS[key] for key in VOLATILE
                 if self.values[key] != DEFAULTS[key]
                 and now - self.updated.get(key, 0.0) > stale_seconds}
        return self._set(stale, now) if stale else False

    def _set(self, updates, now):
        changed = False
        was_connected = self.values["connected"]
        for key, value in updates.items():
            if key not in FIELDS:
                continue
            if self.values.get(key) != value:
                self.values[key] = value
                changed = True

        # a session starts when the vehicle is plugged in and ends when it is unplugged
        if was_connected != self.values["connected"]:
            self._stop_clock(now)
            self.charge_seconds = 0.0
        self._sync_clock(now)
        return changed

    def _sync_clock(self, now):
        charging = bool(self.values["connected"] and self.values["active"])
        if charging and self._charge_since is None:
            self._charge_since = now
        elif not charging:
            self._stop_clock(now)

    def _stop_clock(self, now):
        if self._charge_since is not None:
            self.charge_seconds += max(0.0, now - self._charge_since)
            self._charge_since = None

    def charging_time(self, now=None):
        now = time.time() if now is None else now
        running = (now - self._charge_since) if self._charge_since is not None else 0.0
        return int(self.charge_seconds + max(0.0, running))

    # ---- output --------------------------------------------------------

    def status(self):
        v = self.values
        if not v["connected"]:
            return STATUS_DISCONNECTED
        if v["active"]:
            return STATUS_CHARGING
        if not v["allowed"]:
            return STATUS_WAIT_SUN if v["mode"] in self.auto_modes else STATUS_WAIT_START
        if v["session"] > 0:
            return STATUS_CHARGED
        return STATUS_CONNECTED

    def dbus_values(self, now=None):
        v = self.values
        power_w = max(0.0, v["power"]) * 1000.0
        per_phase = power_w / self.phases
        phase_power = [per_phase] * self.phases + [0.0] * (3 - self.phases)
        amps = power_w / (self.voltage * self.phases) if power_w else 0.0
        set_amps = max(0.0, v["limit"]) * 1000.0 / (self.voltage * self.phases)
        energy = v["session"] if self.energy_source == "session" else v["meter"]
        status = self.status()
        charging_time = self.charging_time(now) if status != STATUS_DISCONNECTED else 0
        return {
            "/Ac/Power": round(power_w, 1),
            "/Ac/L1/Power": round(phase_power[0], 1),
            "/Ac/L2/Power": round(phase_power[1], 1),
            "/Ac/L3/Power": round(phase_power[2], 1),
            "/Ac/Voltage": self.voltage,
            "/Ac/Energy/Forward": round(max(0.0, energy), 3),
            "/Current": round(amps, 1),
            "/SetCurrent": round(set_amps, 1),
            "/Status": status,
            "/Mode": MODE_AUTO if v["mode"] in self.auto_modes else MODE_MANUAL,
            "/StartStop": 1 if v["allowed"] else 0,
            "/ChargingTime": charging_time,
            # newer EVCS firmware paths, read by the gui-v2 overview tile
            "/Session/Energy": round(max(0.0, v["session"]), 3),
            "/Session/Time": charging_time,
        }

    # ---- persistence ---------------------------------------------------

    def to_dict(self, now=None):
        now = time.time() if now is None else now
        return {
            "values": dict(self.values),
            "charge_seconds": self.charging_time(now),
            "charging": self._charge_since is not None,
            "last_received": self.last_received,
        }

    def load_dict(self, data, now=None):
        now = time.time() if now is None else now
        for key, value in (data.get("values") or {}).items():
            if key in FIELDS:
                self.values[key] = FIELDS[key](value)
        self.charge_seconds = float(data.get("charge_seconds") or 0.0)
        self.last_received = float(data.get("last_received") or 0.0)
        # restored values get a fresh window; Loxone's next repeat confirms or they expire
        self.updated = {key: now for key in self.values}
        # time spent while the driver was down is not counted
        self._charge_since = now if data.get("charging") else None
