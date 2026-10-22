#!/usr/bin/env python3
"""
Garmin Connect -> SQLite sinkronizacija.

Koristenje:
  python garmin_sync.py                    # zadnja 3 dana + osvjezi report.html
  python garmin_sync.py --days 14          # zadnjih 14 dana
  python garmin_sync.py --since 2024-01-01 # povijest (backfill)
  python garmin_sync.py --html             # samo ponovno generiraj report.html
  python garmin_sync.py --export-csv       # izvezi tablice u CSV

Prijava: upisi GARMIN_EMAIL i GARMIN_PASSWORD u datoteku .env
(kopiraj .env.example). Nakon prve prijave tokeni se spremaju u
~/.garminconnect.
"""
import argparse
import csv
import json
import os
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv
from garminconnect import Garmin

from report import generate as generate_html

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DB_PATH = Path(os.getenv("GARMIN_DB", BASE_DIR / "garmin.db"))
HTML_PATH = BASE_DIR / "report.html"
TOKEN_DIR = Path(os.getenv("GARMIN_TOKENS", Path.home() / ".garminconnect"))


# ---------- Baza ----------
# Stupci u "daily" koji su dodani nakon prve verzije - dodaju se migracijom
# (ALTER TABLE) umjesto brisanja tablice, da se ne izgubi lokalna povijest.
NOVI_STUPCI_DAILY = [
    ("step_goal", "INTEGER"),
    ("active_calories", "INTEGER"),
    ("bmr_calories", "INTEGER"),
    ("floors_climbed", "INTEGER"),
    ("min_hr", "INTEGER"),
    ("max_hr", "INTEGER"),
    ("moderate_intensity_min", "INTEGER"),
    ("vigorous_intensity_min", "INTEGER"),
    ("avg_spo2", "REAL"),
    ("lowest_spo2", "REAL"),
    ("avg_respiration", "REAL"),
    ("highest_respiration", "REAL"),
    ("lowest_respiration", "REAL"),
    ("stress_duration_min", "INTEGER"),
    ("rest_stress_min", "INTEGER"),
    ("low_stress_min", "INTEGER"),
    ("medium_stress_min", "INTEGER"),
    ("high_stress_min", "INTEGER"),
    ("body_battery_charged", "INTEGER"),
    ("body_battery_drained", "INTEGER"),
    ("sleep_avg_respiration", "REAL"),
    ("sleep_avg_stress", "REAL"),
    ("sleep_deep_pct", "REAL"),
    ("sleep_light_pct", "REAL"),
    ("sleep_rem_pct", "REAL"),
    ("hrv_5min_high", "INTEGER"),
    ("hrv_feedback", "TEXT"),
]


