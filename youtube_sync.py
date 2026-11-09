"""
YouTube sinkronizacija: jednom se povežeš (Google OAuth "authorization code" tok),
backend zadrži refresh token i sam povlači podatke s YouTubea u Firestore.

Tok:
  1. Vue app zove GET /youtube/povezi-url (s Firebase tokenom admina) -> dobije Google URL
  2. Korisnik odobri pristup na Googleu -> Google ga vrati na GET /youtube/callback
  3. Backend zamijeni "code" za tokene (treba client secret, zato je ovo na serveru)
     i spremi refresh token ENKRIPTIRAN u Firestore (youtube-tajne/token)
  4. POST /youtube/osvjezi (gumb u appu) ili GET /youtube/osvjezi-cron (GitHub Actions)
     povuku podatke i upišu ih u Firestore kolekcije:
        youtube-kanal/{datum}   dnevni snimak ukupnih brojki kanala (povijest raste od prvog dana)
        youtube-dnevno/{datum}  dnevni pregledi, minute gledanja, pretplatnici, lajkovi
        youtube-videa/{id}      zadnje stanje svakog videa
        youtube-status/sync     zadnja sinkronizacija, greške, je li povezano

Env varijable (Render):
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET  - iz Google Cloud -> Credentials -> OAuth client
  YOUTUBE_REDIRECT_URI                    - (opcionalno) default <render url>/youtube/callback;
                                            mora biti upisan u "Authorized redirect URIs"
  YOUTUBE_VEZA_VRIJEDI_DANA               - (opcionalno) koliko dana vrijedi veza, default 7 (Google "Testing"
                                            način). Postavi 0 ako je OAuth aplikacija objavljena (token ne istječe).
  YOUTUBE_POVRATNI_ORIGINI                - (opcionalno) zarezom odvojeni origini na koje se
                                            korisnik smije vratiti nakon prijave
"""
import base64
import hashlib
import hmac
import os
import re
import secrets
from datetime import date, datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import RedirectResponse

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
YOUTUBE_REDIRECT_URI = os.getenv(
    "YOUTUBE_REDIRECT_URI", "https://garmin-health-sync-xlyl.onrender.com/youtube/callback"
)
DOZVOLJENI_POVRATNI = [
    o.strip().rstrip("/")
    for o in os.getenv("YOUTUBE_POVRATNI_ORIGINI", "https://nagyphotos.netlify.app").split(",")
    if o.strip()
]

SCOPE_DATA = "https://www.googleapis.com/auth/youtube.readonly"
SCOPE_ANALITIKA = "https://www.googleapis.com/auth/yt-analytics.readonly"

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
DATA_API = "https://www.googleapis.com/youtube/v3"
ANALITIKA_API = "https://youtubeanalytics.googleapis.com/v2/reports"

ZAGREB = ZoneInfo("Europe/Zagreb")
MAX_STRANICA_VIDEA = 4          # 4 x 50 = zadnjih 200 videa
POCETNA_POVIJEST_DANA = 730     # prvi sync povuče do 2 godine unatrag
VEZA_VRIJEDI_DANA = int(os.getenv("YOUTUBE_VEZA_VRIJEDI_DANA", "7"))   # Google u "Testing" načinu ukida token nakon 7 dana; 0 = ne istječe
UPOZORI_DANA_PRIJE = 2          # push upozorenje otprilike toliko dana prije isteka veze
OBICNI_SYNC_DANA = 10           # analitika kasni i naknadno se ispravlja, pa uvijek povuci zadnjih 10 dana


def _danas() -> date:
    return datetime.now(ZAGREB).date()


def _povratak_dozvoljen(origin: str) -> bool:
    origin = origin.rstrip("/")
    return origin in DOZVOLJENI_POVRATNI or bool(re.fullmatch(r"http://localhost:\d+", origin))


def _fernet() -> Fernet:
    if not GOOGLE_CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="GOOGLE_CLIENT_SECRET nije postavljen na serveru.")
    # Ključ za enkripciju refresh tokena izveden iz client secreta: bez te tajne s Rendera
    # token u bazi je beskoristan, čak i ako netko dođe do Firestore dokumenta.
    kljuc = base64.urlsafe_b64encode(hashlib.sha256(("yt-token:" + GOOGLE_CLIENT_SECRET).encode()).digest())
    return Fernet(kljuc)


def _greska_iz_odgovora(r: requests.Response) -> str:
    try:
        tijelo = r.json()
        g = tijelo.get("error")
        if isinstance(g, dict):
            return g.get("message") or str(g)
        if g:
            return f"{g}: {tijelo.get('error_description', '')}".strip(": ")
    except Exception:
        pass
    return f"HTTP {r.status_code}"


