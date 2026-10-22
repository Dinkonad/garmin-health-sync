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
import base64
import json
import os
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path

import firebase_admin
import webauthn
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from firebase_admin import auth as fb_auth
from firebase_admin import credentials, firestore
from pywebpush import WebPushException, webpush
from webauthn.helpers import (
    parse_authentication_credential_json,
    parse_registration_credential_json,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from garmin_sync import DB_PATH, init_db, login, sync_activities, sync_day

BASE_DIR = Path(__file__).resolve().parent
ADMIN_EMAIL = "naddinko@gmail.com"
WEBAUTHN_RP_ID = os.getenv("WEBAUTHN_RP_ID", "nagyphotos.netlify.app")
WEBAUTHN_RP_NAME = "Nagy Photos"

FIREBASE_CREDENTIALS_PATH = os.getenv("FIREBASE_CREDENTIALS_PATH", str(BASE_DIR / "serviceAccountKey.json"))
VAPID_PRIVATE_KEY_PATH = os.getenv("VAPID_PRIVATE_KEY_PATH", str(BASE_DIR / "vapid_private.pem"))
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", f"mailto:{ADMIN_EMAIL}")
CRON_SECRET = os.getenv("CRON_SECRET", "")

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


def posalji_aktivnosti_u_firestore(conn, start: date, end: date):
    cur = conn.execute(
        "SELECT * FROM activities WHERE start_time >= ? AND start_time < ? ORDER BY start_time",
        (start.isoformat(), (end + timedelta(days=1)).isoformat()),
    )
    cols = [c[0] for c in cur.description]
    broj = 0
    for row in cur.fetchall():
        podaci = dict(zip(cols, row))
        activity_id = podaci.pop("activity_id")
        podaci.pop("json", None)  # prevelik/nepotreban u Firestoreu
        podaci["azurirano"] = firestore.SERVER_TIMESTAMP
        db_fs.collection("zdravlje-aktivnosti").document(str(activity_id)).set(podaci, merge=True)
        broj += 1
    return broj


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
    broj = 0
    broj_aktivnosti = 0

    try:
        api = login()

        pocetak_komada = start
        day = start
        while day <= end:
            sync_day(api, conn, day)
            if posalji_dan_u_firestore(conn, day.isoformat()):
                broj += 1
            day += timedelta(days=1)
            time.sleep(1)

            # aktivnosti se salju u komadima od ~30 dana tijekom sinkronizacije (ne tek
            # na kraju) - tako da duga povlacenja povijesti ne izgube sve ako se negdje
            # prekinu (npr. Render ugasi pozadinski proces usred posla)
            kraj_komada = min(day - timedelta(days=1), end)
            if (kraj_komada - pocetak_komada).days >= 29 or day > end:
                sync_activities(api, conn, pocetak_komada, kraj_komada)
                broj_aktivnosti += posalji_aktivnosti_u_firestore(conn, pocetak_komada, kraj_komada)
                pocetak_komada = day
                meta_ref.set({"dana": broj, "aktivnosti": broj_aktivnosti}, merge=True)

        meta_ref.set({
            "vrijeme": firestore.SERVER_TIMESTAMP,
            "dana": broj,
            "aktivnosti": broj_aktivnosti,
            "uTijeku": False,
            "greska": firestore.DELETE_FIELD,
        }, merge=True)
    except Exception as e:
        # Bilo koja greška usred sinkronizacije (mreža, Garmin API, itd.) - uvijek
        # resetiraj "uTijeku" da se status ne zaglavi zauvijek.
        meta_ref.set({
            "uTijeku": False,
            "greska": str(e),
            "vrijeme": firestore.SERVER_TIMESTAMP,
            "dana": broj,
            "aktivnosti": broj_aktivnosti,
        }, merge=True)
    finally:
        conn.close()


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


@app.post("/sync/reset")
def sync_reset(authorization: str | None = Header(default=None)):
    """Ručni izlaz iz nužde ako je status ostao zaglavljen na 'u tijeku' - npr.
    zato što je Render ugasio proces (redeploy) usred sinkronizacije."""
    provjeri_admina(authorization)
    db_fs.collection("zdravlje-meta").document("zadnji-sync").set(
        {"uTijeku": False, "greska": "Ručno resetirano."}, merge=True
    )
    return {"ok": True}


# ==================== FACE ID (WebAuthn) ====================
# Prava WebAuthn registracija/prijava - backend čuva javni ključ svakog uređaja i
# provjerava kriptografski potpis, baš kao svaka ozbiljna "Prijavi se Face ID-om"
# mogućnost. Nakon uspješne potvrde izdaje se prava Firebase prijava (custom token).

def b64url_decode(s: str) -> bytes:
    dopuna = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + dopuna)


