#!/usr/bin/env python3
"""Victron Venus OS driver: publishes a Loxone Wallbox as com.victronenergy.evcharger.

Loxone pushes the Wallbox block outputs via a Virtual Output (HTTP GET), e.g.
    GET /set?power=3.7
    GET /set?connected=1&active=1
This driver keeps the latest values, maps them to the evcharger D-Bus paths,
and the GX (GUI, VRM logger, Modbus TCP) picks them up from there.
"""

import configparser
import json
import logging
import logging.handlers
import os
import platform
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, BASE_DIR)
sys.path.insert(1, "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python")

from evcs_model import FIELDS, EvcsModel, parse_value  # noqa: E402

VERSION = "1.1.0"
PRODUCT_ID = 0xC025  # Victron EV Charging Station
log = logging.getLogger("dbus-loxone-evcs")


def load_config():
    cfg = configparser.ConfigParser()
    path = os.path.join(BASE_DIR, "config.ini")
    if not cfg.read(path):
        raise SystemExit("config.ini not found at %s (copy config.sample.ini)" % path)
    return cfg["DEFAULT"]


def setup_logging(level):
    handler = logging.handlers.RotatingFileHandler(
        os.path.join(BASE_DIR, "current.log"), maxBytes=1_000_000, backupCount=2)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


class Inbox:
    """Thread-safe hand-over from the HTTP thread to the GLib main loop."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pending = {}

    def put(self, updates):
        with self._lock:
            self._pending.update(updates)

    def take(self):
        with self._lock:
            pending, self._pending = self._pending, {}
        return pending


def make_handler(inbox, model, token):
    class Handler(BaseHTTPRequestHandler):
        server_version = "dbus-loxone-evcs/" + VERSION

        def _reply(self, code, body):
            data = (json.dumps(body) + "\n").encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlparse(self.path)
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            if token and query.pop("token", None) != token:
                return self._reply(403, {"error": "invalid token"})
            if url.path == "/state":
                return self._reply(200, {"received": model.to_dict(), "dbus": model.dbus_values()})
            if url.path != "/set":
                return self._reply(404, {"error": "use /set or /state"})
            updates, errors = {}, {}
            for key, raw in query.items():
                if key not in FIELDS:
                    errors[key] = "unknown field"
                    continue
                try:
                    updates[key] = parse_value(key, raw)
                except ValueError:
                    errors[key] = "not a number: %r" % raw
            if updates:
                inbox.put(updates)
            log.debug("HTTP %s -> %s %s", self.client_address[0], updates, errors)
            return self._reply(400 if errors and not updates else 200, {"accepted": updates, "errors": errors})

        do_POST = do_GET

        def log_message(self, fmt, *args):  # keep HTTP access lines out of the log
            pass

    return Handler


class LoxoneEvcsService:
    def __init__(self, conf, model, inbox):
        from vedbus import VeDbusService

        self.model = model
        self.inbox = inbox
        self.state_file = os.path.join(BASE_DIR, "state.json")
        self._dirty = False
        self._last_save = 0.0
        self.stale_seconds = conf.getint("StaleSeconds", 700)
        instance = conf.getint("DeviceInstance", 43)

        svc = VeDbusService("com.victronenergy.evcharger.loxone_%02d" % instance, register=False)
        svc.add_path("/Mgmt/ProcessName", os.path.realpath(__file__))
        svc.add_path("/Mgmt/ProcessVersion", VERSION + " on Python " + platform.python_version())
        svc.add_path("/Mgmt/Connection", "Loxone Virtual Output (HTTP :%s)" % conf.get("ListenPort", "8090"))
        svc.add_path("/DeviceInstance", instance)
        svc.add_path("/ProductId", PRODUCT_ID)
        svc.add_path("/ProductName", "Loxone Wallbox")
        svc.add_path("/CustomName", conf.get("CustomName", "Wallbox"))
        svc.add_path("/FirmwareVersion", VERSION)
        svc.add_path("/HardwareVersion", 0)
        svc.add_path("/Serial", conf.get("Serial", "loxone-%02d" % instance))
        svc.add_path("/Connected", 1)
        svc.add_path("/Position", conf.getint("AcPosition", 1))
        svc.add_path("/MaxCurrent", conf.getint("MaxCurrent", 16))
        svc.add_path("/UpdateIndex", 0)

        fmt = {
            "/Ac/Power": "W", "/Ac/L1/Power": "W", "/Ac/L2/Power": "W", "/Ac/L3/Power": "W",
            "/Ac/Voltage": "V", "/Ac/Energy/Forward": "kWh", "/Current": "A", "/SetCurrent": "A",
            "/ChargingTime": "s", "/Session/Energy": "kWh", "/Session/Time": "s",
        }
        for path, value in model.dbus_values().items():
            unit = fmt.get(path)
            text = (lambda u: (lambda p, v: "%s %s" % (v, u)))(unit) if unit else None
            svc.add_path(path, value, gettextcallback=text)
        svc.register()
        self.svc = svc

    def tick(self):
        try:
            updates = self.inbox.take()
            if updates:
                if self.model.apply(updates):
                    self._dirty = True
                log.debug("applied %s", updates)
            if self.model.expire(self.stale_seconds):
                self._dirty = True
                log.info("Loxone stopped repeating live values, reset to 0: %s", self.model.values)
            values = self.model.dbus_values()
            changed = False
            for path, value in values.items():
                if self.svc[path] != value:
                    self.svc[path] = value
                    changed = True
            if changed:
                self.svc["/UpdateIndex"] = (self.svc["/UpdateIndex"] + 1) % 256
            now = time.time()
            if self._dirty and now - self._last_save > 10:
                self.save_state()
        except Exception:
            log.exception("tick failed")
        return True

    def save_state(self):
        tmp = self.state_file + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.model.to_dict(), fh)
        os.replace(tmp, self.state_file)
        self._dirty = False
        self._last_save = time.time()

    def load_state(self):
        try:
            with open(self.state_file) as fh:
                self.model.load_dict(json.load(fh))
            log.info("restored state: %s", self.model.values)
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("could not read %s, starting empty", self.state_file)

    def heartbeat(self):
        age = time.time() - self.model.last_received if self.model.last_received else None
        log.info("alive: last value from Loxone %s ago, values %s",
                 "%ds" % age if age is not None else "never", self.model.values)
        return True


def main():
    conf = load_config()
    setup_logging(conf.get("LogLevel", "INFO"))
    log.info("starting dbus-loxone-evcs %s", VERSION)

    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib

    DBusGMainLoop(set_as_default=True)

    model = EvcsModel(
        phases=conf.getint("Phases", 3),
        voltage=conf.getfloat("Voltage", 230.0),
        auto_modes=[int(m) for m in conf.get("AutoModes", "1,2").split(",") if m.strip()],
        energy_source=conf.get("EnergySource", "session"),
    )
    inbox = Inbox()
    service = LoxoneEvcsService(conf, model, inbox)
    service.load_state()

    host, port = conf.get("ListenHost", "0.0.0.0"), conf.getint("ListenPort", 8090)
    httpd = ThreadingHTTPServer((host, port), make_handler(inbox, model, conf.get("Token", "").strip()))
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
    log.info("listening on %s:%d", host, port)

    GLib.timeout_add(1000, service.tick)
    GLib.timeout_add_seconds(max(60, conf.getint("HeartbeatMinutes", 15) * 60), service.heartbeat)
    try:
        GLib.MainLoop().run()
    finally:
        service.save_state()


if __name__ == "__main__":
    main()
