"""
Module D — Batch Retrainer
============================
Causwagan Norte Irrigation System — ML Pipeline

Scheduled via Linux CRON to run once a week at 02:00 AM.

Cron entry (edit with: crontab -e):
    0 2 * * 0  /home/pi/irrigation_env/bin/python /home/pi/cron_retrain.py >> /home/pi/retrain.log 2>&1

What it does:
  1. Load training_data.csv
  2. Validate and clean rows (drop nulls / out-of-range values)
  3. Split features (Temp, Humidity, Moisture_Deficit) and target (Target_Volume)
  4. Fit a RandomForestRegressor
  5. Compute RMSE on the training set (and hold-out set if ≥50 rows)
  6. Overwrite /home/pi/irrigation_brain.pkl via joblib
  7. Append a one-line summary to /home/pi/retrain.log

Environment variables:
    IRRIGATION_BRAIN_PKL     (default /home/pi/irrigation_brain.pkl)
    TRAINING_CSV             (default /home/pi/training_data.csv)
    RETRAIN_LOG              (default /home/pi/retrain.log)
    RETRAIN_MIN_ROWS         Minimum rows required before retraining
                             (default 20)
    RF_N_ESTIMATORS          Number of trees  (default 100)
    RF_MAX_DEPTH             Max tree depth   (default None = unlimited)
    RF_RANDOM_STATE          RNG seed         (default 42)
"""

import csv
import json
import math
import os
import sys
from datetime import datetime

# ─────────────────────────────────────────────────────────────────────────────
#  Configuration
# ─────────────────────────────────────────────────────────────────────────────

BRAIN_PKL    = os.getenv("IRRIGATION_BRAIN_PKL", "/home/pi/irrigation_brain.pkl")
TRAINING_CSV = os.getenv("TRAINING_CSV",          "/home/pi/training_data.csv")
RETRAIN_LOG  = os.getenv("RETRAIN_LOG",           "/home/pi/retrain.log")
MIN_ROWS     = int(os.getenv("RETRAIN_MIN_ROWS",  "20"))

RF_N_ESTIMATORS  = int(os.getenv("RF_N_ESTIMATORS", "100"))
RF_MAX_DEPTH_STR = os.getenv("RF_MAX_DEPTH", "")    # empty string → None
RF_MAX_DEPTH     = int(RF_MAX_DEPTH_STR) if RF_MAX_DEPTH_STR.strip() else None
RF_RANDOM_STATE  = int(os.getenv("RF_RANDOM_STATE", "42"))

# Column names expected in the CSV
FEATURE_COLS = ["Temp", "Humidity", "Moisture_Deficit"]
TARGET_COL   = "Target_Volume"

# Sanity bounds for feature values — rows outside these are dropped.
BOUNDS = {
    "Temp":             (-10.0,  60.0),
    "Humidity":         (  0.0, 100.0),
    "Moisture_Deficit": (  0.0, 100.0),
    "Target_Volume":    (  0.0, 200.0),
}


# ─────────────────────────────────────────────────────────────────────────────
#  Logging helper
# ─────────────────────────────────────────────────────────────────────────────

def _log(msg: str):
    """Print to stdout and append to RETRAIN_LOG."""
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        with open(RETRAIN_LOG, "a") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  Data loading & validation
# ─────────────────────────────────────────────────────────────────────────────

def _load_and_clean_csv() -> tuple[list[list[float]], list[float]]:
    """
    Read training_data.csv, validate each row, and return:
        X — list of [Temp, Humidity, Moisture_Deficit] rows
        y — list of Target_Volume values
    Rows with missing or out-of-range values are silently dropped.
    """
    if not os.path.exists(TRAINING_CSV):
        raise FileNotFoundError(f"Training CSV not found: {TRAINING_CSV}")

    X, y = [], []
    dropped = 0

    with open(TRAINING_CSV, newline="") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            try:
                row_features = [float(raw[c]) for c in FEATURE_COLS]
                row_target   = float(raw[TARGET_COL])
            except (KeyError, ValueError, TypeError):
                dropped += 1
                continue

            # Out-of-range check
            values = dict(zip(FEATURE_COLS, row_features))
            values[TARGET_COL] = row_target
            ok = True
            for col, (lo, hi) in BOUNDS.items():
                if not (lo <= values[col] <= hi):
                    ok = False
                    break
            if not ok:
                dropped += 1
                continue

            X.append(row_features)
            y.append(row_target)

    _log(f"Loaded {len(X)} valid rows, dropped {dropped} invalid rows.")
    return X, y


# ─────────────────────────────────────────────────────────────────────────────
#  Train / evaluate
# ─────────────────────────────────────────────────────────────────────────────