def napravi_router(db_fs, provjeri_admina, cron_secret: str, posalji_push=None) -> APIRouter:
    router = APIRouter(prefix="/youtube")

    tajne = lambda: db_fs.collection("youtube-tajne")  # noqa: E731
    status_ref = lambda: db_fs.collection("youtube-status").document("sync")  # noqa: E731

    def postavi_status(**polja):
        status_ref().set(polja, merge=True)

    def javi_push(naslov: str, tijelo: str) -> bool:
        """Push na mobitel; greška pri slanju nikad ne smije srušiti sinkronizaciju."""
        if not posalji_push:
            return False
        try:
            posalji_push(naslov, tijelo)
            return True
        except Exception as e:  # noqa: BLE001
            print(f"YouTube push nije poslan: {e}")
            return False

    def javi_istek_veze():
        """Jednom po istjecanju: push 'veza je istekla' (flag se briše pri novom povezivanju)."""
        status = status_ref().get().to_dict() or {}
        postavi_status(povezano=False, istekla=True)
        if not status.get("obavijestIstek"):
            if javi_push("YouTube veza je istekla", "Otvori Društvene mreže > YouTube i klikni 'Poveži ponovno'. Podaci se nadoknade."):
                postavi_status(obavijestIstek=True)

    def provjeri_blizi_istek():
        """Jednom prije isteka: push 'veza ističe za N dana'."""
        status = status_ref().get().to_dict() or {}
        istjece_txt = status.get("istjeceU")
        if not istjece_txt or status.get("obavijestUskoro"):
            return
        try:
            istjece = datetime.fromisoformat(istjece_txt)
        except ValueError:
            return
        preostalo = istjece - datetime.now(ZAGREB)
        if timedelta(0) < preostalo <= timedelta(days=UPOZORI_DANA_PRIJE):
            dana = max(1, round(preostalo.total_seconds() / 86400))
            tekst = "sutra" if dana == 1 else f"za {dana} dana"
            if javi_push(f"YouTube veza ističe {tekst}", "Otvori Društvene mreže > YouTube i klikni 'Poveži ponovno' (traje par sekundi)."):
                postavi_status(obavijestUskoro=True)

    # ==================== POVEZIVANJE ====================
    @router.get("/povezi-url")
    def povezi_url(povratak: str, authorization: str | None = Header(default=None)):
        provjeri_admina(authorization)
        if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
            raise HTTPException(status_code=500, detail="GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET nisu postavljeni na serveru.")
        if not _povratak_dozvoljen(povratak):
            raise HTTPException(status_code=400, detail=f"Povratni origin nije dozvoljen: {povratak}")

        state = secrets.token_urlsafe(24)
        tajne().document("oauth-state").set({
            "state": state,
            "istice": (datetime.now(ZAGREB) + timedelta(minutes=10)).isoformat(),
            "povratak": povratak.rstrip("/"),
        })
        parametri = {
            "client_id": GOOGLE_CLIENT_ID,
            "redirect_uri": YOUTUBE_REDIRECT_URI,
            "response_type": "code",
            "scope": f"{SCOPE_DATA} {SCOPE_ANALITIKA}",
            "access_type": "offline",   # da dobijemo refresh token
            "prompt": "consent",        # Google refresh token šalje samo uz izričit pristanak
            "state": state,
        }
        return {"url": f"{GOOGLE_AUTH_URL}?{urlencode(parametri)}"}

    @router.get("/callback")
    def callback(code: str | None = None, state: str | None = None, error: str | None = None):
        zapis = tajne().document("oauth-state").get()
        podaci = zapis.to_dict() if zapis.exists else {}
        povratak = podaci.get("povratak") or (DOZVOLJENI_POVRATNI[0] if DOZVOLJENI_POVRATNI else "")

        def vrati(ok: bool, razlog: str = ""):
            if not povratak:
                return RedirectResponse("about:blank")
            upit = "youtube=povezano" if ok else "youtube=greska&" + urlencode({"razlog": razlog[:200]})
            return RedirectResponse(f"{povratak}/admin/drustvene-mreze?{upit}", status_code=303)

        # state je jednokratan - obriši ga odmah, što god se dalje dogodilo
        if zapis.exists:
            tajne().document("oauth-state").delete()

        if error:
            return vrati(False, f"Google: {error}")
        if not code or not state or not podaci.get("state") or not hmac.compare_digest(podaci["state"], state):
            return vrati(False, "Neispravan state (zahtjev nije započet iz aplikacije).")
        if datetime.fromisoformat(podaci["istice"]) < datetime.now(ZAGREB):
            return vrati(False, "Veza je istekla, pokušaj ponovno.")

        try:
            r = requests.post(GOOGLE_TOKEN_URL, data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": YOUTUBE_REDIRECT_URI,
                "grant_type": "authorization_code",
            }, timeout=30)
            if r.status_code != 200:
                return vrati(False, f"Zamjena koda nije uspjela: {_greska_iz_odgovora(r)}")
            tokeni = r.json()
        except requests.RequestException as e:
            return vrati(False, f"Mrežna greška: {e}")

        refresh = tokeni.get("refresh_token")
        if not refresh:
            return vrati(False, "Google nije vratio refresh token. Ukloni pristup aplikaciji na myaccount.google.com/permissions i pokušaj ponovno.")

        odobreno = (tokeni.get("scope") or "").split()
        tajne().document("token").set({
            "refresh": _fernet().encrypt(refresh.encode()).decode(),
            "povezanoU": datetime.now(ZAGREB).isoformat(),
        })
        sada_veza = datetime.now(ZAGREB)
        postavi_status(
            povezano=True,
            analitikaOdobrena=SCOPE_ANALITIKA in odobreno,
            greska="",
            povezanoU=sada_veza.isoformat(),
            istjeceU=(sada_veza + timedelta(days=VEZA_VRIJEDI_DANA)).isoformat() if VEZA_VRIJEDI_DANA > 0 else None,
            istekla=False,
            obavijestIstek=False,
            obavijestUskoro=False,
        )
        return vrati(True)

    @router.post("/odspoji")
    def odspoji(authorization: str | None = Header(default=None)):
        provjeri_admina(authorization)
        zapis = tajne().document("token").get()
        if zapis.exists:
            try:
                refresh = _fernet().decrypt(zapis.to_dict()["refresh"].encode()).decode()
                requests.post(GOOGLE_REVOKE_URL, params={"token": refresh}, timeout=15)
            except Exception:
                pass  # ako opoziv ne uspije, svejedno brišemo lokalno
            tajne().document("token").delete()
        postavi_status(povezano=False, greska="")
        return {"ok": True}

    # ==================== SINKRONIZACIJA ====================
    def pristupni_token() -> str:
        zapis = tajne().document("token").get()
        if not zapis.exists:
            raise HTTPException(status_code=409, detail="YouTube nije povezan.")
        try:
            refresh = _fernet().decrypt(zapis.to_dict()["refresh"].encode()).decode()
        except InvalidToken:
            postavi_status(povezano=False, greska="Spremljena veza se ne može pročitati (promijenjen client secret?). Poveži ponovno.")
            raise HTTPException(status_code=409, detail="Spremljeni token se ne može dešifrirati - poveži ponovno.")

        r = requests.post(GOOGLE_TOKEN_URL, data={
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        }, timeout=30)
        if r.status_code != 200:
            poruka = _greska_iz_odgovora(r)
            if "invalid_grant" in poruka:
                postavi_status(greska="Veza s YouTubeom je istekla ili opozvana - poveži ponovno.")
                javi_istek_veze()
                raise HTTPException(status_code=409, detail="Veza je istekla ili opozvana - poveži ponovno.")
            raise HTTPException(status_code=502, detail=f"Obnova tokena nije uspjela: {poruka}")
        return r.json()["access_token"]

    def google_get(url: str, token: str, params: dict | None = None) -> dict:
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=40)
        if r.status_code != 200:
            raise RuntimeError(f"{r.status_code}: {_greska_iz_odgovora(r)}")
        return r.json()

    def spremi_u_serijama(kolekcija: str, dokumenti: list[tuple[str, dict]]):
        for i in range(0, len(dokumenti), 400):
            batch = db_fs.batch()
            for doc_id, podaci in dokumenti[i:i + 400]:
                batch.set(db_fs.collection(kolekcija).document(doc_id), podaci)
            batch.commit()

    def sinkroniziraj(puno: bool) -> dict:
        token = pristupni_token()
        sada = datetime.now(ZAGREB)
        danas = sada.date()
        status = status_ref().get().to_dict() or {}

        try:
            # ---- kanal ----
            kanali = google_get(f"{DATA_API}/channels", token,
                                {"part": "snippet,statistics,contentDetails", "mine": "true"}).get("items", [])
            if not kanali:
                raise RuntimeError("Ovaj Google račun nema YouTube kanal (za brand račun odaberi taj račun pri povezivanju).")
            kanal = kanali[0]
            stat = kanal.get("statistics", {})
            spremi_u_serijama("youtube-kanal", [(danas.isoformat(), {
                "datum": danas.isoformat(),
                "snippet": {"title": kanal.get("snippet", {}).get("title", "")},
                "statistics": {
                    "viewCount": stat.get("viewCount", "0"),
                    "subscriberCount": stat.get("subscriberCount", "0"),
                    "videoCount": stat.get("videoCount", "0"),
                    "hiddenSubscriberCount": bool(stat.get("hiddenSubscriberCount", False)),
                },
                "azurirano": sada.isoformat(),
            })])

            # ---- videa ----
            uploads = kanal.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
            ids: list[str] = []
            if uploads:
                stranica = None
                for _ in range(MAX_STRANICA_VIDEA):
                    parametri = {"part": "contentDetails", "playlistId": uploads, "maxResults": 50}
                    if stranica:
                        parametri["pageToken"] = stranica
                    p = google_get(f"{DATA_API}/playlistItems", token, parametri)
                    ids += [s["contentDetails"]["videoId"] for s in p.get("items", [])]
                    stranica = p.get("nextPageToken")
                    if not stranica:
                        break

            videa_doc: list[tuple[str, dict]] = []
            for i in range(0, len(ids), 50):
                v = google_get(f"{DATA_API}/videos", token, {"part": "snippet,statistics", "id": ",".join(ids[i:i + 50])})
                for stavka in v.get("items", []):
                    sn = stavka.get("snippet", {})
                    st = stavka.get("statistics", {})
                    videa_doc.append((stavka["id"], {
                        "id": stavka["id"],
                        "snippet": {
                            "title": sn.get("title", ""),
                            "publishedAt": sn.get("publishedAt", ""),
                            "thumbnails": {"default": {"url": sn.get("thumbnails", {}).get("default", {}).get("url", "")}},
                        },
                        "statistics": {
                            "viewCount": st.get("viewCount", "0"),
                            "likeCount": st.get("likeCount", "0"),
                            "commentCount": st.get("commentCount", "0"),
                        },
                        "azurirano": sada.isoformat(),
                    }))
            spremi_u_serijama("youtube-videa", videa_doc)

            # ---- dnevna analitika (greška ovdje ne ruši ostatak) ----
            broj_dana = 0
            analitika_greska = ""
            if status.get("analitikaOdobrena", True):
                try:
                    uvodi_povijest = puno or not status.get("povijestPovucena")
                    if uvodi_povijest:
                        objavljen = kanal.get("snippet", {}).get("publishedAt", "")[:10]
                        od = max(date.fromisoformat(objavljen) if objavljen else danas - timedelta(days=POCETNA_POVIJEST_DANA),
                                 danas - timedelta(days=POCETNA_POVIJEST_DANA))
                    else:
                        od = danas - timedelta(days=OBICNI_SYNC_DANA)
                    izvjestaj = google_get(ANALITIKA_API, token, {
                        "ids": "channel==MINE",
                        "startDate": od.isoformat(),
                        "endDate": danas.isoformat(),
                        "metrics": "views,estimatedMinutesWatched,subscribersGained,subscribersLost,likes",
                        "dimensions": "day",
                        "sort": "day",
                    })
                    imena = [c["name"] for c in izvjestaj.get("columnHeaders", [])]
                    dani = []
                    for red in izvjestaj.get("rows", []):
                        zapis = dict(zip(imena, red))
                        dani.append((zapis["day"], zapis))
                    spremi_u_serijama("youtube-dnevno", dani)
                    broj_dana = len(dani)
                except Exception as e:  # noqa: BLE001
                    analitika_greska = str(e)
            else:
                analitika_greska = "Dozvola za analitiku nije odobrena."

            postavi_status(
                povezano=True,
                zadnjaSinkronizacija=sada.isoformat(),
                uspjeh=True,
                greska="",
                analitikaGreska=analitika_greska,
                brojDana=broj_dana,
                brojVidea=len(videa_doc),
                povijestPovucena=bool(status.get("povijestPovucena")) or (not analitika_greska),
            )
            return {"ok": True, "dana": broj_dana, "videa": len(videa_doc), "analitikaGreska": analitika_greska}
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            postavi_status(uspjeh=False, greska=str(e), zadnjaSinkronizacija=sada.isoformat())
            raise HTTPException(status_code=502, detail=str(e))

    @router.post("/osvjezi")
    def osvjezi(puno: bool = False, authorization: str | None = Header(default=None)):
        provjeri_admina(authorization)
        return sinkroniziraj(puno)

    @router.get("/osvjezi-cron")
    def osvjezi_cron(x_cron_secret: str | None = Header(default=None)):
        if cron_secret and x_cron_secret != cron_secret:
            raise HTTPException(status_code=401, detail="Neispravna tajna.")
        if not tajne().document("token").get().exists:
            return {"ok": False, "razlog": "YouTube nije povezan."}
        provjeri_blizi_istek()
        try:
            return sinkroniziraj(False)
        except HTTPException as e:
            if e.status_code == 409:
                # očekivano: veza je istekla (push je već poslan) - ne tretiramo kao kvar servera
                return {"ok": False, "istekla": True, "razlog": e.detail}
            raise

    return router