def b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def spremi_izazov(challenge: bytes):
    db_fs.collection("security").document("faceid-izazov").set({
        "izazov": b64url_encode(challenge),
        "vrijeme": firestore.SERVER_TIMESTAMP,
    })


def dohvati_izazov() -> bytes:
    snap = db_fs.collection("security").document("faceid-izazov").get()
    if not snap.exists:
        raise HTTPException(status_code=400, detail="Nema aktivnog izazova - pokušaj ponovno.")
    return b64url_decode(snap.to_dict()["izazov"])


def dohvati_credentiale() -> list:
    snap = db_fs.collection("security").document("webauthn-credentials").get()
    return (snap.to_dict() or {}).get("credentials", []) if snap.exists else []


@app.get("/faceid/register-options")
def faceid_register_options(authorization: str | None = Header(default=None)):
    provjeri_admina(authorization)
    user = fb_auth.get_user_by_email(ADMIN_EMAIL)
    postojeci = dohvati_credentiale()

    opcije = webauthn.generate_registration_options(
        rp_id=WEBAUTHN_RP_ID,
        rp_name=WEBAUTHN_RP_NAME,
        user_name=ADMIN_EMAIL,
        user_id=user.uid.encode(),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=b64url_decode(c["id"])) for c in postojeci
        ],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )
    spremi_izazov(opcije.challenge)
    return json.loads(webauthn.options_to_json(opcije))


