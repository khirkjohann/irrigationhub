"""
routes/thesis.py — Thesis dashboard process management routes.
"""
import os
import subprocess
import time

from flask import Blueprint, jsonify

bp = Blueprint("thesis", __name__)

_THESIS_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "thesis_dashboard.py")
_THESIS_PYTHON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "irrigation_env", "bin", "python")


def _thesis_pid() -> int | None:
    """Return PID of a running thesis_dashboard.py process, or None."""
    try:
        out  = subprocess.check_output(["pgrep", "-f", "thesis_dashboard.py"], text=True).strip()
        own  = os.getpid()
        pids = [int(p) for p in out.splitlines() if p.strip() and int(p) != own]
        return pids[0] if pids else None
    except subprocess.CalledProcessError:
        return None


@bp.route("/api/thesis/status")
def api_thesis_status():
    pid = _thesis_pid()
    return jsonify({"running": pid is not None, "pid": pid})


@bp.route("/api/thesis/start", methods=["POST"])
def api_thesis_start():
    if _thesis_pid():
        return jsonify({"success": True, "message": "Thesis dashboard is already running."})
    try:
        subprocess.Popen(
            [_THESIS_PYTHON, _THESIS_SCRIPT],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        time.sleep(1.5)
        pid = _thesis_pid()
        if pid:
            return jsonify({"success": True, "message": f"Thesis dashboard started (PID {pid})."})
        return jsonify({
            "error": "Process launched but not detected — check thesis_dashboard.py for errors."
        }), 500
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/thesis/stop", methods=["POST"])
def api_thesis_stop():
    pid = _thesis_pid()
    if not pid:
        return jsonify({"success": True, "message": "Thesis dashboard is not running."})
    try:
        subprocess.run(["kill", str(pid)], check=True)
        return jsonify({"success": True, "message": f"Thesis dashboard stopped (PID {pid})."})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
