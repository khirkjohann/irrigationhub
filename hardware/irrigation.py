"""
irrigation.py — Valve control, queue worker, scheduling, auto-control loop,
                and sensor poll loop.
"""
import threading
import time
from datetime import datetime, timedelta

from core.config import (
    AUTO_CONTROL_ENABLED,
    AUTO_FAILSAFE_MINUTES,
    AUTO_HYSTERESIS,
    AUTO_PREDICT_MINUTES,
    CONTROL_LOOP_SECONDS,
    SENSOR_POLL_SECONDS,
    VALID_ZONES,
)
from core.db import get_db
from hardware.gpio_control import write_relay
from core.utils import clamp, parse_ts, voltage_to_pct

# ── Shared irrigation state ───────────────────────────────────────────────────
_valve_lock         = threading.Lock()
_valve_timers:       dict = {}   # zone_id → threading.Timer
_valve_manual_until: dict = {}   # zone_id → datetime (blocks auto_control)

_irr_queue_lock = threading.Lock()
_irr_queue:     list = []
_irr_active          = None   # currently running item
_irr_completed: list = []     # last 50 completed items
_irr_next_id         = 1


def get_irr_snapshot() -> dict:
    with _irr_queue_lock:
        return {
            "queue":     list(_irr_queue),
            "active":    dict(_irr_active) if _irr_active else None,
            "completed": list(_irr_completed),
        }


def queue_add(zone_id: int, volume_liters: float, source: str = "manual") -> dict:
    """Add an irrigation job to the queue. Returns the queued item dict."""
    global _irr_next_id
    conn      = get_db()
    zrow      = conn.execute(
        "SELECT flow_rate_lpm FROM zone_profile WHERE zone_id=?", (zone_id,)
    ).fetchone()
    conn.close()
    flow_rate        = float(zrow["flow_rate_lpm"]) if zrow and zrow["flow_rate_lpm"] else 3.0
    duration_minutes = round(volume_liters / flow_rate, 2)

    # get_latest_moisture imported at module level, safe to call here
    initial_moisture = get_latest_moisture(zone_id)

    with _irr_queue_lock:
        item = {
            "id":               _irr_next_id,
            "zone_id":          zone_id,
            "added_at":         datetime.now().isoformat(timespec="seconds"),
            "initial_moisture": initial_moisture,
            "volume_liters":    round(volume_liters, 2),
            "duration_minutes": duration_minutes,
            "status":           "queued",
            "est_complete":     None,
            "source":           source,
        }
        _irr_next_id += 1
        _irr_queue.append(item)
    return item


# ── Event log ─────────────────────────────────────────────────────────────────

def log_event(zone_id: int, event_type: str, source: str, detail: str = "") -> None:
    conn = get_db()
    conn.execute(
        "INSERT INTO control_events (zone_id, event_type, source, detail) VALUES (?,?,?,?)",
        (zone_id, event_type, source, detail),
    )
    conn.commit()
    conn.close()


# ── Sensor data helpers ───────────────────────────────────────────────────────