def _rmse(y_true: list[float], y_pred: list[float]) -> float:
    """Root Mean Square Error."""
    n = len(y_true)
    if n == 0:
        return float("nan")
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(y_true, y_pred)) / n)


def retrain():
    """Full retraining pipeline. Raises on unrecoverable errors."""
    _log("═" * 60)
    _log("Batch retraining started")
    _log(f"  CSV     : {TRAINING_CSV}")
    _log(f"  Model   : {BRAIN_PKL}")
    _log(f"  RF config: n_estimators={RF_N_ESTIMATORS}, "
         f"max_depth={RF_MAX_DEPTH}, random_state={RF_RANDOM_STATE}")

    # ── 1. Load CSV ───────────────────────────────────────────────────────────
    X, y = _load_and_clean_csv()

    if len(X) < MIN_ROWS:
        msg = (f"Only {len(X)} valid rows — need ≥{MIN_ROWS} before retraining. "
               f"Run bootstrap_data.py to collect more observations.")
        _log(f"SKIP: {msg}")
        return {"success": False, "reason": msg, "n_rows": len(X)}

    import numpy as np
    from sklearn.ensemble import RandomForestRegressor
    from sklearn.model_selection import train_test_split
    import joblib

    X_arr = np.array(X, dtype=float)
    y_arr = np.array(y, dtype=float)

    # ── 2. Optional hold-out split (≥50 rows) ─────────────────────────────────
    if len(X) >= 50:
        X_train, X_test, y_train, y_test = train_test_split(
            X_arr, y_arr, test_size=0.2, random_state=RF_RANDOM_STATE
        )
        _log(f"  Train rows: {len(X_train)}, Test rows: {len(X_test)}")
    else:
        X_train, y_train = X_arr, y_arr
        X_test,  y_test  = None, None
        _log(f"  Train rows: {len(X_train)} (no hold-out — fewer than 50 rows)")

    # ── 3. Fit RandomForestRegressor ──────────────────────────────────────────
    model = RandomForestRegressor(
        n_estimators  = RF_N_ESTIMATORS,
        max_depth     = RF_MAX_DEPTH,
        random_state  = RF_RANDOM_STATE,
        n_jobs        = -1,    # use all CPU cores
    )
    model.fit(X_train, y_train)
    _log("  RandomForestRegressor fitted.")

    # ── 4. RMSE ───────────────────────────────────────────────────────────────
    train_preds  = model.predict(X_train).tolist()
    train_rmse   = _rmse(y_train.tolist(), train_preds)
    _log(f"  Train RMSE : {train_rmse:.4f} L")

    if X_test is not None and y_test is not None:
        test_preds = model.predict(X_test).tolist()
        test_rmse  = _rmse(y_test.tolist(), test_preds)
        _log(f"  Test  RMSE : {test_rmse:.4f} L")
    else:
        test_rmse = None

    # Feature importances
    feat_imp = dict(zip(FEATURE_COLS, model.feature_importances_.round(4).tolist()))
    _log(f"  Feature importances: {feat_imp}")

    # ── 5. Save model ─────────────────────────────────────────────────────────
    joblib.dump(model, BRAIN_PKL)
    _log(f"  Model saved → {BRAIN_PKL}")

    # ── 6. Save metadata sidecar (used by thesis_dashboard for display) ───────
    meta = {
        "trained_at":         datetime.now().isoformat(),
        "n_rows":             len(X),
        "train_rmse":         round(train_rmse, 4),
        "test_rmse":          round(test_rmse, 4) if test_rmse is not None else None,
        "feature_cols":       FEATURE_COLS,
        "target_col":         TARGET_COL,
        "n_estimators":       RF_N_ESTIMATORS,
        "max_depth":          RF_MAX_DEPTH,
        "feature_importances": feat_imp,
        "model_path":         BRAIN_PKL,
    }
    meta_path = BRAIN_PKL.replace(".pkl", "_meta.json")
    with open(meta_path, "w") as fh:
        json.dump(meta, fh, indent=2)
    _log(f"  Metadata saved → {meta_path}")

    _log("Batch retraining complete.")
    _log("═" * 60)
    return {"success": True, **meta}


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    try:
        result = retrain()
        if not result.get("success"):
            print(f"[RETRAIN] Skipped: {result.get('reason')}")
            sys.exit(2)   # non-zero but not a failure — cron will ignore
        sys.exit(0)
    except FileNotFoundError as exc:
        _log(f"ERROR: {exc}")
        sys.exit(1)
    except Exception as exc:
        _log(f"FATAL: {exc}")
        import traceback
        _log(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