def init_db(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS raw (
            day TEXT, kind TEXT, json TEXT,
            PRIMARY KEY (day, kind)
        );
        CREATE TABLE IF NOT EXISTS daily (
            day TEXT PRIMARY KEY,
            steps INTEGER, distance_m REAL, calories INTEGER,
            resting_hr INTEGER, avg_stress INTEGER,
            body_battery_high INTEGER, body_battery_low INTEGER,
            sleep_seconds INTEGER, deep_seconds INTEGER, light_seconds INTEGER,
            rem_seconds INTEGER, awake_seconds INTEGER, sleep_score INTEGER,
            hrv_last_night INTEGER, hrv_weekly_avg INTEGER, hrv_status TEXT
        );
        CREATE TABLE IF NOT EXISTS activities (
            activity_id INTEGER PRIMARY KEY,
            start_time TEXT, name TEXT, type TEXT,
            distance_m REAL, duration_s REAL, calories REAL,
            avg_hr REAL, max_hr REAL, json TEXT
        );
        """
    )
    for naziv, tip in [
        ("elevation_gain", "REAL"), ("avg_speed", "REAL"), ("max_speed", "REAL"),
        ("aerobic_effect", "REAL"), ("anaerobic_effect", "REAL"), ("vo2max", "REAL"),
        ("avg_power", "REAL"), ("steps", "INTEGER"),
    ]:
        try:
            conn.execute(f"ALTER TABLE activities ADD COLUMN {naziv} {tip}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    for naziv, tip in NOVI_STUPCI_DAILY:
        try:
            conn.execute(f"ALTER TABLE daily ADD COLUMN {naziv} {tip}")
        except sqlite3.OperationalError:
            pass  # stupac vec postoji
    conn.commit()


def save_raw(conn, day, kind, data):
    if data:
        conn.execute(
            "INSERT OR REPLACE INTO raw VALUES (?,?,?)",
            (day, kind, json.dumps(data, ensure_ascii=False)),
        )


# ---------- Garmin ----------
def login():
    has_tokens = TOKEN_DIR.exists() and any(TOKEN_DIR.iterdir())
    if not has_tokens and not (os.getenv("GARMIN_EMAIL") and os.getenv("GARMIN_PASSWORD")):
        raise SystemExit("Nedostaju podaci za prijavu. Kopiraj .env.example u .env "
                         "i upisi GARMIN_EMAIL i GARMIN_PASSWORD.")
    api = Garmin(
        os.getenv("GARMIN_EMAIL"),
        os.getenv("GARMIN_PASSWORD"),
        prompt_mfa=lambda: input("Garmin MFA kod: "),
    )
    api.login(str(TOKEN_DIR))  # koristi spremljene tokene ako postoje
    return api


def safe(label, fn, *args):
    """Jedan neuspjeli endpoint ne smije srusiti cijelu sinkronizaciju."""
    try:
        return fn(*args)
    except Exception as e:
        print(f"  ! {label}: {e}")
        return None


def sekunde_u_minute(s):
    return round(s / 60) if isinstance(s, (int, float)) else None


def postotak(dio, ukupno):
    if not isinstance(dio, (int, float)) or not isinstance(ukupno, (int, float)) or ukupno <= 0:
        return None
    return round((dio / ukupno) * 100, 1)


def sync_day(api, conn, day):
    d = day.isoformat()
    print(f"-> {d}")
    stats = safe("stats", api.get_stats, d)
    sleep = safe("sleep", api.get_sleep_data, d)
    hrv = safe("hrv", api.get_hrv_data, d)
    hr = safe("heart_rate", api.get_heart_rates, d)
    stress = safe("stress", api.get_stress_data, d)
    bb = safe("body_battery", api.get_body_battery, d, d)

    for kind, data in [("stats", stats), ("sleep", sleep), ("hrv", hrv),
                       ("heart_rate", hr), ("stress", stress), ("body_battery", bb)]:
        save_raw(conn, d, kind, data)

    stats = stats or {}
    sdto = (sleep or {}).get("dailySleepDTO") or {}
    sleep_scores = sdto.get("sleepScores") or {}
    score = (sleep_scores.get("overall") or {}).get("value")
    sleep_total = sdto.get("sleepTimeSeconds")
    hs = (hrv or {}).get("hrvSummary") or {}

    podaci = {
        "steps": stats.get("totalSteps"),
        "distance_m": stats.get("totalDistanceMeters"),
        "calories": stats.get("totalKilocalories"),
        "resting_hr": stats.get("restingHeartRate"),
        "avg_stress": stats.get("averageStressLevel"),
        "body_battery_high": stats.get("bodyBatteryHighestValue"),
        "body_battery_low": stats.get("bodyBatteryLowestValue"),
        "sleep_seconds": sleep_total,
        "deep_seconds": sdto.get("deepSleepSeconds"),
        "light_seconds": sdto.get("lightSleepSeconds"),
        "rem_seconds": sdto.get("remSleepSeconds"),
        "awake_seconds": sdto.get("awakeSleepSeconds"),
        "sleep_score": score,
        "hrv_last_night": hs.get("lastNightAvg"),
        "hrv_weekly_avg": hs.get("weeklyAvg"),
        "hrv_status": hs.get("status"),
        # ---- prosireno ----
        "step_goal": stats.get("dailyStepGoal"),
        "active_calories": stats.get("activeKilocalories"),
        "bmr_calories": stats.get("bmrKilocalories"),
        "floors_climbed": stats.get("floorsAscended"),
        "min_hr": stats.get("minHeartRate"),
        "max_hr": stats.get("maxHeartRate"),
        "moderate_intensity_min": stats.get("moderateIntensityMinutes"),
        "vigorous_intensity_min": stats.get("vigorousIntensityMinutes"),
        "avg_spo2": stats.get("averageSpo2"),
        "lowest_spo2": stats.get("lowestSpo2"),
        "avg_respiration": stats.get("avgWakingRespirationValue"),
        "highest_respiration": stats.get("highestRespirationValue"),
        "lowest_respiration": stats.get("lowestRespirationValue"),
        "stress_duration_min": sekunde_u_minute(stats.get("stressDuration")),
        "rest_stress_min": sekunde_u_minute(stats.get("restStressDuration")),
        "low_stress_min": sekunde_u_minute(stats.get("lowStressDuration")),
        "medium_stress_min": sekunde_u_minute(stats.get("mediumStressDuration")),
        "high_stress_min": sekunde_u_minute(stats.get("highStressDuration")),
        "body_battery_charged": stats.get("bodyBatteryChargedValue"),
        "body_battery_drained": stats.get("bodyBatteryDrainedValue"),
        "sleep_avg_respiration": sdto.get("averageRespirationValue"),
        "sleep_avg_stress": sdto.get("avgSleepStress"),
        "sleep_deep_pct": postotak(sdto.get("deepSleepSeconds"), sleep_total),
        "sleep_light_pct": postotak(sdto.get("lightSleepSeconds"), sleep_total),
        "sleep_rem_pct": postotak(sdto.get("remSleepSeconds"), sleep_total),
        "hrv_5min_high": hs.get("lastNight5MinHigh"),
        "hrv_feedback": hs.get("feedbackPhrase"),
    }

    stupci = ", ".join(podaci.keys())
    placeholderi = ", ".join("?" for _ in podaci)
    conn.execute(
        f"INSERT OR REPLACE INTO daily (day, {stupci}) VALUES (?, {placeholderi})",
        (d, *podaci.values()),
    )
    conn.commit()


def sync_activities(api, conn, start, end):
    acts = safe("activities", api.get_activities_by_date, start.isoformat(), end.isoformat()) or []
    for a in acts:
        podaci = {
            "start_time": a.get("startTimeLocal"),
            "name": a.get("activityName"),
            "type": (a.get("activityType") or {}).get("typeKey"),
            "distance_m": a.get("distance"),
            "duration_s": a.get("duration"),
            "calories": a.get("calories"),
            "avg_hr": a.get("averageHR"),
            "max_hr": a.get("maxHR"),
            "elevation_gain": a.get("elevationGain"),
            "avg_speed": a.get("averageSpeed"),
            "max_speed": a.get("maxSpeed"),
            "aerobic_effect": a.get("aerobicTrainingEffect"),
            "anaerobic_effect": a.get("anaerobicTrainingEffect"),
            "vo2max": a.get("vO2MaxValue"),
            "avg_power": a.get("avgPower"),
            "steps": a.get("steps"),
            "json": json.dumps(a, ensure_ascii=False),
        }
        stupci = ", ".join(podaci.keys())
        placeholderi = ", ".join("?" for _ in podaci)
        conn.execute(
            f"INSERT OR REPLACE INTO activities (activity_id, {stupci}) VALUES (?, {placeholderi})",
            (a.get("activityId"), *podaci.values()),
        )
    conn.commit()
    print(f"-> aktivnosti: {len(acts)}")


# ---------- CSV ----------
def export_csv(conn):
    for table in ("daily", "activities"):
        cur = conn.execute(f"SELECT * FROM {table}" if table == "daily"
                           else "SELECT activity_id,start_time,name,type,distance_m,duration_s,calories,avg_hr,max_hr FROM activities")
        cols = [c[0] for c in cur.description]
        with open(f"{table}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(cur.fetchall())
        print(f"Izvezeno: {table}.csv")


# ---------- Main ----------
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=3, help="koliko zadnjih dana povuci")
    p.add_argument("--since", help="pocetni datum YYYY-MM-DD (backfill)")
    p.add_argument("--export-csv", action="store_true", help="samo izvezi CSV i izadi")
    p.add_argument("--html", action="store_true", help="samo generiraj report.html i izadi")
    p.add_argument("--no-html", action="store_true", help="ne generiraj report.html nakon sinkronizacije")
    args = p.parse_args()

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    if args.export_csv:
        export_csv(conn)
        return

    if args.html:
        conn.close()
        print("HTML:", generate_html(DB_PATH, HTML_PATH))
        return

    end = date.today()
    start = date.fromisoformat(args.since) if args.since else end - timedelta(days=args.days - 1)

    api = login()
    day = start
    while day <= end:
        sync_day(api, conn, day)
        day += timedelta(days=1)
        time.sleep(1)  # budi fin prema Garminu, izbjegni rate limit
    sync_activities(api, conn, start, end)
    conn.close()
    if not args.no_html:
        print("HTML:", generate_html(DB_PATH, HTML_PATH))
    print("Gotovo.")


if __name__ == "__main__":
    main()