def get_latest_moisture(zone_id: int) -> float | None:
    try:
        conn = get_db()
        row = conn.execute(
            f"SELECT soil_moisture_{zone_id} FROM sensor_data ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row and row[0] is not None:
            return round(float(row[0]), 1)
    except Exception:
        pass
    return None


def get_latest_env() -> tuple:
    """Return (temperature, humidity) from the most recent sensor_data row."""
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT temperature, humidity FROM sensor_data ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row:
            return row["temperature"], row["humidity"]
    except Exception:
        pass
    return None, None


# ── Irrigation log persistence ────────────────────────────────────────────────

def persist_irrigation_log(item: dict) -> None:
    try:
        conn = get_db()
        zrow = conn.execute("""
            SELECT zp.flow_rate_lpm, ct.name AS crop_target_name, zp.target_moisture
            FROM zone_profile zp
            LEFT JOIN crop_target ct ON ct.id = zp.crop_target_id
            WHERE zp.zone_id = ?
        """, (item["zone_id"],)).fetchone()
        conn.execute("""
            INSERT INTO irrigation_log
              (zone_id, source, added_at, started_at, completed_at,
               volume_liters, est_duration_minutes, actual_duration_minutes, flow_rate_lpm,
               initial_moisture, post_moisture,
               temperature, humidity,
               crop_target_name, target_moisture,
               day_of_week, hour_of_day)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            item["zone_id"],
            item.get("source", "manual"),
            item.get("added_at"),
            item.get("started_at"),
            item.get("completed_at"),
            item.get("volume_liters"),
            item.get("duration_minutes"),
            item.get("actual_duration_minutes"),
            zrow["flow_rate_lpm"] if zrow else None,
            item.get("initial_moisture"),
            item.get("post_moisture"),
            item.get("temperature"),
            item.get("humidity"),
            zrow["crop_target_name"] if zrow else None,
            zrow["target_moisture"] if zrow else None,
            item.get("day_of_week"),
            item.get("hour_of_day"),
        ))
        conn.commit()
        conn.close()
    except Exception as exc:
        print(f"[IRR-LOG] Persist failed: {exc}")


def load_irrigation_log() -> list:
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM irrigation_log ORDER BY id DESC LIMIT 50"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ── Valve control ─────────────────────────────────────────────────────────────

def _failsafe_close(zone_id: int) -> None:
    log_event(zone_id, "valve_off", "failsafe",
              f"Auto-close after {AUTO_FAILSAFE_MINUTES} min")
    try:
        write_relay(zone_id, "OFF")
    except Exception as exc:
        print(f"[FAILSAFE] GPIO write failed for zone {zone_id}: {exc}")
    conn = get_db()
    conn.execute(
        "UPDATE valve_status SET status='OFF', last_updated=CURRENT_TIMESTAMP WHERE valve_id=?",
        (zone_id,),
    )
    conn.commit()
    conn.close()
    with _valve_lock:
        _valve_timers.pop(zone_id, None)
        _valve_manual_until.pop(zone_id, None)


def set_valve(zone_id: int, state: str,
              auto_close_minutes: float | None = None,
              source: str = "manual") -> None:
    try:
        write_relay(zone_id, state)
    except Exception as exc:
        print(f"[RELAY] Write failed zone {zone_id}: {exc}")

    conn = get_db()
    conn.execute(
        "UPDATE valve_status SET status=?, last_updated=CURRENT_TIMESTAMP WHERE valve_id=?",
        (state, zone_id),
    )
    conn.commit()
    conn.close()

    with _valve_lock:
        old = _valve_timers.pop(zone_id, None)
        if old:
            old.cancel()
        if state == "ON":
            if auto_close_minutes and auto_close_minutes > 0:
                t = threading.Timer(auto_close_minutes * 60, _failsafe_close, args=(zone_id,))
                t.daemon = True
                t.start()
                _valve_timers[zone_id]       = t
                _valve_manual_until[zone_id] = datetime.now() + timedelta(minutes=auto_close_minutes)
            else:
                _valve_manual_until[zone_id] = datetime.now() + timedelta(hours=24)
        else:
            _valve_manual_until.pop(zone_id, None)

    detail = f"Valve {zone_id} → {state}"
    if state == "ON" and auto_close_minutes and auto_close_minutes > 0:
        detail += f" (failsafe {auto_close_minutes} min)"
    log_event(zone_id, f"valve_{state.lower()}", source, detail)


# ── Schedule helpers ──────────────────────────────────────────────────────────
_zone_schedules:   dict = {}
_sched_last_fired: dict = {}


def _dow_js_to_py(js_dow: int) -> int:
    """Convert JS day-of-week (0=Sun) to Python weekday (0=Mon)."""
    return (int(js_dow) - 1) % 7


# ── Prediction helper ─────────────────────────────────────────────────────────

def predict_moisture(rows, zone_id: int, minutes_ahead: float) -> float | None:
    """Linear extrapolation over recent readings. Returns None if not enough data."""
    key   = f"soil_moisture_{zone_id}"
    _raw  = [(parse_ts(r["timestamp"]), float(r[key])) for r in rows if r[key] is not None]
    points = [(ts, m) for ts, m in _raw if ts is not None]
    if len(points) < 4:
        return None
    t0  = points[0][0]
    xs  = [(ts - t0).total_seconds() / 60 for ts, _ in points]
    ys  = [m for _, m in points]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    vx  = sum((x - mx) ** 2 for x in xs)
    if vx == 0:
        return ys[-1]
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / vx
    return clamp(slope * (xs[-1] + minutes_ahead) + (my - slope * mx), 0.0, 100.0)


# ── Queue worker ──────────────────────────────────────────────────────────────

def irr_queue_worker() -> None:
    global _irr_active, _irr_next_id
    post_wait_until = None
    while True:
        time.sleep(3)
        try:
            # ── Schedule checker ──────────────────────────────────────
            now       = datetime.now()
            today_str = now.strftime("%Y-%m-%d")
            for zone_id, sched in list(_zone_schedules.items()):
                if not sched or not sched.get("days") or not sched.get("slots"):
                    continue
                if now.weekday() not in [_dow_js_to_py(d) for d in sched["days"]]:
                    continue
                for slot in sched["slots"]:
                    slot_time = slot.get("time", "")
                    if not slot_time:
                        continue
                    try:
                        hh, mm = map(int, slot_time.split(":"))
                    except ValueError:
                        continue
                    if now.hour != hh or now.minute != mm:
                        continue
                    fire_key = (zone_id, slot_time)
                    if _sched_last_fired.get(fire_key) == today_str:
                        continue
                    _sched_last_fired[fire_key] = today_str
                    conn2 = get_db()
                    zrow2 = conn2.execute(
                        "SELECT flow_rate_lpm FROM zone_profile WHERE zone_id=?", (zone_id,)
                    ).fetchone()
                    conn2.close()
                    flow   = float(zrow2["flow_rate_lpm"]) if zrow2 and zrow2["flow_rate_lpm"] else 3.0
                    liters = float(slot["liters"])
                    with _irr_queue_lock:
                        _irr_queue.append({
                            "id":               _irr_next_id,
                            "zone_id":          zone_id,
                            "added_at":         now.isoformat(timespec="seconds"),
                            "initial_moisture": get_latest_moisture(zone_id),
                            "volume_liters":    round(liters, 2),
                            "duration_minutes": round(liters / flow, 2),
                            "status":           "queued",
                            "est_complete":     None,
                            "source":           "schedule",
                        })
                        _irr_next_id += 1
                    print(f"[SCHED] Zone {zone_id} slot {slot_time} → queued {liters} L")

            # ── Queue processor ───────────────────────────────────────
            action      = None
            action_item = None

            with _irr_queue_lock:
                active_snapshot = dict(_irr_active) if _irr_active else None
                queue_has_items = bool(_irr_queue)

            if active_snapshot is None:
                if queue_has_items:
                    conn = get_db()
                    any_on = conn.execute(
                        "SELECT COUNT(*) FROM valve_status WHERE status='ON'"
                    ).fetchone()[0]
                    conn.close()
                    if not any_on:
                        with _irr_queue_lock:
                            if _irr_active is None and _irr_queue:
                                item = _irr_queue.pop(0)
                                item["status"]       = "running"
                                item["est_complete"] = (
                                    datetime.now() + timedelta(minutes=item["duration_minutes"])
                                ).isoformat(timespec="seconds")
                                _irr_active     = item
                                post_wait_until = None
                                action          = "start"
                                action_item     = item
                                print(f"[IRR-QUEUE] Starting zone {item['zone_id']} "
                                      f"{item['volume_liters']}L ({item['duration_minutes']} min)")
            else:
                conn = get_db()
                row = conn.execute(
                    "SELECT status FROM valve_status WHERE valve_id=?",
                    (active_snapshot["zone_id"],)
                ).fetchone()
                conn.close()
                valve_is_on = row and row["status"] == "ON"
                not_before  = active_snapshot.get("_valve_close_not_before")
                if not_before and datetime.now() < not_before:
                    valve_is_on = True
                with _irr_queue_lock:
                    if not valve_is_on:
                        if post_wait_until is None:
                            post_wait_until = datetime.now() + timedelta(seconds=10)
                        elif datetime.now() >= post_wait_until:
                            action      = "complete"
                            action_item = dict(active_snapshot)

            if action is not None and action_item is not None:
                if action == "start":
                    set_valve(action_item["zone_id"], "ON",
                              auto_close_minutes=action_item["duration_minutes"],
                              source="queue")
                    started_at = datetime.now()
                    with _irr_queue_lock:
                        if _irr_active:
                            _irr_active["started_at"] = started_at.isoformat(timespec="seconds")
                            _irr_active["_valve_close_not_before"] = started_at + timedelta(
                                minutes=action_item["duration_minutes"])
                elif action == "complete":
                    temp, hum   = get_latest_env()
                    started_str = action_item.get("started_at")
                    not_before  = action_item.get("_valve_close_not_before")
                    if not_before:
                        completed_at = not_before
                    elif started_str:
                        try:
                            completed_at = datetime.fromisoformat(started_str) + timedelta(
                                minutes=action_item["duration_minutes"])
                        except Exception:
                            completed_at = datetime.now()
                    else:
                        completed_at = datetime.now()
                    if started_str:
                        try:
                            started_dt  = datetime.fromisoformat(started_str)
                            actual_secs = (completed_at - started_dt).total_seconds()
                            action_item["actual_duration_minutes"] = round(max(0, actual_secs) / 60, 2)
                        except Exception:
                            action_item["actual_duration_minutes"] = None
                    else:
                        action_item["actual_duration_minutes"] = None
                    action_item["post_moisture"] = get_latest_moisture(action_item["zone_id"])
                    action_item["completed_at"]  = completed_at.isoformat(timespec="seconds")
                    action_item["temperature"]   = temp
                    action_item["humidity"]       = hum
                    action_item["day_of_week"]    = completed_at.weekday()
                    action_item["hour_of_day"]    = completed_at.hour
                    action_item.pop("status", None)
                    action_item.pop("est_complete", None)
                    action_item.pop("_valve_close_not_before", None)
                    print(f"[IRR-QUEUE] Completing zone {action_item['zone_id']} "
                          f"actual={action_item.get('actual_duration_minutes')} min")
                    with _irr_queue_lock:
                        _irr_active = None
                        _irr_completed.insert(0, action_item)
                        if len(_irr_completed) > 50:
                            _irr_completed[:] = _irr_completed[:50]
                    post_wait_until = None
                    try:
                        persist_irrigation_log(action_item)
                    except Exception as exc:
                        print(f"[IRR-QUEUE] Persist failed (non-fatal): {exc}")
        except Exception as exc:
            print(f"[IRR-QUEUE] Worker error: {exc}")


# ── Auto-control loop ─────────────────────────────────────────────────────────

def auto_control_loop() -> None:
    while True:
        time.sleep(CONTROL_LOOP_SECONDS)
        if not AUTO_CONTROL_ENABLED:
            continue
        try:
            conn  = get_db()
            rows  = conn.execute(
                """SELECT timestamp, soil_moisture_1, soil_moisture_2,
                          soil_moisture_3, soil_moisture_4
                   FROM sensor_data ORDER BY timestamp DESC LIMIT 48"""
            ).fetchall()
            zones = conn.execute(
                "SELECT zone_id, target_moisture, disabled FROM zone_profile ORDER BY zone_id"
            ).fetchall()
            valves = {r["valve_id"]: r["status"]
                      for r in conn.execute("SELECT valve_id, status FROM valve_status").fetchall()}
            locks  = {r["zone_id"] for r in conn.execute(
                "SELECT zone_id FROM testing_lock WHERE locked_until > datetime('now')"
            ).fetchall()}
            conn.close()

            if not rows:
                continue
            latest  = rows[0]
            history = list(reversed(rows))

            for z in zones:
                zid     = z["zone_id"]
                target  = z["target_moisture"]
                current = latest[f"soil_moisture_{zid}"]
                if z["disabled"] or target is None or current is None or zid in locks:
                    continue
                with _valve_lock:
                    guard = _valve_manual_until.get(zid)
                if guard and guard > datetime.now():
                    continue
                predicted = predict_moisture(history, zid, AUTO_PREDICT_MINUTES) or float(current)
                status    = valves.get(zid, "OFF")
                if predicted < target - AUTO_HYSTERESIS and status == "OFF":
                    set_valve(zid, "ON", auto_close_minutes=AUTO_FAILSAFE_MINUTES, source="auto")
                elif float(current) >= target + AUTO_HYSTERESIS and status == "ON":
                    set_valve(zid, "OFF", source="auto")
        except Exception as exc:
            print(f"[AUTO] {exc}")


# ── Sensor poll loop ──────────────────────────────────────────────────────────

def sensor_poll_loop() -> None:
    """Read hardware every SENSOR_POLL_SECONDS (or 30 s when sensors are missing)."""
    # Deferred import avoids circular import: sensors.py ← irrigation.py ← sensors.py
    from hardware.sensors import get_sensor_snapshot, update_sensor, read_hardware

    while True:
        now = datetime.now().isoformat()
        try:
            snapshot  = read_hardware()
            conn      = get_db()
            zones     = conn.execute(
                "SELECT zone_id, disabled, soil_baseline_id FROM zone_profile ORDER BY zone_id"
            ).fetchall()
            baselines = {r["id"]: r for r in conn.execute(
                "SELECT id, dry_voltage, wet_voltage FROM soil_baseline"
            ).fetchall()}

            moisture: dict = {}
            for z in zones:
                zid = z["zone_id"]
                if z["disabled"]:
                    moisture[zid] = None
                    continue
                vol = snapshot.get(f"soil_probe_{zid}_voltage")
                bl  = baselines.get(z["soil_baseline_id"])
                moisture[zid] = (
                    voltage_to_pct(vol, bl["dry_voltage"], bl["wet_voltage"]) if bl
                    else snapshot.get(f"soil_probe_{zid}")
                )

            conn.execute(
                """INSERT INTO sensor_data
                   (temperature, humidity,
                    soil_moisture_1, soil_moisture_2, soil_moisture_3, soil_moisture_4)
                   VALUES (?,?,?,?,?,?)""",
                (snapshot["temperature"], snapshot["humidity"],
                 moisture.get(1), moisture.get(2), moisture.get(3), moisture.get(4)),
            )
            conn.commit()
            conn.close()

            missing = get_sensor_snapshot().get("missing_inputs", [])
            update_sensor({
                "last_poll": now,
                **({"last_success": now, "last_error": None} if not missing
                   else {"last_error": f"Missing: {', '.join(missing)}"}),
            })
        except Exception as exc:
            from hardware.sensors import update_sensor as _us  # keep import safe inside loop
            _us({"last_poll": now, "last_error": f"Poll error: {exc}"})
            print(f"[SENSOR] {exc}")

        retry_fast = bool(get_sensor_snapshot().get("missing_inputs"))
        time.sleep(30.0 if retry_fast else SENSOR_POLL_SECONDS)
