"""
Linkovi: spremanje zanimljivih videa (Instagram, Facebook, TikTok, YouTube...) JEDNIM DODIROM.

Tok:
  1. Na mobitelu u Instagramu/Facebooku/TikToku/YouTubeu: Podijeli -> "Spremi u Nagy" (iOS Prečac).
     Prečac pošalje link na  POST /linkovi/spremi  zajedno s tajnim ključem.
  2. Backend iz poslanog teksta izvuče link, očisti ga od praćenja (igsh, utm...), prepozna platformu,
     provjeri je li već spremljen, dohvati naslov / autora / sličicu i sve upiše u Firestore
     kolekciju "linkovi" (ista koju čita stranica Linkovi u appu).
  3. Sličica se SPREMA u dokument (malena JPEG kopija), jer adrese slika na Instagramu/TikToku
     nakon par dana prestanu raditi.

Ista ruta služi i za ručno dodavanje u appu (tamo se šalje Firebase token admina umjesto ključa).

Ključ prečaca: nasumičan niz koji backend sam napravi i spremi u Firestore "linkovi-tajne/precac".
Vidiš ga (i možeš ga promijeniti) u appu: Linkovi > "Spremanje s mobitela".

Dohvaćanje tuđih stranica ide kroz "sigurni_get": samo http/https na portovima 80/443, samo javne
IP adrese (nikad localhost / privatna mreža), ograničeno vrijeme i veličina odgovora.
"""
import asyncio
import base64
import hashlib
import hmac
import io
import ipaddress
import json
import re
import secrets
import socket
import time
from collections import deque
from html.parser import HTMLParser
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse

import requests
from fastapi import APIRouter, Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool

try:  # Pillow je opcionalan: bez njega se sličica sprema u izvornoj veličini (ako je dovoljno mala)
    from PIL import Image, ImageOps
except Exception:  # noqa: BLE001
    Image = None
    ImageOps = None

UA_PREVIEW = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
UA_PREGLEDNIK = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"
)

MAX_HTML_BAJTOVA = 700_000
MAX_SLIKA_BAJTOVA = 6_000_000
MAX_SIROVA_SLIKA_BAJTOVA = 150_000   # bez Pillowa: veće slike ne spremamo u dokument
SLIKA_NAJVECA_STRANICA = 520
TIMEOUT = (4, 8)
MAX_PRESKOKA = 5

PLATFORME = {
    "youtube-video": "YouTube",
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "facebook": "Facebook",
    "x": "X",
    "pinterest": "Pinterest",
    "ostalo": "Web",
}
ZAMJENSKI_NASLOV = {
    "youtube-video": "YouTube video",
    "instagram": "Instagram objava",
    "tiktok": "TikTok video",
    "facebook": "Facebook video",
    "x": "X objava",
    "pinterest": "Pinterest pin",
}

# parametri koji samo prate odakle je link kliknut - nisu dio sadržaja
PARAMETRI_PRACENJA = {
    "igsh", "igshid", "fbclid", "si", "feature", "ref_src", "ref_url", "gclid", "mibextid",
    "is_from_webapp", "sender_device", "_t", "_r", "share_app_id", "share_item_id", "share_link_id",
    "sec_user_id", "social_sharing", "ab_channel",
}
KRATKE_DOMENE = ("vm.tiktok.com", "vt.tiktok.com", "fb.watch", "pin.it", "t.co", "lnkd.in", "bit.ly")

_neuspjeli_kljucevi = deque(maxlen=50)   # vremena zadnjih pogrešnih ključeva (zaštita od pogađanja)


def _host_je(host: str, *domene: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in domene)


# ---------------------------------------------------------------- link: izvlačenje, čišćenje, ključ

def izvuci_url(tekst) -> str | None:
    """Prvi link iz teksta (dijeljenje s mobitela često šalje 'Pogledaj ovo https://...')."""
    if isinstance(tekst, (list, tuple)):
        tekst = " ".join(str(t) for t in tekst)
    tekst = str(tekst or "").strip()
    if not tekst:
        return None
    m = re.search(r"https?://[^\s<>\"']+", tekst, flags=re.IGNORECASE)
    if m:
        url = m.group(0)
    elif re.fullmatch(r"(www\.)?[a-z0-9-]+(\.[a-z0-9-]+)+(/\S*)?", tekst, flags=re.IGNORECASE):
        url = "https://" + tekst   # npr. "instagram.com/reel/abc"
    else:
        return None
    return url.rstrip(".,;:!?)]}'\"")


