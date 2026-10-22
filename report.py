"""Generira samostalnu HTML stranicu (report.html) iz garmin.db."""
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

TEMPLATE = r"""<!DOCTYPE html>
<html lang="hr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Garmin podaci</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#1c2430;--muted:#6b7686;--line:#e3e7ee;--accent:#0a7cff}
@media (prefers-color-scheme:dark){:root{--bg:#0f1319;--card:#171d26;--text:#e8edf4;--muted:#93a0b2;--line:#263040;--accent:#4da3ff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;line-height:1.45}
.wrap{max-width:1100px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:1.5rem;margin:0}
h2{font-size:1.1rem;margin:28px 0 10px}
.top{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px}
.muted{color:var(--muted);font-size:.85rem}
select{background:var(--card);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:6px 10px;font:inherit}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.card .v{font-size:1.5rem;font-weight:600}
.card .l{color:var(--muted);font-size:.8rem}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px}
.chart{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px}
.chart h3{margin:0 0 8px;font-size:.95rem}
.chart div{position:relative;height:220px}
.tablebox{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:auto;max-height:480px}
table{border-collapse:collapse;width:100%;font-size:.85rem;white-space:nowrap}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:right}
th:first-child,td:first-child,td.l,th.l{text-align:left}
th{position:sticky;top:0;background:var(--card);color:var(--muted);font-weight:600}
pre{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;overflow:auto;max-height:420px;font-size:.78rem}
.row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div><h1>Garmin Venu 3 – moji podaci</h1><div class="muted" id="gen"></div></div>
    <div>Razdoblje: <select id="range">
      <option value="7">7 dana</option><option value="30" selected>30 dana</option>
      <option value="90">90 dana</option><option value="365">1 godina</option><option value="0">Sve</option>
    </select></div>
  </div>

  <div class="cards" id="cards"></div>

  <h2>Grafovi</h2>
  <div class="grid">
    <div class="chart"><h3>Koraci</h3><div><canvas id="c_steps"></canvas></div></div>
    <div class="chart"><h3>Spavanje (sati po fazama)</h3><div><canvas id="c_sleep"></canvas></div></div>
    <div class="chart"><h3>Puls u mirovanju</h3><div><canvas id="c_rhr"></canvas></div></div>
    <div class="chart"><h3>HRV</h3><div><canvas id="c_hrv"></canvas></div></div>
    <div class="chart"><h3>Body Battery</h3><div><canvas id="c_bb"></canvas></div></div>
    <div class="chart"><h3>Prosječni stres</h3><div><canvas id="c_stress"></canvas></div></div>
  </div>

  <h2>Dnevni podaci</h2>
  <div class="tablebox"><table id="t_daily"></table></div>

  <h2>Aktivnosti</h2>
  <div class="tablebox"><table id="t_act"></table></div>

  <h2>Sirovi podaci (JSON)</h2>
  <div class="muted" style="margin-bottom:8px" id="rawinfo"></div>
  <div class="row"><select id="rawday"></select><select id="rawkind"></select></div>
  <pre id="rawview">–</pre>
</div>

<script>
const D = __DATA__;
const $ = id => document.getElementById(id);
const hrs = s => s == null ? null : +(s / 3600).toFixed(2);
const fh  = s => s == null ? '–' : Math.floor(s/3600) + ' h ' + Math.round((s%3600)/60) + ' min';
const n   = v => v == null ? '–' : (typeof v === 'number' ? Math.round(v*10)/10 : v);
const km  = m => m == null ? '–' : (m/1000).toFixed(2) + ' km';
const dur = s => s == null ? '–' : Math.floor(s/3600) + ':' + String(Math.floor((s%3600)/60)).padStart(2,'0') + ':' + String(Math.round(s%60)).padStart(2,'0');

$('gen').textContent = 'Generirano: ' + D.generated;

function pick(rows, days){
  if(!days) return rows;
  const cut = new Date(); cut.setDate(cut.getDate() - days);
  const c = cut.toISOString().slice(0,10);
  return rows.filter(r => r.day >= c);
}

let charts = [];
function mk(id, type, labels, sets, opts){
  if(!window.Chart) return;
  charts.push(new Chart($(id), {type, data:{labels, datasets:sets},
    options:Object.assign({responsive:true, maintainAspectRatio:false, spanGaps:true,
      plugins:{legend:{display:sets.length>1}}}, opts||{})}));
}

function render(){
  charts.forEach(c => c.destroy()); charts = [];
  const rows = pick(D.daily, +$('range').value);
  const L = rows.map(r => r.day);

  // kartice (zadnji dan s podacima)
  const last = [...rows].reverse().find(r => r.steps != null) || rows[rows.length-1] || {};
  const cards = [
    ['Koraci', n(last.steps)], ['Spavanje', fh(last.sleep_seconds)], ['Sleep score', n(last.sleep_score)],
    ['Puls u mirovanju', n(last.resting_hr)], ['HRV (noć)', n(last.hrv_last_night)],
    ['Body Battery max', n(last.body_battery_high)], ['Stres (prosj.)', n(last.avg_stress)], ['Kalorije', n(last.calories)]
  ];
  $('cards').innerHTML = cards.map(c => `<div class="card"><div class="v">${c[1]}</div><div class="l">${c[0]}${last.day ? ' · ' + last.day : ''}</div></div>`).join('');

  mk('c_steps','bar',L,[{label:'Koraci',data:rows.map(r=>r.steps),backgroundColor:'#0a7cff'}]);
  mk('c_sleep','bar',L,[
    {label:'Duboki',data:rows.map(r=>hrs(r.deep_seconds)),backgroundColor:'#3b5bdb'},
    {label:'Lagani',data:rows.map(r=>hrs(r.light_seconds)),backgroundColor:'#74a7ff'},
    {label:'REM',data:rows.map(r=>hrs(r.rem_seconds)),backgroundColor:'#9775fa'},
    {label:'Budan',data:rows.map(r=>hrs(r.awake_seconds)),backgroundColor:'#ffa94d'}
  ],{scales:{x:{stacked:true},y:{stacked:true}}});
  mk('c_rhr','line',L,[{label:'bpm',data:rows.map(r=>r.resting_hr),borderColor:'#e03131',tension:.3}]);
  mk('c_hrv','line',L,[
    {label:'Noć',data:rows.map(r=>r.hrv_last_night),borderColor:'#2f9e44',tension:.3},
    {label:'Tjedni prosjek',data:rows.map(r=>r.hrv_weekly_avg),borderColor:'#868e96',borderDash:[5,4],tension:.3}
  ]);
  mk('c_bb','line',L,[
    {label:'Najviše',data:rows.map(r=>r.body_battery_high),borderColor:'#f08c00',tension:.3},
    {label:'Najmanje',data:rows.map(r=>r.body_battery_low),borderColor:'#adb5bd',tension:.3}
  ]);
  mk('c_stress','line',L,[{label:'Stres',data:rows.map(r=>r.avg_stress),borderColor:'#d6336c',tension:.3}]);

  // tablica dana
  const head = ['Dan','Koraci','Udaljenost','Kcal','Puls mir.','Stres','BB max','BB min','Spavanje','Duboki','Lagani','REM','Budan','Score','HRV noć','HRV tjedni','HRV status'];
  const body = [...rows].reverse().map(r => [r.day,n(r.steps),km(r.distance_m),n(r.calories),n(r.resting_hr),n(r.avg_stress),
    n(r.body_battery_high),n(r.body_battery_low),fh(r.sleep_seconds),fh(r.deep_seconds),fh(r.light_seconds),fh(r.rem_seconds),
    fh(r.awake_seconds),n(r.sleep_score),n(r.hrv_last_night),n(r.hrv_weekly_avg),r.hrv_status||'–']);
  $('t_daily').innerHTML = '<thead><tr>' + head.map(h=>`<th>${h}</th>`).join('') + '</tr></thead><tbody>' +
    body.map(r => '<tr>' + r.map(c=>`<td>${c}</td>`).join('') + '</tr>').join('') + '</tbody>';

  // aktivnosti
  const days = +$('range').value;
  let acts = D.activities;
  if(days){ const cut = new Date(); cut.setDate(cut.getDate()-days); const c = cut.toISOString().slice(0,10); acts = acts.filter(a => (a.start_time||'') >= c); }
  const ah = ['Početak','Naziv','Tip','Udaljenost','Trajanje','Kcal','Prosj. puls','Max puls'];
  $('t_act').innerHTML = '<thead><tr>' + ah.map((h,i)=>`<th class="${i<3?'l':''}">${h}</th>`).join('') + '</tr></thead><tbody>' +
    (acts.length ? acts.map(a => `<tr><td class="l">${(a.start_time||'').slice(0,16)}</td><td class="l">${a.name||'–'}</td><td class="l">${a.type||'–'}</td><td>${km(a.distance_m)}</td><td>${dur(a.duration_s)}</td><td>${n(a.calories)}</td><td>${n(a.avg_hr)}</td><td>${n(a.max_hr)}</td></tr>`).join('')
    : '<tr><td class="l" colspan="8">Nema aktivnosti u ovom razdoblju.</td></tr>') + '</tbody>';
}

// sirovi podaci
const rawDays = Object.keys(D.raw).sort().reverse();
$('rawinfo').textContent = 'Prikazano posljednjih ' + D.rawDays + ' dana. Kompletna povijest je u garmin.db (tablica raw).';
$('rawday').innerHTML = rawDays.map(d => `<option>${d}</option>`).join('');
function fillKinds(){
  const kinds = Object.keys(D.raw[$('rawday').value] || {});
  $('rawkind').innerHTML = kinds.map(k => `<option>${k}</option>`).join('');
  showRaw();
}
function showRaw(){
  const o = (D.raw[$('rawday').value] || {})[$('rawkind').value];
  $('rawview').textContent = o ? JSON.stringify(o, null, 2) : '–';
}
$('rawday').onchange = fillKinds; $('rawkind').onchange = showRaw; $('range').onchange = render;
render(); fillKinds();
</script>
</body>
</html>
"""


def generate(db_path, out_path, raw_days=30):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    daily = [dict(r) for r in conn.execute("SELECT * FROM daily ORDER BY day")]
    acts = [dict(r) for r in conn.execute(
        "SELECT activity_id,start_time,name,type,distance_m,duration_s,calories,avg_hr,max_hr "
        "FROM activities ORDER BY start_time DESC")]

    cut = (date.today() - timedelta(days=raw_days)).isoformat()
    raw = {}
    for r in conn.execute("SELECT day,kind,json FROM raw WHERE day >= ? ORDER BY day", (cut,)):
        raw.setdefault(r["day"], {})[r["kind"]] = json.loads(r["json"])
    conn.close()

    data = {
        "generated": date.today().strftime("%d.%m.%Y."),
        "daily": daily, "activities": acts, "raw": raw, "rawDays": raw_days,
    }
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    Path(out_path).write_text(TEMPLATE.replace("__DATA__", payload), encoding="utf-8")
    return out_path
