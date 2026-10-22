# Garmin -> SQLite -> HTML

Dnevno povlaci podatke s Garmin Connecta (Venu 3) u `garmin.db` i generira
stranicu **report.html** sa svim podacima (kartice, grafovi, tablice, sirovi JSON).

## 1. Instalacija (u VS Code terminalu)
```bash
python -m venv .venv
.venv\Scripts\Activate.ps1        # Windows PowerShell
# source .venv/bin/activate       # macOS / Linux
pip install -r requirements.txt
```

## 2. Podaci za prijavu -> .env datoteka
1. Kopiraj `.env.example` u novu datoteku `.env` (u istoj mapi).
2. Otvori `.env` i upisi svoj email i lozinku:
```
GARMIN_EMAIL=tvoj@email.com
GARMIN_PASSWORD=tvoja_lozinka
```
`.env` je u `.gitignore`, pa je Git nece objaviti. Nikome je ne salji.

## 3. Pokretanje
```bash
python garmin_sync.py --since 2024-01-01   # prvi put: povijest
python garmin_sync.py                      # kasnije: zadnja 3 dana
```
Nakon sinkronizacije otvori **report.html** dvoklikom (ili desni klik u VS Codeu ->
Open with Live Server / Reveal in File Explorer -> otvori u pregledniku).

Samo osvjezi stranicu bez povlacenja podataka: `python garmin_sync.py --html`

Ako imas 2FA, upisi kod kad te skripta zatrazi. Nakon prve prijave tokeni se spremaju
u `~/.garminconnect`, pa lozinka u `.env` vise nije potrebna.

## 4. Automatski svaki dan
**Windows:** Task Scheduler -> Create Basic Task -> Daily -> Start a program:
- Program: putanja do `.venv\Scripts\python.exe`
- Arguments: `garmin_sync.py`
- Start in: mapa projekta

**Linux / macOS (cron):**
```
0 7 * * * cd /putanja/do/garmin-sync && .venv/bin/python garmin_sync.py >> sync.log 2>&1
```

## Napomene
- Grafovi koriste Chart.js s interneta; bez neta tablice rade, grafovi ne.
- Biblioteka `garminconnect` je nesluzbena; ako nesto prestane raditi: `pip install -U garminconnect`.
- Tablica `raw` u bazi cuva kompletne JSON odgovore, a stranica prikazuje zadnjih 30 dana.

## 5. Backend za Zdravlje stranicu u nagy_app (Render)

`garmin_backend.py` je mali web servis (FastAPI) s jednim endpointom `POST /sync`
koji povuce Garmin podatke i upise ih u Firestore (istu bazu koju koristi nagy_app).
Zove ga gumb "Osvjezi sad" na Zdravlje stranici - zahtjev mora doci od prijavljenog
admina (provjerava se Firebase ID token), tako da ga nitko drugi ne moze pokrenuti.

### 5.1 Firebase service account kljuc
1. Firebase konzola -> Project settings (zupcanik) -> Service accounts.
2. "Generate new private key" -> preuzme se `.json` datoteka.
3. Lokalno: spremi je u ovu mapu kao `serviceAccountKey.json` (vec je u `.gitignore`,
   NIKAD je ne commitaj).
4. Na Renderu: u Settings -> Secret Files dodaj tu istu datoteku, npr. pod imenom
   `serviceAccountKey.json` (Render je montira na `/etc/secrets/serviceAccountKey.json`).

### 5.2 Deploy na Render
1. [render.com](https://render.com) -> New -> Web Service -> poveži ovaj repo/mapu.
2. Runtime: Python 3. Build command: `pip install -r requirements.txt`.
   Start command: `uvicorn garmin_backend:app --host 0.0.0.0 --port $PORT`
   (ili ostavi prazno, `Procfile` vec to govori Renderu).
3. Environment -> Environment Variables, dodaj:
   - `GARMIN_EMAIL`, `GARMIN_PASSWORD` - isto kao u `.env`
   - `FIREBASE_CREDENTIALS_PATH` = `/etc/secrets/serviceAccountKey.json`
4. Deploy. Render ti da javnu adresu tipa `https://garmin-backend-xxxx.onrender.com`.
5. Tu adresu upiši u nagy_app (`VITE_GARMIN_BACKEND_URL` u `.env` tog projekta).

### 5.3 Automatsko dnevno osvježavanje (opcionalno)
Render -> New -> Cron Job, isti repo, naredba npr.:
```
curl -X POST https://TVOJA-ADRESA.onrender.com/sync
```
(bez admin tokena cron poziv neće proći proveru - ako želiš automatski cron bez
gumba u appu, javi pa dodamo poseban "service" ključ samo za cron, odvojen od
Firebase admin provjere.)

### Napomena o Render free planu
Besplatan Web Service "zaspi" nakon 15 min neaktivnosti - prvi sljedeći poziv (npr.
klik na "Osvježi sad") može potrajati 30-50 sekundi dok se probudi. To je normalno.