def ocisti_url(url: str) -> str:
    p = urlparse(url.strip())
    host = (p.hostname or "").lower()
    port = f":{p.port}" if p.port and p.port not in (80, 443) else ""
    upit = [
        (k, v) for k, v in parse_qsl(p.query, keep_blank_values=False)
        if k.lower() not in PARAMETRI_PRACENJA and not k.lower().startswith("utm_")
    ]
    return urlunparse((p.scheme.lower() or "https", host + port, p.path, "", urlencode(upit), ""))


def prepoznaj_platformu(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if _host_je(host, "youtube.com", "youtu.be", "youtube-nocookie.com"):
        return "youtube-video"
    if _host_je(host, "instagram.com", "instagr.am"):
        return "instagram"
    if _host_je(host, "tiktok.com"):
        return "tiktok"
    if _host_je(host, "facebook.com", "fb.com", "fb.watch", "fb.me"):
        return "facebook"
    if _host_je(host, "x.com", "twitter.com", "t.co"):
        return "x"
    if _host_je(host, "pinterest.com", "pin.it") or re.match(r"^([a-z]{2}\.)?pinterest\.[a-z.]+$", host):
        return "pinterest"
    return "ostalo"


def youtube_id(url: str) -> str | None:
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if host == "youtu.be":
        kandidat = p.path.strip("/").split("/")[0]
    else:
        m = re.match(r"^/(?:shorts|embed|live|v)/([A-Za-z0-9_-]{11})", p.path)
        kandidat = m.group(1) if m else dict(parse_qsl(p.query)).get("v", "")
    return kandidat if re.fullmatch(r"[A-Za-z0-9_-]{11}", kandidat or "") else None


def kljuc_linka(url: str) -> str:
    """Isti video = isti ključ, bez obzira na oblik linka (za otkrivanje duplikata)."""
    tip = prepoznaj_platformu(url)
    p = urlparse(url)
    if tip == "youtube-video":
        vid = youtube_id(url)
        if vid:
            return f"youtube:{vid}"
    if tip == "instagram":
        m = re.search(r"/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", p.path)
        if m:
            return f"instagram:{m.group(1)}"
    if tip == "tiktok":
        m = re.search(r"/video/(\d+)", p.path)
        if m:
            return f"tiktok:{m.group(1)}"
    host = re.sub(r"^(www|m|mobile)\.", "", (p.hostname or "").lower())
    upit = urlencode(sorted(parse_qsl(p.query)))
    return f"{host}{p.path.rstrip('/')}" + (f"?{upit}" if upit else "")


# ---------------------------------------------------------------- sigurno dohvaćanje tuđih stranica

def _javna_adresa(host: str) -> bool:
    try:
        informacije = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    if not informacije:
        return False
    for info in informacije:
        ip = ipaddress.ip_address(info[4][0])
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if not ip.is_global:
            return False
    return True


def provjeri_odrediste(url: str):
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("Nedozvoljena adresa.")
    if p.port not in (None, 80, 443):
        raise ValueError("Nedozvoljen port.")
    if not _javna_adresa(p.hostname):
        raise ValueError("Adresa nije javna.")


def sigurni_get(url: str, ua: str, max_bajtova: int, accept: str = "*/*"):
    """GET s ručnim praćenjem preusmjeravanja (svaki korak se provjerava). Vraća (konačni_url, status, headers, bajtovi)."""
    trenutni = url
    for _ in range(MAX_PRESKOKA + 1):
        provjeri_odrediste(trenutni)
        r = requests.get(
            trenutni,
            headers={"User-Agent": ua, "Accept": accept, "Accept-Language": "hr,en;q=0.8"},
            timeout=TIMEOUT,
            allow_redirects=False,
            stream=True,
        )
        try:
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                trenutni = urljoin(trenutni, r.headers["location"])
                continue
            podaci = bytearray()
            for dio in r.iter_content(16384):
                podaci.extend(dio)
                if len(podaci) >= max_bajtova:
                    break
            return trenutni, r.status_code, r.headers, bytes(podaci)
        finally:
            r.close()
    raise ValueError("Previše preusmjeravanja.")


def razrijesi_kratki_link(url: str) -> str:
    """vm.tiktok.com, fb.watch, pin.it... -> pravi link (da duplikati i sličice rade)."""
    p = urlparse(url)
    host = (p.hostname or "").lower()
    kratki = host in KRATKE_DOMENE or (
        _host_je(host, "instagram.com", "facebook.com") and re.search(r"/share(/|$)", p.path)
    )
    if not kratki:
        return url
    try:
        konacni, _status, _h, _d = sigurni_get(url, UA_PREGLEDNIK, 1)
    except Exception:  # noqa: BLE001
        return url
    konacni_p = urlparse(konacni)
    # prijava/zid prijave nije pravi sadržaj - zadrži izvorni link
    if re.search(r"/(login|accounts/login|checkpoint)", konacni_p.path, flags=re.IGNORECASE):
        return url
    return ocisti_url(konacni)


# ---------------------------------------------------------------- pregled: naslov, autor, sličica

class _MetaParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self._u_naslovu = False

    def handle_starttag(self, tag, attrs):
        if tag == "meta":
            a = {k.lower(): v for k, v in attrs if k}
            kljuc = (a.get("property") or a.get("name") or "").lower()
            vrijednost = a.get("content")
            if kljuc and vrijednost and kljuc not in self.meta:
                self.meta[kljuc] = vrijednost.strip()
        elif tag == "title":
            self._u_naslovu = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._u_naslovu = False

    def handle_data(self, data):
        if self._u_naslovu and len(self.title) < 400:
            self.title += data


def procitaj_meta(html: str) -> dict:
    parser = _MetaParser()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001
        pass
    m = parser.meta
    return {
        "naslov": m.get("og:title") or m.get("twitter:title") or parser.title.strip(),
        "opis": m.get("og:description") or m.get("twitter:description") or m.get("description") or "",
        "slika": m.get("og:image") or m.get("og:image:url") or m.get("twitter:image") or "",
        "stranica": m.get("og:site_name") or "",
    }


def skrati(tekst, najvise: int) -> str:
    tekst = re.sub(r"\s+", " ", str(tekst or "")).strip()
    return tekst if len(tekst) <= najvise else tekst[: najvise - 1].rstrip() + "…"


GENERICKI_NASLOVI = {"instagram", "facebook", "tiktok", "x", "twitter", "log in", "login", "log into facebook",
                     "login • instagram", "facebook – log in or sign up", "tiktok - make your day", "just a moment..."}


def _ocisti_naslov(tip: str, naslov: str, opis: str) -> tuple[str, str]:
    """Vraća (naslov, autor). Instagram šalje 'Ime on Instagram: "opis"'."""
    naslov = (naslov or "").strip()
    autor = ""
    if naslov.lower() in GENERICKI_NASLOVI:
        naslov = ""
    if tip == "instagram":
        m = re.match(r'^(.+?) on Instagram:\s*["“”]?(.*?)["“”]?\s*$', naslov, flags=re.DOTALL)
        if m:
            autor, naslov = m.group(1).strip(), m.group(2).strip()
        elif not naslov and opis:
            m = re.search(r'-\s*([^-:]+?) on [A-Z][a-z]+ \d+, \d{4}:\s*["“”]?(.*?)["“”]?\s*$', opis, flags=re.DOTALL)
            if m:
                autor, naslov = m.group(1).strip(), m.group(2).strip()
    if tip == "facebook" and naslov.endswith(" | Facebook"):
        naslov = naslov[: -len(" | Facebook")]
    return skrati(naslov, 200), skrati(autor, 80)


def _json_get(url: str):
    try:
        _k, status, headers, podaci = sigurni_get(url, UA_PREGLEDNIK, 200_000, accept="application/json")
        if status != 200:
            return None
        return json.loads(podaci.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None


def dohvati_pregled(url: str, tip: str) -> dict:
    """{naslov, autor, opis, slika_url}. Greške se gutaju: link se uvijek spremi, pregled je bonus."""
    naslov = autor = opis = slika_url = ""

    if tip == "youtube-video":
        o = _json_get(f"https://www.youtube.com/oembed?format=json&url={quote(url, safe='')}")
        if o:
            naslov, autor = o.get("title", ""), o.get("author_name", "")
        vid = youtube_id(url)
        if vid:
            slika_url = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"   # stabilna adresa, ne treba kopija
    elif tip == "tiktok":
        o = _json_get(f"https://www.tiktok.com/oembed?url={quote(url, safe='')}")
        if o:
            naslov, autor, slika_url = o.get("title", ""), o.get("author_name", ""), o.get("thumbnail_url", "")

    if not naslov or not slika_url:
        # Open Graph oznake (isto što čitaju poruke kad pošalješ link). Prvo kao "preglednik poveznica", pa kao mobitel.
        for ua in (UA_PREVIEW, UA_PREGLEDNIK):
            try:
                _k, status, headers, podaci = sigurni_get(url, ua, MAX_HTML_BAJTOVA, accept="text/html,*/*")
            except Exception:  # noqa: BLE001
                continue
            if status >= 400 and not podaci:
                continue
            kodiranje = "utf-8"
            m = re.search(r"charset=([\w-]+)", headers.get("content-type", ""), flags=re.IGNORECASE)
            if m:
                kodiranje = m.group(1)
            try:
                html = podaci.decode(kodiranje, "replace")
            except LookupError:
                html = podaci.decode("utf-8", "replace")
            meta = procitaj_meta(html)
            n, a = _ocisti_naslov(tip, meta["naslov"], meta["opis"])
            naslov = naslov or n
            autor = autor or a
            opis = opis or skrati(meta["opis"], 400)
            if not slika_url and meta["slika"]:
                slika_url = urljoin(url, meta["slika"])
            if naslov and slika_url:
                break

    naslov = skrati(naslov, 200)
    return {"naslov": naslov, "autor": skrati(autor, 80), "opis": skrati(opis, 400), "slika_url": slika_url}


def slika_u_podatke(slika_url: str) -> str | None:
    """Skine sličicu i vrati je kao malenu JPEG 'data:' adresu (ili None ako ne ide)."""
    if not slika_url or not slika_url.lower().startswith(("http://", "https://")):
        return None
    try:
        _k, status, headers, podaci = sigurni_get(slika_url, UA_PREGLEDNIK, MAX_SLIKA_BAJTOVA, accept="image/*")
    except Exception:  # noqa: BLE001
        return None
    tip_sadrzaja = headers.get("content-type", "").split(";")[0].strip().lower()
    if status != 200 or not podaci or not tip_sadrzaja.startswith("image/"):
        return None

    if Image is not None:
        try:
            slika = Image.open(io.BytesIO(podaci))
            slika = ImageOps.exif_transpose(slika).convert("RGB")
            slika.thumbnail((SLIKA_NAJVECA_STRANICA, SLIKA_NAJVECA_STRANICA))
            izlaz = io.BytesIO()
            slika.save(izlaz, "JPEG", quality=72, optimize=True)
            return "data:image/jpeg;base64," + base64.b64encode(izlaz.getvalue()).decode("ascii")
        except Exception:  # noqa: BLE001
            pass
    if len(podaci) <= MAX_SIROVA_SLIKA_BAJTOVA and tip_sadrzaja in ("image/jpeg", "image/png", "image/webp"):
        return f"data:{tip_sadrzaja};base64," + base64.b64encode(podaci).decode("ascii")
    return None


# ---------------------------------------------------------------- spremanje u Firestore

def ocisti_oznake(oznake) -> list[str]:
    if isinstance(oznake, str):
        oznake = re.split(r"[,;\n]", oznake)
    rezultat: list[str] = []
    for o in oznake if isinstance(oznake, (list, tuple)) else []:
        o = re.sub(r"\s+", " ", str(o)).strip().lstrip("#")[:30]
        if o and o.lower() not in (x.lower() for x in rezultat):
            rezultat.append(o)
    return rezultat[:12]


def spremi_link(db_fs, tekst, naslov=None, biljeska=None, oznake=None, izvor="app") -> dict:
    from firebase_admin import firestore as fb_firestore  # lokalno: modul se može testirati i bez Firebasea

    url = izvuci_url(tekst)
    if not url:
        raise HTTPException(status_code=400, detail="U poslanom tekstu nema linka.")
    url = ocisti_url(url)
    if urlparse(url).scheme not in ("http", "https") or not urlparse(url).hostname:
        raise HTTPException(status_code=400, detail="Neispravan link.")
    url = razrijesi_kratki_link(url)

    tip = prepoznaj_platformu(url)
    kljuc = kljuc_linka(url)

    kolekcija = db_fs.collection("linkovi")
    for polje, vrijednost in (("kljuc", kljuc), ("url", url)):
        postojeci = list(kolekcija.where(polje, "==", vrijednost).limit(1).stream())
        if postojeci:
            stari = postojeci[0].to_dict() or {}
            return {
                "ok": True, "vec_postoji": True, "id": postojeci[0].id, "tip": tip,
                "naziv": stari.get("naziv", ""),
                "poruka": "Već je spremljeno" + (f": {skrati(stari.get('naziv', ''), 60)}" if stari.get("naziv") else "."),
            }

    pregled = {"naslov": "", "autor": "", "opis": "", "slika_url": ""}
    try:
        pregled = dohvati_pregled(url, tip)
    except Exception as e:  # noqa: BLE001
        print(f"Linkovi: pregled nije dohvaćen ({url}): {e}")

    slika = None
    if pregled["slika_url"]:
        if tip == "youtube-video":
            slika = pregled["slika_url"]
        else:
            slika = slika_u_podatke(pregled["slika_url"])

    host = re.sub(r"^www\.", "", urlparse(url).hostname or "")
    ima_pregled = bool(pregled["naslov"] or slika)
    naziv = skrati(naslov, 200) if naslov and str(naslov).strip() else (pregled["naslov"] or ZAMJENSKI_NASLOV.get(tip) or host)

    dokument = {
        "url": url,
        "naziv": naziv,
        "tip": tip,
        "slika": slika,
        "autor": pregled["autor"],
        "opis": pregled["opis"],
        "biljeska": skrati(biljeska, 2000) if biljeska else "",
        "oznake": ocisti_oznake(oznake),
        "kljuc": kljuc,
        "izvor": izvor,
        "pregled": ima_pregled,
        "datum": fb_firestore.SERVER_TIMESTAMP,
        "izmijenjeno": fb_firestore.SERVER_TIMESTAMP,
    }
    ref = kolekcija.document()
    ref.set(dokument)

    platforma = PLATFORME.get(tip, "Web")
    if ima_pregled:
        poruka = f"Spremljeno ({platforma}): {skrati(naziv, 60)}"
    else:
        poruka = f"Spremljeno ({platforma}), ali bez sličice i naslova. Dodaj ih u appu ako želiš."
    return {"ok": True, "vec_postoji": False, "id": ref.id, "tip": tip, "naziv": naziv, "ima_sliku": bool(slika), "poruka": poruka}


# ---------------------------------------------------------------- ruter

def napravi_router(db_fs, provjeri_admina) -> APIRouter:
    router = APIRouter(prefix="/linkovi")
    tajna_ref = lambda: db_fs.collection("linkovi-tajne").document("precac")  # noqa: E731

    def dohvati_kljuc(napravi: bool = True, novi: bool = False) -> str | None:
        if not novi:
            kljuc = (tajna_ref().get().to_dict() or {}).get("kljuc")
            if kljuc:
                return kljuc
            if not napravi:
                return None
        kljuc = secrets.token_urlsafe(24)
        tajna_ref().set({"kljuc": kljuc, "napravljen": int(time.time())})
        return kljuc

    def kljuc_vrijedi(predan: str | None) -> bool:
        pravi = dohvati_kljuc(napravi=False)
        return bool(predan and pravi and hmac.compare_digest(str(predan).encode(), pravi.encode()))

    @router.post("/spremi")
    async def spremi(
        request: Request,
        authorization: str | None = Header(default=None),
        x_link_token: str | None = Header(default=None),
    ):
        sirovo = await request.body()
        tijelo = None
        try:
            tijelo = json.loads(sirovo.decode("utf-8", "replace")) if sirovo else None
        except ValueError:
            tijelo = None
        if not isinstance(tijelo, dict):
            tekst = sirovo.decode("utf-8", "replace")
            if "application/x-www-form-urlencoded" in request.headers.get("content-type", "").lower():
                tijelo = {k: v[0] for k, v in parse_qs(tekst).items()}
            else:
                tijelo = {"url": tekst}   # običan tekst (ili link) bez JSON-a

        if authorization:
            await run_in_threadpool(provjeri_admina, authorization)
            izvor = "app"
        else:
            predan = x_link_token or tijelo.get("kljuc")
            sada = time.time()
            while _neuspjeli_kljucevi and sada - _neuspjeli_kljucevi[0] > 600:
                _neuspjeli_kljucevi.popleft()
            if len(_neuspjeli_kljucevi) >= 20:
                raise HTTPException(status_code=429, detail="Previše pogrešnih pokušaja. Pokušaj za 10 minuta.")
            if not await run_in_threadpool(kljuc_vrijedi, predan):
                _neuspjeli_kljucevi.append(sada)
                await asyncio.sleep(0.5)
                raise HTTPException(status_code=401, detail="Neispravan ključ prečaca.")
            izvor = "precac"

        tekst_s_linkom = tijelo.get("url") or tijelo.get("tekst") or tijelo.get("link") or ""
        return await run_in_threadpool(
            spremi_link, db_fs, tekst_s_linkom, tijelo.get("naziv"), tijelo.get("biljeska"), tijelo.get("oznake"), izvor
        )

    @router.get("/precac")
    def precac_kljuc(authorization: str | None = Header(default=None)):
        provjeri_admina(authorization)
        return {"kljuc": dohvati_kljuc()}

    @router.post("/precac/novi")
    def precac_novi_kljuc(authorization: str | None = Header(default=None)):
        provjeri_admina(authorization)
        return {"kljuc": dohvati_kljuc(novi=True)}

    return router