@app.post("/faceid/register-verify")
async def faceid_register_verify(request: Request, authorization: str | None = Header(default=None)):
    provjeri_admina(authorization)
    tijelo = await request.json()
    expected_challenge = dohvati_izazov()

    try:
        credential = parse_registration_credential_json(tijelo["credential"])
        rezultat = webauthn.verify_registration_response(
            credential=credential,
            expected_challenge=expected_challenge,
            expected_rp_id=WEBAUTHN_RP_ID,
            expected_origin=DOZVOLJENI_ORIGINI,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Registracija nije uspjela: {e}")

    zapis = {
        "id": b64url_encode(rezultat.credential_id),
        "javniKljuc": b64url_encode(rezultat.credential_public_key),
        "brojPotpisa": rezultat.sign_count,
        "naziv": tijelo.get("naziv") or "Uređaj",
    }
    postojeci = dohvati_credentiale()
    postojeci.append(zapis)
    db_fs.collection("security").document("webauthn-credentials").set({"credentials": postojeci}, merge=True)

    return {"ok": True}


@app.get("/faceid/login-options")
def faceid_login_options():
    postojeci = dohvati_credentiale()
    if not postojeci:
        raise HTTPException(status_code=404, detail="Face ID nije registriran ni na jednom uređaju.")

    opcije = webauthn.generate_authentication_options(
        rp_id=WEBAUTHN_RP_ID,
        allow_credentials=[PublicKeyCredentialDescriptor(id=b64url_decode(c["id"])) for c in postojeci],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    spremi_izazov(opcije.challenge)
    return json.loads(webauthn.options_to_json(opcije))


@app.post("/faceid/login-verify")
async def faceid_login_verify(request: Request):
    tijelo = await request.json()
    expected_challenge = dohvati_izazov()

    postojeci = dohvati_credentiale()
    poslani_id = tijelo["credential"]["id"]
    zapis = next((c for c in postojeci if c["id"] == poslani_id), None)
    if not zapis:
        raise HTTPException(status_code=401, detail="Nepoznat Face ID uređaj.")

    try:
        credential = parse_authentication_credential_json(tijelo["credential"])
        rezultat = webauthn.verify_authentication_response(
            credential=credential,
            expected_challenge=expected_challenge,
            expected_rp_id=WEBAUTHN_RP_ID,
            expected_origin=DOZVOLJENI_ORIGINI,
            credential_public_key=b64url_decode(zapis["javniKljuc"]),
            credential_current_sign_count=zapis.get("brojPotpisa", 0),
        )
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Face ID potvrda nije uspjela: {e}")

    azurirani = [
        {**c, "brojPotpisa": rezultat.new_sign_count} if c["id"] == poslani_id else c
        for c in postojeci
    ]
    db_fs.collection("security").document("webauthn-credentials").set({"credentials": azurirani})

    user = fb_auth.get_user_by_email(ADMIN_EMAIL)
    token = fb_auth.create_custom_token(user.uid)
    return {"token": token.decode() if isinstance(token, bytes) else token}


# ==================== PUSH NOTIFIKACIJE (Web Push) ====================
# Pretplate (po uređaju) sprema sam Vue app direktno u Firestore
# security/push-subscriptions (isti obrazac kao webauthn-credentials).
# Backend samo čita tu listu i šalje push preko standardnog, besplatnog
# Web Push protokola (VAPID) - bez ikakvog plaćenog servisa.

def dohvati_push_pretplate() -> list:
    snap = db_fs.collection("security").document("push-subscriptions").get()
    return (snap.to_dict() or {}).get("pretplate", []) if snap.exists else []


def ukloni_push_pretplatu(endpoint: str):
    trenutne = dohvati_push_pretplate()
    filtrirane = [p for p in trenutne if p.get("endpoint") != endpoint]
    db_fs.collection("security").document("push-subscriptions").set({"pretplate": filtrirane}, merge=True)


def posalji_push_svima(naslov: str, tijelo: str) -> int:
    if not Path(VAPID_PRIVATE_KEY_PATH).is_file():
        raise RuntimeError(
            f"VAPID privatni ključ nije pronađen na serveru (očekivana putanja: {VAPID_PRIVATE_KEY_PATH}). "
            "Provjeri Secret File i VAPID_PRIVATE_KEY_PATH na Renderu."
        )

    poslano = 0
    for pretplata in dohvati_push_pretplate():
        try:
            webpush(
                subscription_info=pretplata,
                data=json.dumps({"naslov": naslov, "tijelo": tijelo}),
                vapid_private_key=VAPID_PRIVATE_KEY_PATH,
                vapid_claims={"sub": VAPID_SUBJECT},
            )
            poslano += 1
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                ukloni_push_pretplatu(pretplata.get("endpoint"))
            else:
                print(f"Push greška: {e}")
        except Exception as e:
            print(f"Push greška (neočekivano): {e}")
    return poslano


@app.get("/push/provjeri-kilometrazu")
def provjeri_kilometrazu(x_cron_secret: str | None = Header(default=None)):
    """Poziva se jednom dnevno (GitHub Actions cron). Ako je blizu kraja mjeseca
    i još nema upisa kilometraže za tekući mjesec, pošalje podsjetnik."""
    if CRON_SECRET and x_cron_secret != CRON_SECRET:
        raise HTTPException(status_code=401, detail="Neispravna tajna.")

    danas = date.today()
    if danas.day < 27:
        return {"ok": True, "poslano": False, "razlog": "nije još 27. u mjesecu"}

    mjesec_str = f"{danas.year}-{danas.month:02d}"
    upisi = list(
        db_fs.collection("auto")
        .where("tip", "==", "ocitanje-km")
        .where("mjesec", "==", mjesec_str)
        .limit(1)
        .stream()
    )
    if upisi:
        return {"ok": True, "poslano": False, "razlog": "već upisano"}

    nazivi_mjeseci = [
        "siječanj", "veljača", "ožujak", "travanj", "svibanj", "lipanj",
        "srpanj", "kolovoz", "rujan", "listopad", "studeni", "prosinac",
    ]
    mjesec_citljivo = f"{nazivi_mjeseci[danas.month - 1]} {danas.year}"

    try:
        broj = posalji_push_svima(
            "Upiši kilometražu",
            f"Još nisi upisao stanje kilometara za {mjesec_citljivo}. Otvori Auto prije kraja mjeseca.",
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "poslano": broj > 0, "broj_uredjaja": broj}


@app.post("/push/test")
def push_test(authorization: str | None = Header(default=None)):
    """Ručno testiranje push notifikacija iz app-a - privremeni endpoint."""
    provjeri_admina(authorization)
    try:
        broj = posalji_push_svima("Test notifikacija", "Ako ovo vidiš, push notifikacije rade!")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "broj_uredjaja": broj}
