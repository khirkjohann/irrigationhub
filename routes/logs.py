"""
routes/logs.py — Logs viewer app (runs on port LOG_VIEWER_PORT).

The logs Flask app (logs_app) is a separate WSGI application created in app.py
and served from a daemon thread listening on LOG_VIEWER_PORT.
This module provides the two route functions; app.py registers them on logs_app.
"""
from flask import redirect, render_template, request, url_for

from core.config import LOG_VIEWER_PORT, MAIN_APP_PORT
from core.db import get_db
from core.utils import clamp


def logs_page():
    limit       = clamp(request.args.get("limit", default=200, type=int) or 200, 20, 500)
    conn        = get_db()
    sensor_rows = conn.execute(
        """SELECT datetime(timestamp,'localtime') AS timestamp,
                  temperature, humidity,
                  soil_moisture_1, soil_moisture_2, soil_moisture_3, soil_moisture_4
           FROM sensor_data ORDER BY timestamp DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    event_rows = conn.execute(
        """SELECT datetime(timestamp,'localtime') AS timestamp,
                  zone_id, event_type, source, detail
           FROM control_events ORDER BY timestamp DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return render_template(
        "logs_viewer.html",
        sensor_rows=sensor_rows,
        event_rows=event_rows,
        limit=limit,
        main_port=MAIN_APP_PORT,
        log_port=LOG_VIEWER_PORT,
        cleared=request.args.get("cleared"),
    )


def logs_delete_all():
    conn = get_db()
    conn.execute("DELETE FROM sensor_data")
    conn.execute("DELETE FROM control_events")
    conn.commit()
    conn.close()
    return redirect(url_for("logs_page", cleared="1"))
