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
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
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


def posalji_dan_u_firestore(conn, dan: str):
    """Pošalje jedan dan iz SQLite u Firestore odmah - tako se napredak sprema
    postupno, čak i ako dugo povlačenje povijesti negdje usput zapne."""
    cur = conn.execute("SELECT * FROM daily WHERE day = ?", (dan,))
    cols = [c[0] for c in cur.description]
    row = cur.fetchone()
    if not row:
        return False
    podaci = dict(zip(cols, row))
    podaci.pop("day")
    podaci["dan"] = dan
    podaci["azurirano"] = firestore.SERVER_TIMESTAMP
    db_fs.collection("zdravlje-dani").document(dan).set(podaci, merge=True)
    return True


def obavi_sync(start: date, end: date):
    meta_ref = db_fs.collection("zdravlje-meta").document("zadnji-sync")
    meta_ref.set({
        "uTijeku": True,
        "pocetak": firestore.SERVER_TIMESTAMP,
        "od": start.isoformat(),
        "do": end.isoformat(),
    }, merge=True)

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    try:
        api = login()
    except Exception as e:
        conn.close()
        meta_ref.set({"uTijeku": False, "greska": str(e), "vrijeme": firestore.SERVER_TIMESTAMP}, merge=True)
        return

    broj = 0
    day = start
    while day <= end:
        sync_day(api, conn, day)
        if posalji_dan_u_firestore(conn, day.isoformat()):
            broj += 1
        day += timedelta(days=1)
        time.sleep(1)

    sync_activities(api, conn, start, end)
    conn.close()

    meta_ref.set({
        "vrijeme": firestore.SERVER_TIMESTAMP,
        "dana": broj,
        "uTijeku": False,
        "greska": firestore.DELETE_FIELD,
    }, merge=True)


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/sync")
def sync(background_tasks: BackgroundTasks, days: int = 3, since: str | None = None,
          authorization: str | None = Header(default=None)):
    provjeri_admina(authorization)

    end = date.today()
    start = date.fromisoformat(since) if since else end - timedelta(days=max(days, 1) - 1)
    if start > end:
        raise HTTPException(status_code=400, detail="'since' mora biti prije danas.")

    background_tasks.add_task(obavi_sync, start, end)

    return {"ok": True, "pokrenuto": True, "od": start.isoformat(), "do": end.isoformat()}
