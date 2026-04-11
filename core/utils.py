"""
utils.py — Pure helper functions with no side effects.
"""
from datetime import datetime


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def parse_ts(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


def to_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in {"1", "true", "yes", "on"}:
            return True
        if s in {"0", "false", "no", "off"}:
            return False
    return False


def voltage_to_pct(voltage, dry_v, wet_v) -> float | None:
    """Two-point capacitive calibration: high voltage = dry, low voltage = wet."""
    if None in (voltage, dry_v, wet_v):
        return None
    span = float(dry_v) - float(wet_v)
    if abs(span) < 1e-9:
        return None
    return round(clamp(((float(dry_v) - float(voltage)) / span) * 100.0, 0.0, 100.0), 1)


def clamp_voltage(v: float) -> float:
    return clamp(float(v), 0.0, 6.5)
