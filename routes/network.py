"""
routes/network.py — WiFi scan, connect, hotspot, profile management routes.
"""
import subprocess

from flask import Blueprint, jsonify, request

from core.config import WIFI_PROFILE_PREFIX

bp = Blueprint("network", __name__)


def _nmcli(*args, timeout: int = 15) -> str:
    """Run nmcli with sudo; return stdout. Raises CalledProcessError on failure."""
    result = subprocess.run(
        ["sudo", "nmcli", "--wait", str(timeout), *args],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        err = subprocess.CalledProcessError(result.returncode, "nmcli")
        err.stderr = result.stderr.strip()
        raise err
    return result.stdout.strip()


@bp.route("/api/network/wifi-scan", methods=["GET"])
def api_wifi_scan():
    try:
        raw = subprocess.run(
            ["sudo", "nmcli", "--wait", "10", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY",
             "device", "wifi", "list", "--rescan", "auto"],
            capture_output=True, text=True,
        )
        networks = []
        seen     = set()
        for line in raw.stdout.strip().splitlines():
            parts = line.split(":", 3)
            if len(parts) < 4:
                continue
            in_use   = parts[0].strip() == "*"
            ssid     = parts[1].strip()
            signal   = parts[2].strip()
            security = parts[3].strip()
            if not ssid or ssid in seen:
                continue
            seen.add(ssid)
            networks.append({
                "ssid":     ssid,
                "signal":   int(signal) if signal.isdigit() else 0,
                "security": security,
                "open":     security in ("", "--"),
                "in_use":   in_use,
            })
        networks.sort(key=lambda n: -n["signal"])
        return jsonify({"networks": networks})
    except Exception as exc:
        return jsonify({"networks": [], "error": str(exc)})


@bp.route("/api/network/wifi-status", methods=["GET"])
def api_wifi_status():
    try:
        out = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"],
            capture_output=True, text=True,
        ).stdout.strip()
        active_wifi = None
        for line in out.splitlines():
            parts = line.split(":")
            if len(parts) >= 2 and parts[1] == "802-11-wireless":
                active_wifi = parts[0]
                break
        ip_out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "wlan0"],
            capture_output=True, text=True,
        ).stdout.strip()
        ip = ip_out.split()[3].split("/")[0] if ip_out else None
        return jsonify({
            "active":  active_wifi,
            "ip":      ip,
            "hotspot": active_wifi == "Hotspot" if active_wifi else False,
        })
    except Exception as exc:
        return jsonify({"active": None, "ip": None, "hotspot": False, "error": str(exc)})


@bp.route("/api/network/wifi-connect", methods=["POST"])
def api_wifi_connect():
    data     = request.get_json(force=True) or {}
    ssid     = (data.get("ssid") or "").strip()
    password = (data.get("password") or "").strip()

    if not ssid:
        return jsonify({"error": "ssid is required"}), 400
    if len(ssid) > 32:
        return jsonify({"error": "SSID must be ≤ 32 characters"}), 400
    if password and len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400

    conn_name = WIFI_PROFILE_PREFIX + ssid
    try:
        _nmcli("connection", "delete", conn_name)
    except subprocess.CalledProcessError:
        pass

    try:
        if password:
            _nmcli("connection", "add",
                   "type", "wifi",
                   "con-name", conn_name,
                   "ssid", ssid,
                   "wifi-sec.key-mgmt", "wpa-psk",
                   "wifi-sec.psk", password,
                   "connection.autoconnect", "yes",
                   "connection.autoconnect-priority", "10")
        else:
            _nmcli("connection", "add",
                   "type", "wifi",
                   "con-name", conn_name,
                   "ssid", ssid,
                   "connection.autoconnect", "yes",
                   "connection.autoconnect-priority", "10")
    except subprocess.CalledProcessError as exc:
        return jsonify({"error": exc.stderr or str(exc)}), 500

    try:
        _nmcli("connection", "up", conn_name, timeout=25)
        try:
            _nmcli("connection", "down", "Hotspot")
        except subprocess.CalledProcessError:
            pass
        ip_out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "wlan0"],
            capture_output=True, text=True,
        ).stdout.strip()
        ip = ip_out.split()[3].split("/")[0] if ip_out else "unknown"
        return jsonify({"success": True, "message": f"Connected to '{ssid}'.", "ip": ip})
    except subprocess.CalledProcessError as exc:
        return jsonify({
            "error": f"Saved profile but could not connect — network may be out of range "
                     f"or password wrong. ({exc.returncode})"
        }), 500


@bp.route("/api/network/use-hotspot", methods=["POST"])
def api_use_hotspot():
    try:
        _nmcli("connection", "up", "Hotspot", timeout=25)
        ip_out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "wlan0"],
            capture_output=True, text=True,
        ).stdout.strip()
        ip = ip_out.split()[3].split("/")[0] if ip_out else "unknown"
        return jsonify({"success": True, "message": "Switched to Hotspot.", "ip": ip})
    except subprocess.CalledProcessError as exc:
        return jsonify({"error": exc.stderr or str(exc)}), 500


@bp.route("/api/network/wifi-delete", methods=["POST"])
def api_wifi_delete():
    data    = request.get_json(force=True) or {}
    profile = (data.get("profile") or "").strip()
    if not profile.startswith(WIFI_PROFILE_PREFIX):
        return jsonify({"error": "Only irr-wifi-* profiles can be deleted from here"}), 400
    try:
        _nmcli("connection", "delete", profile)
        return jsonify({"success": True, "message": f"Deleted '{profile}'."})
    except subprocess.CalledProcessError as exc:
        return jsonify({"error": exc.stderr or str(exc)}), 500


@bp.route("/api/network/wifi-activate", methods=["POST"])
def api_wifi_activate():
    data    = request.get_json(force=True) or {}
    profile = (data.get("profile") or "").strip()
    if not profile.startswith(WIFI_PROFILE_PREFIX):
        return jsonify({"error": "Only irr-wifi-* profiles can be activated from here"}), 400
    try:
        _nmcli("connection", "up", profile, timeout=25)
        try:
            _nmcli("connection", "down", "Hotspot")
        except subprocess.CalledProcessError:
            pass
        ip_out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "wlan0"],
            capture_output=True, text=True,
        ).stdout.strip()
        ip = ip_out.split()[3].split("/")[0] if ip_out else "unknown"
        return jsonify({"success": True, "message": f"Connected via '{profile}'.", "ip": ip})
    except subprocess.CalledProcessError as exc:
        return jsonify({
            "error": f"Could not activate profile — network may be out of range. ({exc.returncode})"
        }), 500


@bp.route("/api/network/wifi-profiles", methods=["GET"])
def api_wifi_profiles():
    try:
        out   = _nmcli("-t", "-f", "NAME,TYPE", "connection", "show")
        names = [
            line.split(":")[0] for line in out.splitlines()
            if line.split(":")[0].startswith(WIFI_PROFILE_PREFIX)
            and ":802-11-wireless" in line
        ]
        return jsonify({"profiles": names})
    except Exception as exc:
        return jsonify({"profiles": [], "error": str(exc)})
