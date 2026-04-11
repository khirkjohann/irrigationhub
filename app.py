"""
app.py — Causwagan Norte Automated Irrigation System
Thin application shell: creates the Flask apps, registers blueprints,
starts background workers, and runs the main server.

Port layout:
  5000 → Main dashboard  (app)
  5001 → Logs viewer     (logs_app)
"""
import threading

from flask import Flask

from core.config import APP_START_TIME, AUTO_CONTROL_ENABLED, LOG_VIEWER_PORT, MAIN_APP_PORT
from core.db import initialize_db
from hardware.gpio_control import init_gpio
from hardware.irrigation import (
    auto_control_loop,
    irr_queue_worker,
    load_irrigation_log,
    sensor_poll_loop,
    _irr_completed,
)

# ── Flask application instances ───────────────────────────────────────────────

app      = Flask(__name__)
logs_app = Flask("irrigation-logs",
                 template_folder="templates",
                 static_folder="static")

# ── Blueprint registration ────────────────────────────────────────────────────

from routes.pages       import bp as pages_bp
from routes.data        import bp as data_bp
from routes.valve       import bp as valve_bp
from routes.irrigation  import bp as irr_bp
from routes.calibration import bp as cal_bp
from routes.zones       import bp as zones_bp
from routes.diagnostics import bp as diag_bp
from routes.network     import bp as net_bp
from routes.thesis      import bp as thesis_bp

import routes.data as _data_module
_data_module.APP_START_TIME = APP_START_TIME   # share the authoritative start time

for _bp in (pages_bp, data_bp, valve_bp, irr_bp, cal_bp,
            zones_bp, diag_bp, net_bp, thesis_bp):
    app.register_blueprint(_bp)

# ── Logs viewer routes (separate app, same process) ───────────────────────────

from routes.logs import logs_page, logs_delete_all

logs_app.add_url_rule("/",           "logs_page",       logs_page)
logs_app.add_url_rule("/delete-all", "logs_delete_all", logs_delete_all, methods=["POST"])

# ── Background workers ────────────────────────────────────────────────────────

_workers_lock    = threading.Lock()
_workers_started = False


def _run_logs_server() -> None:
    logs_app.run(host="0.0.0.0", port=LOG_VIEWER_PORT, debug=False, use_reloader=False)


def start_workers() -> None:
    global _workers_started
    with _workers_lock:
        if _workers_started:
            return
        init_gpio()
        _irr_completed[:] = load_irrigation_log()
        threading.Thread(target=sensor_poll_loop,  daemon=True, name="sensor-loop").start()
        if AUTO_CONTROL_ENABLED:
            threading.Thread(target=auto_control_loop, daemon=True, name="auto-control").start()
        threading.Thread(target=_run_logs_server,  daemon=True, name="logs-server").start()
        threading.Thread(target=irr_queue_worker,  daemon=True, name="irr-queue").start()
        from hardware.gpio_control import fan_control_loop
        threading.Thread(target=fan_control_loop,  daemon=True, name="fan-control").start()
        _workers_started = True


# ── Entry point ───────────────────────────────────────────────────────────────

initialize_db()
start_workers()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=MAIN_APP_PORT, debug=True, use_reloader=False)
