"""
db.py — SQLite database connection and schema initialisation.
"""
import sqlite3

from core.config import DB_PATH, VALID_ZONES


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_db() -> None:
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sensor_data (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       DATETIME DEFAULT CURRENT_TIMESTAMP,
            temperature     REAL,
            humidity        REAL,
            soil_moisture_1 REAL,
            soil_moisture_2 REAL,
            soil_moisture_3 REAL,
            soil_moisture_4 REAL
        );
        CREATE TABLE IF NOT EXISTS valve_status (
            valve_id     INTEGER PRIMARY KEY,
            status       TEXT    NOT NULL DEFAULT 'OFF',
            last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS zone_profile (
            zone_id          INTEGER PRIMARY KEY,
            crop             TEXT,
            target_moisture  REAL,
            disabled         INTEGER NOT NULL DEFAULT 0,
            soil_baseline_id INTEGER,
            crop_target_id   INTEGER
        );
        CREATE TABLE IF NOT EXISTS control_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp  DATETIME DEFAULT CURRENT_TIMESTAMP,
            zone_id    INTEGER  NOT NULL,
            event_type TEXT     NOT NULL,
            source     TEXT     NOT NULL,
            detail     TEXT
        );
        CREATE TABLE IF NOT EXISTS soil_baseline (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL UNIQUE,
            dry_voltage REAL    NOT NULL,
            wet_voltage REAL    NOT NULL,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS crop_target (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            name           TEXT    NOT NULL UNIQUE,
            target_voltage REAL    NOT NULL,
            created_at     DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS testing_lock (
            zone_id      INTEGER PRIMARY KEY,
            locked_until DATETIME NOT NULL
        );
        CREATE TABLE IF NOT EXISTS irrigation_log (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            zone_id           INTEGER  NOT NULL,
            source            TEXT     NOT NULL DEFAULT 'manual',
            added_at          DATETIME,
            started_at        DATETIME,
            completed_at      DATETIME,
            volume_liters          REAL,
            est_duration_minutes   REAL,
            actual_duration_minutes REAL,
            flow_rate_lpm          REAL,
            initial_moisture  REAL,
            post_moisture     REAL,
            temperature       REAL,
            humidity          REAL,
            crop_target_name  TEXT,
            target_moisture   REAL,
            day_of_week       INTEGER,
            hour_of_day       INTEGER
        );
    """)

    # Seed valve rows and zone profiles once.
    conn.executemany(
        "INSERT OR IGNORE INTO valve_status (valve_id, status) VALUES (?, 'OFF')",
        [(z,) for z in VALID_ZONES],
    )
    for zone_id in VALID_ZONES:
        conn.execute(
            "INSERT OR IGNORE INTO zone_profile (zone_id) VALUES (?)",
            (zone_id,),
        )

    # Idempotent column additions for older DBs.
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(zone_profile)")}
    for col, defn in [
        ("disabled",         "INTEGER NOT NULL DEFAULT 0"),
        ("soil_baseline_id", "INTEGER"),
        ("crop_target_id",   "INTEGER"),
        ("flow_rate_lpm",    "REAL NOT NULL DEFAULT 3.0"),
        ("irr_mode",         "TEXT NOT NULL DEFAULT 'ml'"),
        ("threshold_gap",    "REAL NOT NULL DEFAULT 5.0"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE zone_profile ADD COLUMN {col} {defn}")

    irr_cols = {r["name"] for r in conn.execute("PRAGMA table_info(irrigation_log)")}
    for col, defn in [
        ("est_duration_minutes",    "REAL"),
        ("actual_duration_minutes", "REAL"),
        ("started_at",              "DATETIME"),
    ]:
        if col not in irr_cols:
            conn.execute(f"ALTER TABLE irrigation_log ADD COLUMN {col} {defn}")

    conn.commit()
    conn.close()
