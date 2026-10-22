#!/usr/bin/env python3
"""
Mali web backend oko garmin_sync.py - prima zahtjev s weba (Zdravlje stranica u
nagy_app), povuce svjeze podatke s Garmina i upise ih u Firestore (istu bazu koju
koristi Vue aplikacija).

Pokretanje lokalno:
    uvicorn garmin_backend:app --reload --port 8000

Deploy: Render.com (Web Service), start command:
    uvicorn garmin_backend:app --host 0.0.0.0 --port $PORT

Potrebne env varijable (postavi ih u Render dashboardu, NIKAD u git):
  GARMIN_EMAIL, GARMIN_PASSWORD   - isto kao za garmin_sync.py
  FIREBASE_CREDENTIALS_PATH       - putanja do service account JSON kljuca
                                     (lokalno: ./serviceAccountKey.json,
                                      na Renderu: putanja do "Secret File")
"""
import os
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path

import firebase_admin
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from firebase_admin import auth as fb_auth
from firebase_admin import credentials, firestore

from garmin_sync import DB_PATH, init_db, login, sync_activities, sync_day

BASE_DIR = Path(__file__).resolve().parent
ADMIN_EMAIL = "naddinko@gmail.com"

FIREBASE_CREDENTIALS_PATH = os.getenv("FIREBASE_CREDENTIALS_PATH", str(BASE_DIR / "serviceAccountKey.json"))

cred = credentials.Certificate(FIREBASE_CREDENTIALS_PATH)
firebase_admin.initialize_app(cred)
db_fs = firestore.client()

app = FastAPI(title="Garmin -> Firestore sync")

DOZVOLJENI_ORIGINI = [
    "http://localhost:5173",
    "http://localhost:5174",
    "http://localhost:5175",
    "http://localhost:5176",
    "https://nagyphotos.netlify.app",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=DOZVOLJENI_ORIGINI,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def provjeri_admina(authorization: str | None):
    """Prihvaca samo Firebase ID token prijavljenog admina (isti email kao u Vue appu)."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Nedostaje Authorization token.")
    token = authorization.split(" ", 1)[1]
    try:
        decoded = fb_auth.verify_id_token(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Neispravan ili istekao token.")
    if decoded.get("email") != ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Nemaš pristup.")


def posalji_u_firestore(conn, start: date, end: date):
    cur = conn.execute(
        "SELECT * FROM daily WHERE day BETWEEN ? AND ? ORDER BY day",
        (start.isoformat(), end.isoformat()),
    )
    cols = [c[0] for c in cur.description]
    batch = db_fs.batch()
    broj = 0
    for row in cur.fetchall():
        podaci = dict(zip(cols, row))
        dan = podaci.pop("day")
        podaci["azurirano"] = firestore.SERVER_TIMESTAMP
        ref = db_fs.collection("zdravlje-dani").document(dan)
        batch.set(ref, podaci, merge=True)
        broj += 1
    if broj:
        batch.commit()
    return broj


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/sync")
def sync(days: int = 3, authorization: str | None = Header(default=None)):
    provjeri_admina(authorization)

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    end = date.today()
    start = end - timedelta(days=max(days, 1) - 1)

    try:
        api = login()
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=502, detail=f"Prijava na Garmin nije uspjela: {e}")

    day = start
    while day <= end:
        sync_day(api, conn, day)
        day += timedelta(days=1)
        time.sleep(1)
    sync_activities(api, conn, start, end)

    broj = posalji_u_firestore(conn, start, end)
    conn.close()

    db_fs.collection("zdravlje-meta").document("zadnji-sync").set({
        "vrijeme": firestore.SERVER_TIMESTAMP,
        "dana": broj,
    })

    return {"ok": True, "dana_azurirano": broj}
