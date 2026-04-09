import sqlite3

def create_database():
    conn = sqlite3.connect('/home/pi/irrigation_data.db')
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sensor_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            temperature REAL,
            humidity REAL,
            soil_moisture_1 REAL,
            soil_moisture_2 REAL,
            soil_moisture_3 REAL,
            soil_moisture_4 REAL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS valve_status (
            valve_id INTEGER PRIMARY KEY,
            status TEXT,
            last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS zone_profile (
            zone_id INTEGER PRIMARY KEY,
            crop TEXT NOT NULL DEFAULT 'Corn',
            target_moisture REAL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS control_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            zone_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            source TEXT NOT NULL,
            detail TEXT
        )
    ''')

    cursor.execute("INSERT OR IGNORE INTO valve_status (valve_id, status) VALUES (1, 'OFF'), (2, 'OFF'), (3, 'OFF'), (4, 'OFF')")
    cursor.execute("INSERT OR IGNORE INTO zone_profile (zone_id, crop, target_moisture) VALUES (1, 'Corn', 30), (2, 'Corn', 30), (3, 'Corn', 30), (4, 'Corn', 30)")

    conn.commit()
    conn.close()
    print("[SUCCESS] Database initialized for 4 zones and 4 soil probes.")

if __name__ == '__main__':
    create_database()