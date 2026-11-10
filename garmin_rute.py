"""
GPS rute hodanja i trčanja za kartu u appu (Zdravlje > Rute).

Garmin vrati desetke tisuća GPS točaka po aktivnosti. Ovdje se ruta:
  1. izvuče iz odgovora Garmin Connecta,
  2. (neobavezno) odsiječe prvih/zadnjih N metara - privatnost pri dijeljenju ekrana; default je 0 = ništa,
  3. pojednostavi (Douglas-Peucker) na par stotina točaka - dovoljno za lijep crtež,
  4. kodira u kratki tekst (Google "encoded polyline", preciznost 5 decimala ~ 1 m).

Čiste funkcije bez mreže i bez Firestorea, pa su lako provjerljive.
"""
import math

# Garmin tipovi aktivnosti koji imaju smislenu GPS rutu za hodanje/trčanje
TIPOVI_RUTA = (
    "running", "trail_running", "street_running", "track_running", "walking", "casual_walking",
    "speed_walking", "hiking", "nordic_walking",
)
# unutarnje (traka, dvorana) nemaju GPS
ISKLJUCI = ("treadmill", "indoor", "virtual")

MIN_UDALJENOST_M = 300    # kraće od ovoga nije ruta
MAKS_TOCAKA = 400


def je_ruta_tip(tip: str | None) -> bool:
    if not tip:
        return False
    t = tip.lower()
    if any(x in t for x in ISKLJUCI):
        return False
    return t in TIPOVI_RUTA or any(k in t for k in ("run", "walk", "hik"))


