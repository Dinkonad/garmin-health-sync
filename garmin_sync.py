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
    score = ((sdto.get("sleepScores") or {}).get("overall") or {}).get("value")
    hs = (hrv or {}).get("hrvSummary") or {}

    conn.execute(
        "INSERT OR REPLACE INTO daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            d,
            stats.get("totalSteps"),
            stats.get("totalDistanceMeters"),
            stats.get("totalKilocalories"),
            stats.get("restingHeartRate"),
            stats.get("averageStressLevel"),
            stats.get("bodyBatteryHighestValue"),
            stats.get("bodyBatteryLowestValue"),
            sdto.get("sleepTimeSeconds"),
            sdto.get("deepSleepSeconds"),
            sdto.get("lightSleepSeconds"),
            sdto.get("remSleepSeconds"),
            sdto.get("awakeSleepSeconds"),
            score,
            hs.get("lastNightAvg"),
            hs.get("weeklyAvg"),
            hs.get("status"),
        ),
    )
    conn.commit()


def sync_activities(api, conn, start, end):
    acts = safe("activities", api.get_activities_by_date, start.isoformat(), end.isoformat()) or []
    for a in acts:
        conn.execute(
            "INSERT OR REPLACE INTO activities VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                a.get("activityId"),
                a.get("startTimeLocal"),
                a.get("activityName"),
                (a.get("activityType") or {}).get("typeKey"),
                a.get("distance"),
                a.get("duration"),
                a.get("calories"),
                a.get("averageHR"),
                a.get("maxHR"),
                json.dumps(a, ensure_ascii=False),
            ),
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
