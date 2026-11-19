"""
Galerije za klijente: fotograf uploada fotke, klijent otvara SVOJ link, gleda i preuzima.

SIGURNOSNI MODEL (najvažnije):
  * Cloudflare R2 bucket je PRIVATAN. Nikad nije javan. Fotke se vide samo preko privremenih
    potpisanih linkova koje izdaje OVAJ backend (slike 6 h, preuzimanje 5 min).
  * Klijent nema račun i ne dira Firestore ni R2. Sve što vidi dolazi kroz /javno/... rute, a one
    vrate samo podatke jedne galerije i samo ako: token pogađa (128 bita), galerija je objavljena,
    nije istekla i (ako je postavljen PIN) klijent ima valjanu dozvolu.
  * Sve /galerije/... rute (upload, brisanje, postavke) traže Firebase token vlasnika (provjeri_admina).
  * Fotke se u R2 spremaju u 3 oblika: original (privatno), srednja i mala WebP verzija za prikaz.
  * Za svaku nepostojeću / istekao / neobjavljenu galeriju vraća se ISTA greška 404, da se ne može
    saznati postoji li token.

Ključevi u R2 (u istom privatnom bucketu):
  g/{gid}/o/{id}.{ext}   original
  g/{gid}/m/{id}.webp    srednja (prikaz, ~2048 px)
  g/{gid}/t/{id}.webp    mala (mreža, ~480 px)
  g/{gid}/manifest.json  popis fotki (da se pri svakom otvaranju ne čita stotine zapisa iz Firestorea)
  z/{gid}/{hash}.zip     složeni ZIP-ovi (u R2 postavi pravilo: briši z/ nakon 7 dana)

Env varijable (Render): R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import zipfile
from collections import defaultdict, deque
from datetime import date, datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import RedirectResponse

IZVORNI_EXT = {"jpg", "jpeg", "png", "webp", "heic", "heif", "tif", "tiff"}
RAW_EXT = {"cr2", "cr3", "nef", "arw", "dng", "orf", "rw2", "raf", "srw", "pef"}
DOZVOLJENI_EXT = IZVORNI_EXT | RAW_EXT
TIP_SADRZAJA = {
    "jpg": "image/jpeg", "png": "image/png", "webp": "image/webp", "heic": "image/heic", "heif": "image/heif",
    "tif": "image/tiff",
}
MAX_ORIGINAL_BAJTOVA = 500 * 1024 * 1024
MAX_ZIP_BAJTOVA = 10 * 1024 * 1024 * 1024
SLIKE_VRIJEME = 6 * 3600          # važenje linka slike (s)
PREUZIMANJE_VRIJEME = 300         # važenje linka preuzimanja (s)
PRISTUP_VRIJEME = 12 * 3600       # važenje dozvole nakon unosa PIN-a (s)
DIO_VELICINA = 8 * 1024 * 1024    # veličina dijela pri slaganju ZIP-a (R2 traži najmanje 5 MiB)

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
ID_RE = re.compile(r"^[a-f0-9]{12,32}$")
BOJA_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
REF_RE = re.compile(r"^[A-Za-z0-9_-]{1,24}$")


# ---------------------------------------------------------------- pomoćno

def sada_ts() -> float:
    return time.time()


def pozadina_ime(ime: str) -> str:
    """Siguran naziv datoteke za preuzimanje (ASCII + UTF-8 oblik)."""
    ime = re.sub(r"[\r\n\"\\/]", "_", str(ime or "datoteka")).strip() or "datoteka"
    ascii_ime = re.sub(r"[^A-Za-z0-9._ -]", "_", ime)
    return f"attachment; filename=\"{ascii_ime}\"; filename*=UTF-8''{quote(ime)}"


class Ogranicivac:
    """Jednostavno ograničenje broja zahtjeva (u memoriji; backend radi kao jedna instanca)."""

    def __init__(self):
        self._dogadaji = defaultdict(deque)
        self._brava = threading.Lock()

    def provjeri(self, kljuc: str, najvise: int, prozor_s: int, sada=None):
        sada = sada if sada is not None else sada_ts()
        with self._brava:
            d = self._dogadaji[kljuc]
            while d and sada - d[0] > prozor_s:
                d.popleft()
            if len(d) >= najvise:
                raise HTTPException(status_code=429, detail="Previše zahtjeva. Pokušaj za koju minutu.")
            d.append(sada)


def klijentski_ip(request: Request) -> str:
    # iza Rendera: zadnja adresa u X-Forwarded-For dodaje sam proxy (prva može biti lažna)
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[-1].strip()
    return request.client.host if request.client else "nepoznato"


def uredaj_iz_ua(ua: str) -> str:
    return "mobitel" if re.search(r"Mobi|Android|iPhone|iPad", ua or "", re.I) else "racunalo"


# ---------------------------------------------------------------- pohrana (R2 preko S3 API-ja)

class Pohrana:
    """Tanki omot oko S3-kompatibilnog klijenta (boto3). Klijent se predaje izvana pa se lako testira bez mreže."""

    def __init__(self, klijent, bucket: str):
        self.s3 = klijent
        self.bucket = bucket

    # potpisani linkovi
    def potpisi_put(self, kljuc: str, tip: str, istice_s: int = 3600) -> str:
        return self.s3.generate_presigned_url(
            "put_object", Params={"Bucket": self.bucket, "Key": kljuc, "ContentType": tip}, ExpiresIn=istice_s, HttpMethod="PUT"
        )

    def potpisi_get(self, kljuc: str, istice_s: int, naziv_preuzimanja: str | None = None) -> str:
        params = {"Bucket": self.bucket, "Key": kljuc}
        if naziv_preuzimanja:
            params["ResponseContentDisposition"] = pozadina_ime(naziv_preuzimanja)
        return self.s3.generate_presigned_url("get_object", Params=params, ExpiresIn=istice_s)

    # čitanje / pisanje
    def head(self, kljuc: str):
        try:
            return self.s3.head_object(Bucket=self.bucket, Key=kljuc)
        except Exception as e:  # noqa: BLE001
            kod = getattr(e, "response", {}).get("Error", {}).get("Code", "") if hasattr(e, "response") else ""
            if kod in ("404", "NoSuchKey", "NotFound") or "404" in str(e) or "Not Found" in str(e):
                return None
            raise

    def ucitaj_json(self, kljuc: str, zadano):
        try:
            odgovor = self.s3.get_object(Bucket=self.bucket, Key=kljuc)
        except Exception as e:  # noqa: BLE001
            if "NoSuchKey" in str(e) or "404" in str(e):
                return zadano
            raise
        return json.loads(odgovor["Body"].read().decode("utf-8"))

    def spremi_json(self, kljuc: str, objekt):
        self.s3.put_object(Bucket=self.bucket, Key=kljuc, Body=json.dumps(objekt, ensure_ascii=False).encode("utf-8"),
                           ContentType="application/json", CacheControl="no-store")

    def dijelovi(self, kljuc: str, velicina=1024 * 1024):
        tijelo = self.s3.get_object(Bucket=self.bucket, Key=kljuc)["Body"]
        yield from tijelo.iter_chunks(velicina)

    def obrisi_kljuceve(self, kljucevi: list[str]):
        for i in range(0, len(kljucevi), 1000):
            dio = [{"Key": k} for k in kljucevi[i:i + 1000]]
            if dio:
                self.s3.delete_objects(Bucket=self.bucket, Delete={"Objects": dio, "Quiet": True})

    def obrisi_prefiks(self, prefiks: str):
        token = None
        while True:
            argumenti = {"Bucket": self.bucket, "Prefix": prefiks}
            if token:
                argumenti["ContinuationToken"] = token
            o = self.s3.list_objects_v2(**argumenti)
            self.obrisi_kljuceve([x["Key"] for x in o.get("Contents", [])])
            if not o.get("IsTruncated"):
                break
            token = o.get("NextContinuationToken")


class PisacMultipart:
    """Datoteka u koju se piše dio po dio; šalje se u R2 kao multipart upload (memorija ostaje mala)."""

    def __init__(self, pohrana: Pohrana, kljuc: str, tip: str = "application/zip", velicina_dijela=DIO_VELICINA):
        self.p, self.kljuc, self.tip, self.dio = pohrana, kljuc, tip, velicina_dijela
        self.spremnik = bytearray()
        self.upload_id = None
        self.dijelovi = []
        self.ukupno = 0

    def write(self, podaci):
        self.spremnik += podaci
        self.ukupno += len(podaci)
        while len(self.spremnik) >= self.dio:
            self._posalji(bytes(self.spremnik[: self.dio]))
            del self.spremnik[: self.dio]
        return len(podaci)

    def flush(self):
        pass

    def _posalji(self, bajtovi: bytes):
        if self.upload_id is None:
            self.upload_id = self.p.s3.create_multipart_upload(Bucket=self.p.bucket, Key=self.kljuc, ContentType=self.tip)["UploadId"]
        broj = len(self.dijelovi) + 1
        o = self.p.s3.upload_part(Bucket=self.p.bucket, Key=self.kljuc, UploadId=self.upload_id, PartNumber=broj, Body=bajtovi)
        self.dijelovi.append({"ETag": o["ETag"], "PartNumber": broj})

    def zavrsi(self):
        if self.upload_id is None:   # sve stane u jedan dio
            self.p.s3.put_object(Bucket=self.p.bucket, Key=self.kljuc, Body=bytes(self.spremnik), ContentType=self.tip)
            return
        if self.spremnik:
            self._posalji(bytes(self.spremnik))
        self.p.s3.complete_multipart_upload(Bucket=self.p.bucket, Key=self.kljuc, UploadId=self.upload_id,
                                            MultipartUpload={"Parts": self.dijelovi})

    def prekini(self):
        if self.upload_id is not None:
            try:
                self.p.s3.abort_multipart_upload(Bucket=self.p.bucket, Key=self.kljuc, UploadId=self.upload_id)
            except Exception:  # noqa: BLE001
                pass


def napravi_pohranu() -> Pohrana | None:
    """Pravi vezu na R2 iz env varijabli. Vraća None ako nisu postavljene (rute tada javljaju 503)."""
    racun = os.getenv("R2_ACCOUNT_ID", "").strip()
    kljuc = os.getenv("R2_ACCESS_KEY_ID", "").strip()
    tajna = os.getenv("R2_SECRET_ACCESS_KEY", "").strip()
    bucket = os.getenv("R2_BUCKET", "").strip()
    if not (racun and kljuc and tajna and bucket):
        return None
    import boto3  # lokalni uvoz: modul se može testirati i bez boto3
    from botocore.config import Config

    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{racun}.r2.cloudflarestorage.com",
        aws_access_key_id=kljuc,
        aws_secret_access_key=tajna,
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"}),
    )
    return Pohrana(s3, bucket)


# ---------------------------------------------------------------- ZIP (bez učitavanja svega u memoriju)

def slozi_zip(pohrana: Pohrana, gid: str, fotke: list[dict], kljuc_zip: str, napredak=None):
    """Čita originale iz R2 jedan po jedan i piše ZIP (bez kompresije) izravno u R2 u dijelovima."""
    pisac = PisacMultipart(pohrana, kljuc_zip)
    koristena = set()
    try:
        with zipfile.ZipFile(pisac, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            for i, f in enumerate(fotke, start=1):
                osnova = re.sub(r"[\\/:*?\"<>|\r\n]", "_", f.get("n") or f["i"])
                ime = osnova
                n = 2
                while ime.lower() in koristena:           # ista imena datoteka ne smiju se pregaziti
                    stem, tocka, ext = osnova.rpartition(".")
                    ime = f"{stem} ({n}).{ext}" if tocka else f"{osnova} ({n})"
                    n += 1
                koristena.add(ime.lower())
                info = zipfile.ZipInfo(ime, date_time=time.localtime(sada_ts())[:6])
                info.compress_type = zipfile.ZIP_STORED
                with zf.open(info, "w", force_zip64=True) as izlaz:
                    for dio in pohrana.dijelovi(f"g/{gid}/o/{f['i']}.{f['e']}"):
                        izlaz.write(dio)
                if napredak:
                    napredak(i, len(fotke))
        pisac.zavrsi()
    except Exception:
        pisac.prekini()
        raise


# ---------------------------------------------------------------- ruter

def napravi_router(db_fs, provjeri_admina, pohrana_tvornica=napravi_pohranu) -> APIRouter:
    router = APIRouter()
    ogranicivac = Ogranicivac()
    stanje = {"pohrana": None, "tajna": None}
    brave = defaultdict(threading.Lock)           # po galeriji: manifest se piše jedan po jedan
    predmemorija_galerija = {}                    # token -> (vrijeme, gid, podaci)
    predmemorija_manifesta = {}                   # gid -> (vrijeme, manifest)
    poslovi_zip = {}
    zip_semafor = threading.Semaphore(1)
    viđeni_posjeti = {}                           # (gid, posjetitelj) -> zadnji pregled

    # ---------- ovisnosti
    def pohrana() -> Pohrana:
        if stanje["pohrana"] is None:
            stanje["pohrana"] = pohrana_tvornica()
        if stanje["pohrana"] is None:
            raise HTTPException(status_code=503, detail="Pohrana (R2) nije postavljena na serveru.")
        return stanje["pohrana"]

    def tajna() -> bytes:
        if stanje["tajna"] is None:
            ref = db_fs.collection("galerije-tajne").document("pristup")
            podaci = ref.get().to_dict() or {}
            if not podaci.get("kljuc"):
                podaci = {"kljuc": secrets.token_hex(32)}
                ref.set(podaci)
            stanje["tajna"] = podaci["kljuc"].encode()
        return stanje["tajna"]

    def fb():
        from firebase_admin import firestore as fb_firestore

        return fb_firestore

    def kolekcija():
        return db_fs.collection("galerije")

    # ---------- dozvola pristupa (nakon PIN-a)
    def izdaj_pristup(gid: str) -> str:
        sadrzaj = base64.urlsafe_b64encode(json.dumps({"g": gid, "e": int(sada_ts()) + PRISTUP_VRIJEME}).encode()).decode().rstrip("=")
        potpis = hmac.new(tajna(), sadrzaj.encode(), hashlib.sha256).hexdigest()
        return f"{sadrzaj}.{potpis}"

    def pristup_vrijedi(zapis: str | None, gid: str) -> bool:
        try:
            sadrzaj, potpis = (zapis or "").split(".", 1)
            ocekivano = hmac.new(tajna(), sadrzaj.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(potpis, ocekivano):
                return False
            podaci = json.loads(base64.urlsafe_b64decode(sadrzaj + "=" * (-len(sadrzaj) % 4)))
            return podaci.get("g") == gid and int(podaci.get("e", 0)) > sada_ts()
        except Exception:  # noqa: BLE001
            return False

    def hash_pina(pin: str, sol: bytes) -> str:
        return hashlib.scrypt(pin.encode(), salt=sol, n=2 ** 14, r=8, p=1, dklen=32).hex()

    # ---------- galerija po tokenu
    def nepostojeca():
        return HTTPException(status_code=404, detail="Galerija nije pronađena.")

    def galerija_po_tokenu(token: str):
        if not TOKEN_RE.match(token or ""):
            raise nepostojeca()
        sada = sada_ts()
        u = predmemorija_galerija.get(token)
        if u and sada - u[0] < 20:
            gid, g = u[1], u[2]
        else:
            nadeno = list(kolekcija().where("token", "==", token).limit(1).stream())
            if not nadeno:
                raise nepostojeca()
            gid, g = nadeno[0].id, nadeno[0].to_dict() or {}
            predmemorija_galerija[token] = (sada, gid, g)
        if not g.get("objavljena"):
            raise nepostojeca()
        rok = g.get("rok")
        if rok:
            try:
                if date.fromisoformat(str(rok)[:10]) < date.today():
                    raise nepostojeca()
            except ValueError:
                pass
        return gid, g

    def provjeri_pristup(gid: str, g: dict, x_pristup):
        if g.get("pinHash") and not pristup_vrijedi(x_pristup, gid):
            raise HTTPException(status_code=401, detail="Potreban je PIN.")

    def manifest_od(gid: str, osvjezi=False) -> dict:
        sada = sada_ts()
        u = predmemorija_manifesta.get(gid)
        if u and not osvjezi and sada - u[0] < 60:
            return u[1]
        m = pohrana().ucitaj_json(f"g/{gid}/manifest.json", {"verzija": 0, "fotke": []})
        predmemorija_manifesta[gid] = (sada, m)
        return m

    def spremi_manifest(gid: str, m: dict):
        m["verzija"] = int(m.get("verzija", 0)) + 1
        pohrana().spremi_json(f"g/{gid}/manifest.json", m)
        predmemorija_manifesta[gid] = (sada_ts(), m)

    def urlovi_slika(gid: str, m: dict, ids, vrsta: str):
        po_id = {f["i"]: f for f in m["fotke"]}
        izlaz = {}
        for i in ids:
            if i in po_id:
                izlaz[i] = pohrana().potpisi_get(f"g/{gid}/{vrsta}/{i}.webp", SLIKE_VRIJEME)
        return izlaz

    def kompaktno(f: dict) -> dict:
        return {k: f.get(k) for k in ("i", "n", "w", "h", "c", "t", "s", "d")}

    # ---------- statistika
    def zabiljezi(gid: str, g: dict, request: Request, vrsta: str, ref: str | None = None, fid: str | None = None):
        """Brojači na dokumentu galerije. Ne smije nikad srušiti zahtjev."""
        try:
            f = fb()
            posjetitelj = hmac.new(tajna(), f"{klijentski_ip(request)}|{request.headers.get('user-agent', '')}".encode(), hashlib.sha256).hexdigest()[:16]
            promjene = {}
            if vrsta == "pregled":
                zadnji = viđeni_posjeti.get((gid, posjetitelj), 0)
                if sada_ts() - zadnji < 1800:        # isti posjetitelj u 30 min = isti pregled
                    return
                viđeni_posjeti[(gid, posjetitelj)] = sada_ts()
                if len(viđeni_posjeti) > 5000:
                    viđeni_posjeti.clear()
                promjene["statistika.pregleda"] = f.Increment(1)
                promjene["statistika.zadnjiPregled"] = f.SERVER_TIMESTAMP
                promjene[f"statistika.uredaji.{uredaj_iz_ua(request.headers.get('user-agent', ''))}"] = f.Increment(1)
                if ref and REF_RE.match(ref):
                    promjene[f"statistika.refovi.{ref}"] = f.Increment(1)
                try:
                    kolekcija().document(gid).collection("posjetitelji").document(posjetitelj).create({"prvi": f.SERVER_TIMESTAMP})
                    promjene["statistika.jedinstvenih"] = f.Increment(1)
                except Exception:  # već postoji
                    pass
            elif vrsta == "preuzimanje":
                promjene["statistika.preuzimanja"] = f.Increment(1)
                promjene["statistika.zadnjePreuzimanje"] = f.SERVER_TIMESTAMP
                if fid:
                    promjene[f"fotkaPreuzimanja.{fid}"] = f.Increment(1)
            elif vrsta == "zip":
                promjene["statistika.zipova"] = f.Increment(1)
                promjene["statistika.zadnjePreuzimanje"] = f.SERVER_TIMESTAMP
            if promjene:
                kolekcija().document(gid).update(promjene)
        except Exception as e:  # noqa: BLE001
            print(f"Galerije: statistika nije zapisana: {e}")

    # ================================================================ ADMIN
    def admin(authorization):
        provjeri_admina(authorization)

    def galerija_admin(gid: str) -> dict:
        d = kolekcija().document(gid).get()
        g = d.to_dict() if getattr(d, "exists", True) else None
        if not g:
            raise HTTPException(status_code=404, detail="Galerija ne postoji.")
        return g

    def novi_token() -> str:
        return secrets.token_urlsafe(16)          # 128 bita

    @router.post("/galerije")
    def napravi_galeriju(tijelo: dict, authorization: str | None = Header(default=None)):
        admin(authorization)
        f = fb()
        naziv = str(tijelo.get("naziv", "")).strip()[:120]
        if not naziv:
            raise HTTPException(status_code=400, detail="Upiši naziv galerije.")
        ref = kolekcija().document()
        token = novi_token()
        ref.set({
            "naziv": naziv, "klijent": str(tijelo.get("klijent", "")).strip()[:120],
            "datum": str(tijelo.get("datum", ""))[:10], "instagram": re.sub(r"[^A-Za-z0-9._]", "", str(tijelo.get("instagram", "")))[:40],
            "poruka": str(tijelo.get("poruka", ""))[:400], "token": token, "objavljena": False, "preuzimanje": "otvoreno",
            "rok": None, "pinHash": None, "pinSol": None, "naslovnaId": None, "brojFotki": 0, "velicinaUkupno": 0, "verzija": 0,
            "statistika": {"pregleda": 0, "jedinstvenih": 0, "preuzimanja": 0, "zipova": 0},
            "stvoreno": f.SERVER_TIMESTAMP, "izmijenjeno": f.SERVER_TIMESTAMP,
        })
        return {"id": ref.id, "token": token}

    @router.post("/galerije/{gid}/novi-token")
    def promijeni_token(gid: str, authorization: str | None = Header(default=None)):
        admin(authorization)
        galerija_admin(gid)
        token = novi_token()
        kolekcija().document(gid).update({"token": token})
        predmemorija_galerija.clear()
        return {"token": token}

    @router.post("/galerije/{gid}/pin")
    def postavi_pin(gid: str, tijelo: dict, authorization: str | None = Header(default=None)):
        admin(authorization)
        galerija_admin(gid)
        pin = tijelo.get("pin")
        if pin in (None, ""):
            kolekcija().document(gid).update({"pinHash": None, "pinSol": None})
        else:
            pin = str(pin)
            if not 4 <= len(pin) <= 12:
                raise HTTPException(status_code=400, detail="PIN mora imati 4 do 12 znakova.")
            sol = secrets.token_bytes(16)
            kolekcija().document(gid).update({"pinHash": hash_pina(pin, sol), "pinSol": sol.hex()})
        predmemorija_galerija.clear()
        return {"ok": True, "pin": bool(pin)}

    @router.post("/galerije/{gid}/upload-url")
    def upload_url(gid: str, tijelo: dict, authorization: str | None = Header(default=None)):
        admin(authorization)
        galerija_admin(gid)
        datoteke = tijelo.get("datoteke")
        if not isinstance(datoteke, list) or not 1 <= len(datoteke) <= 50:
            raise HTTPException(status_code=400, detail="Pošalji 1 do 50 datoteka odjednom.")
        izlaz = []
        for d in datoteke:
            ext = str(d.get("ext", "")).lower().lstrip(".")
            ext = "jpg" if ext == "jpeg" else ("tif" if ext == "tiff" else ext)
            if ext not in DOZVOLJENI_EXT:
                raise HTTPException(status_code=400, detail=f"Format .{ext or '?'} nije podržan.")
            velicina = int(d.get("velicina", 0))
            if velicina <= 0 or velicina > MAX_ORIGINAL_BAJTOVA:
                raise HTTPException(status_code=400, detail="Datoteka je prevelika (najviše 500 MB).")
            fid = secrets.token_hex(8)
            tip_o = TIP_SADRZAJA.get(ext, "application/octet-stream")
            p = pohrana()
            izlaz.append({
                "id": fid, "ext": ext,
                "o": {"url": p.potpisi_put(f"g/{gid}/o/{fid}.{ext}", tip_o), "tip": tip_o},
                "m": {"url": p.potpisi_put(f"g/{gid}/m/{fid}.webp", "image/webp"), "tip": "image/webp"},
                "t": {"url": p.potpisi_put(f"g/{gid}/t/{fid}.webp", "image/webp"), "tip": "image/webp"},
            })
        return {"datoteke": izlaz}

    @router.post("/galerije/{gid}/fotke")
    def registriraj_fotke(gid: str, tijelo: dict, authorization: str | None = Header(default=None)):
        admin(authorization)
        galerija_admin(gid)
        popis = tijelo.get("fotke")
        if not isinstance(popis, list) or not 1 <= len(popis) <= 200:
            raise HTTPException(status_code=400, detail="Pošalji 1 do 200 fotki.")
        p = pohrana()
        novi = []
        for f in popis:
            fid, ext = str(f.get("id", "")), str(f.get("ext", "")).lower()
            if not ID_RE.match(fid) or ext not in DOZVOLJENI_EXT:
                raise HTTPException(status_code=400, detail="Neispravan zapis fotke.")
            o = p.head(f"g/{gid}/o/{fid}.{ext}")
            if o is None or p.head(f"g/{gid}/m/{fid}.webp") is None or p.head(f"g/{gid}/t/{fid}.webp") is None:
                raise HTTPException(status_code=400, detail=f"Fotka {f.get('ime', fid)} nije u potpunosti spremljena. Pokušaj ponovno.")
            velicina = int(o.get("ContentLength", 0))
            if velicina > MAX_ORIGINAL_BAJTOVA:
                raise HTTPException(status_code=400, detail="Datoteka je prevelika.")
            sirina, visina = int(f.get("w", 0)), int(f.get("h", 0))
            if not (1 <= sirina <= 100000 and 1 <= visina <= 100000):
                raise HTTPException(status_code=400, detail="Neispravne dimenzije.")
            boja = f.get("boja") if BOJA_RE.match(str(f.get("boja", ""))) else ""
            novi.append({
                "i": fid, "e": ext, "n": str(f.get("ime", fid))[:200], "w": sirina, "h": visina, "c": boja,
                "t": "raw" if ext in RAW_EXT else "foto", "s": velicina, "d": str(f.get("snimljeno", ""))[:25],
            })
        with brave[gid]:
            m = manifest_od(gid, osvjezi=True)
            postojeci = {x["i"] for x in m["fotke"]}
            m["fotke"] += [n for n in novi if n["i"] not in postojeci]
            spremi_manifest(gid, m)
        g = galerija_admin(gid)
        f = fb()
        izmjene = {"brojFotki": len(m["fotke"]), "velicinaUkupno": sum(x.get("s", 0) for x in m["fotke"]), "verzija": m["verzija"],
                   "izmijenjeno": f.SERVER_TIMESTAMP}
        if not g.get("naslovnaId") and m["fotke"]:
            izmjene["naslovnaId"] = m["fotke"][0]["i"]
        kolekcija().document(gid).update(izmjene)
        predmemorija_galerija.clear()
        return {"ok": True, "brojFotki": len(m["fotke"]), "verzija": m["verzija"]}

    @router.get("/galerije/{gid}/fotke")
    def popis_fotki(gid: str, authorization: str | None = Header(default=None)):
        admin(authorization)
        galerija_admin(gid)
        m = manifest_od(gid, osvjezi=True)
        return {"verzija": m["verzija"], "fotke": m["fotke"], "urlovi": urlovi_slika(gid, m, [x["i"] for x in m["fotke"]], "t")}

    @router.post("/galerije/{gid}/fotke/obrisi")
    def obrisi_fotke(gid: str, tijelo: dict, authorization: str | None = Header(default=None)):
        admin(authorization)
        g = galerija_admin(gid)
        ids = {str(i) for i in (tijelo.get("ids") or []) if ID_RE.match(str(i))}
        if not ids:
            raise HTTPException(status_code=400, detail="Nema što obrisati.")
        with brave[gid]:
            m = manifest_od(gid, osvjezi=True)
            za_brisanje = [x for x in m["fotke"] if x["i"] in ids]
            m["fotke"] = [x for x in m["fotke"] if x["i"] not in ids]
            spremi_manifest(gid, m)
        kljucevi = []
        for x in za_brisanje:
            kljucevi += [f"g/{gid}/o/{x['i']}.{x['e']}", f"g/{gid}/m/{x['i']}.webp", f"g/{gid}/t/{x['i']}.webp"]
        pohrana().obrisi_kljuceve(kljucevi)
        izmjene = {"brojFotki": len(m["fotke"]), "velicinaUkupno": sum(x.get("s", 0) for x in m["fotke"]), "verzija": m["verzija"]}
        if g.get("naslovnaId") in ids:
            izmjene["naslovnaId"] = m["fotke"][0]["i"] if m["fotke"] else None
        kolekcija().document(gid).update(izmjene)
        predmemorija_galerija.clear()
        return {"ok": True, "obrisano": len(za_brisanje)}

    @router.post("/galerije/{gid}/naslovna")
    def postavi_naslovnu(gid: str, tijelo: dict, authorization: str | None = Header(default=None)):
        admin(authorization)
        galerija_admin(gid)
        fid = str(tijelo.get("id", ""))
        if not any(x["i"] == fid for x in manifest_od(gid, osvjezi=True)["fotke"]):
            raise HTTPException(status_code=404, detail="Fotka ne postoji.")
        kolekcija().document(gid).update({"naslovnaId": fid})
        predmemorija_galerija.clear()
        return {"ok": True}

    @router.post("/galerije/{gid}/obrisi")
    def obrisi_galeriju(gid: str, authorization: str | None = Header(default=None)):
        admin(authorization)
        galerija_admin(gid)
        pohrana().obrisi_prefiks(f"g/{gid}/")
        pohrana().obrisi_prefiks(f"z/{gid}/")
        for d in kolekcija().document(gid).collection("posjetitelji").stream():
            d.reference.delete()
        kolekcija().document(gid).delete()
        predmemorija_galerija.clear()
        predmemorija_manifesta.pop(gid, None)
        return {"ok": True}

    # ================================================================ JAVNO (klijent)
    def opca_granica(request: Request):
        ogranicivac.provjeri(f"opce:{klijentski_ip(request)}", 240, 60)

    @router.get("/javno/g/{token}")
    def javno_galerija(token: str, request: Request, ref: str | None = None, x_pristup: str | None = Header(default=None)):
        opca_granica(request)
        gid, g = galerija_po_tokenu(token)
        if g.get("pinHash") and not pristup_vrijedi(x_pristup, gid):
            return {"trebaPin": True, "naziv": g.get("naziv", ""), "klijent": g.get("klijent", "")}
        m = manifest_od(gid)
        fotke = [kompaktno(f) for f in m["fotke"]]
        prvi = [f["i"] for f in m["fotke"][:36]]
        naslovna = g.get("naslovnaId") if any(f["i"] == g.get("naslovnaId") for f in m["fotke"]) else (prvi[0] if prvi else None)
        zabiljezi(gid, g, request, "pregled", ref)
        return {
            "trebaPin": False, "naziv": g.get("naziv", ""), "klijent": g.get("klijent", ""), "datum": g.get("datum", ""),
            "instagram": g.get("instagram", ""), "poruka": g.get("poruka", ""),
            "preuzimanje": g.get("preuzimanje", "otvoreno"), "brojFotki": len(fotke), "verzija": m["verzija"],
            "naslovnaId": naslovna, "naslovnaUrl": urlovi_slika(gid, m, [naslovna], "m").get(naslovna) if naslovna else None,
            "fotke": fotke, "urlovi": urlovi_slika(gid, m, prvi, "t"),
        }

    @router.post("/javno/g/{token}/pin")
    def javno_pin(token: str, tijelo: dict, request: Request):
        ogranicivac.provjeri(f"pin:{klijentski_ip(request)}:{token[:8]}", 8, 600)
        gid, g = galerija_po_tokenu(token)
        if not g.get("pinHash"):
            return {"pristup": None}
        pin = str(tijelo.get("pin", ""))[:64]
        sol = bytes.fromhex(g.get("pinSol", ""))
        if not hmac.compare_digest(hash_pina(pin, sol), g["pinHash"]):
            raise HTTPException(status_code=401, detail="Neispravan PIN.")
        return {"pristup": izdaj_pristup(gid)}

    @router.post("/javno/g/{token}/slike")
    def javno_slike(token: str, tijelo: dict, request: Request, x_pristup: str | None = Header(default=None)):
        opca_granica(request)
        gid, g = galerija_po_tokenu(token)
        provjeri_pristup(gid, g, x_pristup)
        vrsta = "m" if tijelo.get("v") == "m" else "t"
        ids = [str(i) for i in (tijelo.get("ids") or [])][:60]
        return {"urlovi": urlovi_slika(gid, manifest_od(gid), ids, vrsta)}

    def provjeri_preuzimanje(g: dict):
        if g.get("preuzimanje", "otvoreno") != "otvoreno":
            raise HTTPException(status_code=403, detail="Preuzimanje će biti omogućeno nakon uplate.")

    @router.post("/javno/g/{token}/preuzmi")
    def javno_preuzmi(token: str, tijelo: dict, request: Request, x_pristup: str | None = Header(default=None)):
        ogranicivac.provjeri(f"preuzmi:{klijentski_ip(request)}", 120, 600)
        gid, g = galerija_po_tokenu(token)
        provjeri_pristup(gid, g, x_pristup)
        provjeri_preuzimanje(g)
        fid = str(tijelo.get("id", ""))
        f = next((x for x in manifest_od(gid)["fotke"] if x["i"] == fid), None)
        if not f:
            raise nepostojeca()
        zabiljezi(gid, g, request, "preuzimanje", fid=fid)
        return {"url": pohrana().potpisi_get(f"g/{gid}/o/{fid}.{f['e']}", PREUZIMANJE_VRIJEME, f.get("n") or f"{fid}.{f['e']}")}

    @router.post("/javno/g/{token}/zip")
    def javno_zip(token: str, tijelo: dict, request: Request, x_pristup: str | None = Header(default=None)):
        ogranicivac.provjeri(f"zip:{klijentski_ip(request)}", 120, 3600)   # provjere statusa su česte
        gid, g = galerija_po_tokenu(token)
        provjeri_pristup(gid, g, x_pristup)
        provjeri_preuzimanje(g)
        m = manifest_od(gid)
        zeljeni = tijelo.get("ids")
        if zeljeni is None:
            odabrane = list(m["fotke"])
        else:
            trazeni = {str(i) for i in zeljeni}
            odabrane = [f for f in m["fotke"] if f["i"] in trazeni]
        if not odabrane:
            raise HTTPException(status_code=400, detail="Nema odabranih fotki.")
        if sum(f.get("s", 0) for f in odabrane) > MAX_ZIP_BAJTOVA:
            raise HTTPException(status_code=413, detail="Odabir je prevelik za jedan ZIP. Odaberi manje fotki.")
        sazetak = hashlib.sha1(f"{m['verzija']}|{','.join(sorted(f['i'] for f in odabrane))}".encode()).hexdigest()
        kljuc = f"z/{gid}/{sazetak}.zip"
        naziv = re.sub(r"[^\w.\- ]", "_", g.get("naziv", "galerija")).strip() or "galerija"
        p = pohrana()
        posao = poslovi_zip.get(kljuc)
        if posao and posao["status"] == "gradi":
            return {"status": "gradi", "napredak": posao["napredak"]}
        if p.head(kljuc) is not None:
            zabiljezi(gid, g, request, "zip")
            return {"status": "spremno", "url": p.potpisi_get(kljuc, PREUZIMANJE_VRIJEME, f"{naziv}.zip")}
        if posao and posao["status"] == "greska":
            poslovi_zip.pop(kljuc, None)

        novi = {"status": "gradi", "napredak": 0.0}
        poslovi_zip[kljuc] = novi
        if len(poslovi_zip) > 100:
            for k in [k for k, v in poslovi_zip.items() if v["status"] != "gradi"][:50]:
                poslovi_zip.pop(k, None)

        def posao_fn():
            with zip_semafor:
                try:
                    slozi_zip(p, gid, odabrane, kljuc, napredak=lambda i, n: novi.update(napredak=i / n))
                    novi.update(status="spremno", napredak=1.0)
                except Exception as e:  # noqa: BLE001
                    print(f"Galerije: ZIP nije složen: {e}")
                    novi.update(status="greska")

        threading.Thread(target=posao_fn, daemon=True).start()
        return {"status": "gradi", "napredak": 0.0}

    @router.get("/javno/g/{token}/naslovna")
    def javno_naslovna(token: str, request: Request):
        opca_granica(request)
        gid, g = galerija_po_tokenu(token)
        if g.get("pinHash"):
            raise nepostojeca()
        m = manifest_od(gid)
        fid = g.get("naslovnaId") or (m["fotke"][0]["i"] if m["fotke"] else None)
        if not fid or not any(f["i"] == fid for f in m["fotke"]):
            raise nepostojeca()
        return RedirectResponse(pohrana().potpisi_get(f"g/{gid}/m/{fid}.webp", 3600), status_code=302, headers={"Cache-Control": "no-store"})

    @router.get("/javno/g/{token}/og")
    def javno_og(token: str, request: Request):
        """Samo naslov za pregled linka (WhatsApp, Instagram). Nikad ne otkriva fotke galerije s PIN-om."""
        opca_granica(request)
        gid, g = galerija_po_tokenu(token)
        return {"naslov": g.get("naziv", "Fotografije"), "klijent": g.get("klijent", ""), "slika": not g.get("pinHash")}

    return router