def haversine_m(a, b) -> float:
    """Udaljenost u metrima između dvije točke (lat, lon)."""
    r = 6371000.0
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dla, dlo = la2 - la1, lo2 - lo1
    h = math.sin(dla / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlo / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def izvuci_tocke(detalji) -> list[tuple[float, float]]:
    """Izvuče [(lat, lon), ...] iz odgovora Garmin get_activity_details; prazno ako nema GPS-a."""
    if not isinstance(detalji, dict):
        return []
    poli = (detalji.get("geoPolylineDTO") or {}).get("polyline") or []
    tocke = []
    for t in poli:
        lat, lon = t.get("lat"), t.get("lon")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and not (lat == 0 and lon == 0):
            tocke.append((float(lat), float(lon)))
    return tocke


def odsijeci_krajeve(tocke, metara: int):
    """
    "Krug privatnosti": makne početak rute dok se ne odmakne `metara` (zračno) od polazišta i kraj rute
    dok se ne odmakne `metara` od cilja. Zračna udaljenost (a ne prijeđeni put) jer GPS šum inače
    napuhne duljinu puta pa bi se odsjeklo puno manje od traženog.
    Ruta koja nikad ne izađe iz kruga je prekratka i vraća se prazna.
    """
    if metara <= 0 or len(tocke) < 2:
        return list(tocke)
    polaziste, cilj = tocke[0], tocke[-1]
    pocetak = next((i for i, t in enumerate(tocke) if haversine_m(polaziste, t) >= metara), None)
    if pocetak is None:
        return []
    kraj = next((i for i in range(len(tocke) - 1, -1, -1) if haversine_m(cilj, tocke[i]) >= metara), None)
    if kraj is None or kraj - pocetak < 2:
        return []
    return list(tocke[pocetak:kraj + 1])


def _udaljenost_do_pravca(p, a, b) -> float:
    """Približna udaljenost točke p od pravca ab u metrima (ravninska aproksimacija, dovoljno za rute)."""
    k = math.cos(math.radians((a[0] + b[0]) / 2))
    px, py = (p[1] - a[1]) * k * 111320.0, (p[0] - a[0]) * 110540.0
    bx, by = (b[1] - a[1]) * k * 111320.0, (b[0] - a[0]) * 110540.0
    duljina2 = bx * bx + by * by
    if duljina2 == 0:
        return math.hypot(px, py)
    t = max(0.0, min(1.0, (px * bx + py * by) / duljina2))
    return math.hypot(px - t * bx, py - t * by)


def _rdp(tocke, tolerancija_m: float):
    if len(tocke) < 3:
        return list(tocke)
    zadrzi = [False] * len(tocke)
    zadrzi[0] = zadrzi[-1] = True
    stog = [(0, len(tocke) - 1)]
    while stog:
        i, j = stog.pop()
        najdalje, indeks = 0.0, -1
        for m in range(i + 1, j):
            d = _udaljenost_do_pravca(tocke[m], tocke[i], tocke[j])
            if d > najdalje:
                najdalje, indeks = d, m
        if indeks != -1 and najdalje > tolerancija_m:
            zadrzi[indeks] = True
            stog.append((i, indeks))
            stog.append((indeks, j))
    return [t for t, z in zip(tocke, zadrzi) if z]


def pojednostavi(tocke, maks: int = MAKS_TOCAKA):
    """Douglas-Peucker: povećava toleranciju dok ruta nema najviše `maks` točaka."""
    if len(tocke) <= maks:
        return list(tocke)
    tolerancija = 1.0
    rezultat = _rdp(tocke, tolerancija)
    while len(rezultat) > maks and tolerancija < 500:
        tolerancija *= 1.6
        rezultat = _rdp(tocke, tolerancija)
    if len(rezultat) > maks:  # za svaki slučaj: ravnomjerno prorijedi
        korak = len(rezultat) / maks
        rezultat = [rezultat[int(i * korak)] for i in range(maks)] + [rezultat[-1]]
    return rezultat


def _kodiraj_vrijednost(v: int) -> str:
    v = ~(v << 1) if v < 0 else (v << 1)
    izlaz = ""
    while v >= 0x20:
        izlaz += chr((0x20 | (v & 0x1F)) + 63)
        v >>= 5
    return izlaz + chr(v + 63)


def kodiraj(tocke) -> str:
    """Google encoded polyline (preciznost 1e-5)."""
    izlaz = ""
    pl = po = 0
    for lat, lon in tocke:
        la, lo = round(lat * 1e5), round(lon * 1e5)
        izlaz += _kodiraj_vrijednost(la - pl) + _kodiraj_vrijednost(lo - po)
        pl, po = la, lo
    return izlaz


def dekodiraj(tekst: str):
    """Obrnuto od kodiraj (koristi se u testovima)."""
    tocke, i, lat, lon = [], 0, 0, 0
    while i < len(tekst):
        for os in (0, 1):
            pomak = rezultat = 0
            while True:
                b = ord(tekst[i]) - 63
                i += 1
                rezultat |= (b & 0x1F) << pomak
                pomak += 5
                if b < 0x20:
                    break
            promjena = ~(rezultat >> 1) if rezultat & 1 else (rezultat >> 1)
            if os == 0:
                lat += promjena
            else:
                lon += promjena
        tocke.append((lat / 1e5, lon / 1e5))
    return tocke


def granice(tocke):
    """[minLat, minLon, maxLat, maxLon], zaokruženo prema van na 5 decimala (koliko ima kodirana ruta)."""
    lat = [t[0] for t in tocke]
    lon = [t[1] for t in tocke]
    dolje = lambda v: math.floor(v * 1e5) / 1e5   # noqa: E731
    gore = lambda v: math.ceil(v * 1e5) / 1e5     # noqa: E731
    return [dolje(min(lat)), dolje(min(lon)), gore(max(lat)), gore(max(lon))]


def napravi_zapis(detalji, aktivnost: dict, odsijeci_m: int):
    """
    Iz Garmin detalja i sažetka aktivnosti napravi dokument za Firestore "zdravlje-rute".
    Vraća {"bezRute": True, ...} ako aktivnost nema (dovoljno) GPS-a - da je ne tražimo iznova.
    """
    osnovno = {
        "start_time": aktivnost.get("start_time"),
        "type": aktivnost.get("type"),
        "name": aktivnost.get("name"),
        "distance_m": aktivnost.get("distance_m"),
        "duration_s": aktivnost.get("duration_s"),
        "avg_speed": aktivnost.get("avg_speed"),
    }
    tocke = odsijeci_krajeve(izvuci_tocke(detalji), odsijeci_m)
    if len(tocke) < 5:
        return {**osnovno, "bezRute": True}
    jednostavno = pojednostavi(tocke)
    return {**osnovno, "bezRute": False, "tocke": kodiraj(jednostavno), "n": len(jednostavno), "granice": granice(jednostavno)}
