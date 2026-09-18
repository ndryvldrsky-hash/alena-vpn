#!/usr/bin/env python3
"""Веб-плеер архива камер на VPS alena-vpn (2026-09-17).

Архив: /var/lib/cam-archive/<камера>/<ГГГГММДД_ЧЧММСС>.mkv — минутные куски субпотоков (HEVC + G.711), пишут службы
cam-archive@<камера>, хранится последний час. Имена кусков — во времени VPS (UTC), на странице — по Израилю.

Браузеры HEVC в mkv не играют, поэтому выбранная минута перекодируется в H.264/AAC mp4 по запросу (на 1 vCPU ~7–10 с)
и кладётся в кэш; следующая минута готовится заранее, чтобы воспроизведение шло подряд. Режим «оригинал» — только
переупаковка HEVC в mp4 (~0,5 с), играет там, где браузер умеет HEVC.

Слушает только 127.0.0.1:8080; наружу — `tailscale serve` (HTTPS, видно только устройствам tailnet), своего пароля нет.
Исходник — репозиторий alena-vpn (/config/alena-vpn/cam-archive/cam_archive_web.py), на VPS — /usr/local/lib/cam-archive-web/app.py (служба cam-archive-web).
"""
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo

ARCHIVE = "/var/lib/cam-archive"
CACHE = "/var/cache/cam-archive-web"
TZ = ZoneInfo("Asia/Jerusalem")
NAME_RE = re.compile(r"^(\d{8})_(\d{6})\.mkv$")
# события Frigate кладёт сюда /config/.local/bin/cam_archive_events_push.py (cron аддона на Оптиплексе, раз в минуту)
EVENTS = "/var/lib/cam-archive/events"
EVENT_ID_RE = re.compile(r"^\d+\.\d+-[a-z0-9]+$")
CAM_RE = re.compile(r"^[a-z0-9_]+$")
# кусок, который ещё пишется, не трогаем: последний изменялся недавно
WRITING_AGE = 20

_locks = {}
_locks_guard = threading.Lock()


def cams():
    try:
        # events — папка событий Frigate, не камера
        return sorted(d for d in os.listdir(ARCHIVE)
                      if CAM_RE.match(d) and d != "events" and os.path.isdir(os.path.join(ARCHIVE, d)))
    except FileNotFoundError:
        return []


def chunks(cam):
    """Куски камеры по времени: [(имя, начало UTC, байт, дописан)]."""
    d = os.path.join(ARCHIVE, cam)
    out = []
    now = time.time()
    names = sorted(f for f in os.listdir(d) if NAME_RE.match(f))
    for i, f in enumerate(names):
        p = os.path.join(d, f)
        try:
            st = os.stat(p)
        except FileNotFoundError:
            continue
        m = NAME_RE.match(f)
        start = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        done = i < len(names) - 1 and now - st.st_mtime > WRITING_AGE
        out.append((f, start, st.st_size, done))
    return out


def lock_for(key):
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def stream_start(path, kind):
    """Метка времени первого пакета потока (v — видео, a — звук), секунды; 0, если не удалось узнать."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", f"{kind}:0", "-read_intervals", "%+#5",
                        "-show_entries", "packet=pts_time", "-of", "csv=p=0", path],
                       capture_output=True, text=True, timeout=30)
    vals = [float(x) for x in r.stdout.split() if re.match(r"^-?\d+(\.\d+)?$", x)]
    return min(vals) if vals else 0.0


_vcodec_cache = {}


def video_codec(path, cam):
    """Кодек видео куска (по камере кэшируем — у одной камеры он постоянный). 2026-09-18: axis_2100/imac/xps пишутся
    основным потоком в H.264 — такой кусок в mp4 переупаковываем без перекодирования, а не гоняем libx264 на 1 vCPU."""
    if cam in _vcodec_cache:
        return _vcodec_cache[cam]
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name",
                        "-of", "csv=p=0", path], capture_output=True, text=True, timeout=30)
    codec = r.stdout.strip() or "unknown"
    if codec != "unknown":
        _vcodec_cache[cam] = codec
    return codec


def ensure_mp4(cam, name, mode):
    """Готовый mp4 в кэше (перекодирование или переупаковка). None — исходника нет или ffmpeg упал."""
    src = os.path.join(ARCHIVE, cam, name)
    base = name[:-4] + (".hevc.mp4" if mode == "hevc" else ".mp4")
    dst_dir = os.path.join(CACHE, cam)
    dst = os.path.join(dst_dir, base)
    if os.path.exists(dst):
        return dst
    if not os.path.exists(src):
        return None
    with lock_for(dst):
        if os.path.exists(dst):
            return dst
        os.makedirs(dst_dir, exist_ok=True)
        tmp = dst + ".part"
        # В кусках видео начинается на ~12 с позже звука: сегментатор обнуляет время по первому пакету, а первым
        # приходит звук (метки времени камеры). Без выравнивания плеер ~12 с показывает чёрный экран.
        # Начало видео и звука сводим в ноль: при перекодировании — setpts/asetpts, в режиме «оригинал» (видео
        # без перекодирования фильтр не применить) — сдвигом входа видео на его стартовую метку.
        vstart = stream_start(src, "v")
        codec = video_codec(src, cam)
        if mode == "hevc" or codec == "h264":
            # копия видео: HEVC по просьбе «оригинал» либо источник уже H.264 (полные потоки axis_2100/imac/xps)
            inputs = ["-itsoffset", f"{-vstart:.3f}", "-i", src, "-i", src]
            vargs = ["-map", "0:v:0", "-map", "1:a:0?", "-c:v", "copy", "-af", "asetpts=PTS-STARTPTS"]
            if codec != "h264":
                vargs += ["-tag:v", "hvc1"]
        else:
            inputs = ["-i", src]
            vargs = ["-map", "0:v:0", "-map", "0:a:0?", "-vf", "setpts=PTS-STARTPTS", "-af", "asetpts=PTS-STARTPTS",
                     "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-g", "24", "-pix_fmt", "yuv420p"]
        cmd = ["nice", "-n", "10", "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs,
               *vargs, "-c:a", "aac", "-b:a", "32k", "-movflags", "+faststart", "-f", "mp4", tmp]
        r = subprocess.run(cmd, capture_output=True, timeout=180)
        if r.returncode != 0 or not os.path.exists(tmp):
            try:
                os.remove(tmp)
            except FileNotFoundError:
                pass
            return None
        os.replace(tmp, dst)
        return dst


def prefetch_next(cam, name):
    """Заранее перекодировать следующую дописанную минуту — чтобы плеер перешёл на неё без ожидания."""
    lst = chunks(cam)
    for i, (f, _, _, _) in enumerate(lst):
        if f == name and i + 1 < len(lst) and lst[i + 1][3]:
            threading.Thread(target=ensure_mp4, args=(cam, lst[i + 1][0], "h264"), daemon=True).start()
            return


def cache_cleaner():
    """Удалять из кэша то, чего уже нет в архиве (архив чистит cam-archive-clean.timer)."""
    while True:
        try:
            for cam in os.listdir(CACHE):
                d = os.path.join(CACHE, cam)
                for f in os.listdir(d):
                    src = os.path.join(ARCHIVE, cam, f.split(".")[0] + ".mkv")
                    p = os.path.join(d, f)
                    stale_part = f.endswith(".part") and time.time() - os.path.getmtime(p) > 600
                    if stale_part or (not f.endswith(".part") and not os.path.exists(src)):
                        os.remove(p)
        except FileNotFoundError:
            pass
        except Exception:
            pass
        time.sleep(60)


PAGE = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Архив камер</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/plyr@3.7.8/dist/plyr.css">
<style>
:root{--bg:#0e1116;--panel:#171b22;--panel2:#1f252e;--line:#2a313b;--text:#e8ebf0;--muted:#8b95a4;--acc:#ff7a45;--acc2:#ffb347;
--seg:#39424f;--ready:#2e8b57;--writing:#8a6d1f;--head:#ff4d4f;--plyr-color-main:#ff7a45;--plyr-video-control-background-hover:#ff7a45}
@media (prefers-color-scheme: light){:root{--bg:#f3f4f7;--panel:#fff;--panel2:#f0f2f5;--line:#dde1e7;--text:#1b1f24;--muted:#646d79;--acc:#e85d1f;--acc2:#d9891a;
--seg:#c3cad4;--ready:#6cc08f;--writing:#e3c77a;--head:#d1242f;--plyr-color-main:#e85d1f}}
*{box-sizing:border-box}body{margin:0;font:15px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--bg);color:var(--text)}
header{display:flex;flex-wrap:wrap;gap:10px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--panel)}
h1{font-size:17px;margin:0;display:flex;align-items:center;gap:8px}
h1 .rec{width:10px;height:10px;border-radius:50%;background:var(--head);box-shadow:0 0 0 0 rgba(255,77,79,.6);animation:pulse 2s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(255,77,79,.6)}70%{box-shadow:0 0 0 8px rgba(255,77,79,0)}100%{box-shadow:0 0 0 0 rgba(255,77,79,0)}}
.sub{color:var(--muted);font-size:13px}
button{background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:999px;padding:6px 13px;cursor:pointer;font:inherit;transition:border-color .15s,color .15s,background .15s}
button:hover{border-color:var(--acc)}
button.on{border-color:var(--acc);color:#fff;background:var(--acc)}
main{max-width:1200px;margin:0 auto;padding:14px 16px}
.player{position:relative;border-radius:14px;overflow:hidden;background:#000;box-shadow:0 8px 30px rgba(0,0,0,.35)}
.player video{width:100%;display:block;max-height:62vh;background:#000}
.plyr{border-radius:14px}
.osd{position:absolute;left:14px;top:12px;z-index:5;pointer-events:none;display:none;
  background:rgba(0,0,0,.55);backdrop-filter:blur(4px);color:#fff;border-radius:10px;padding:6px 12px;font-variant-numeric:tabular-nums}
.osd b{font-size:22px;letter-spacing:.5px}.osd span{display:block;font-size:12px;opacity:.8}
.status{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);z-index:6;display:none;
  background:rgba(0,0,0,.7);color:#fff;border-radius:12px;padding:12px 18px;font-size:15px;text-align:center;max-width:80%}
.status .spin{display:inline-block;width:16px;height:16px;border:2px solid rgba(255,255,255,.3);border-top-color:#fff;border-radius:50%;animation:rot 1s linear infinite;vertical-align:-3px;margin-right:8px}
@keyframes rot{to{transform:rotate(360deg)}}
.bar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:12px 0}
.bar .grp{display:flex;gap:4px;align-items:center;padding:3px;border-radius:999px;background:var(--panel);border:1px solid var(--line)}
.bar .grp button{border:none;background:transparent;padding:5px 11px}
.bar .grp button.on{background:var(--acc);color:#fff}
.bar .sp{flex:1}
.bar a{color:var(--acc)}
.muted{color:var(--muted)}
/* таймлайн */
.tl{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:10px 12px 12px;user-select:none}
.tl-row{display:flex;align-items:flex-start;gap:8px}
.tl-labels{width:84px;flex:none;padding-top:20px}
.tl-label{height:34px;margin-top:4px;display:flex;align-items:center;justify-content:flex-end;font-size:13px;color:var(--muted);cursor:pointer;white-space:nowrap;overflow:hidden}
.tl-label.on{color:var(--acc);font-weight:600}
.tl-area{position:relative;flex:1;min-width:0}
.ticks{position:relative;height:20px;font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums}
.ticks .tk{position:absolute;bottom:0;border-left:1px solid var(--line);height:6px}
.ticks .tk.major{height:10px;border-color:var(--muted)}
.ticks .tx{position:absolute;top:0;transform:translateX(-50%);white-space:nowrap}
.track{position:relative;height:34px;margin-top:4px;background:var(--bg);border-radius:7px;cursor:pointer;touch-action:none;overflow:hidden}
.track.on{box-shadow:inset 0 0 0 1px var(--acc)}
.seg{position:absolute;top:4px;bottom:4px;background:var(--seg);border-radius:3px}
.seg.ready{background:var(--ready)}
.seg.writing{background:var(--writing)}
.seg.play{box-shadow:inset 0 0 0 2px var(--acc2)}
.ev{position:absolute;bottom:2px;height:12px;min-width:5px;border-radius:3px;z-index:2;cursor:pointer;box-shadow:0 0 0 1px rgba(0,0,0,.5)}
.ev:hover{transform:scaleY(1.35);transform-origin:bottom}
.head{position:absolute;top:20px;bottom:0;width:2px;background:var(--head);pointer-events:none;display:none;z-index:3;box-shadow:0 0 6px var(--head)}
.head::before{content:"";position:absolute;top:-6px;left:-5px;border:6px solid transparent;border-top-color:var(--head)}
.hover{position:absolute;top:20px;bottom:0;width:1px;background:var(--text);opacity:.55;pointer-events:none;display:none;z-index:2}
.hover span{position:absolute;top:-20px;left:4px;background:var(--text);color:var(--bg);font-size:11px;padding:1px 5px;border-radius:4px;white-space:nowrap}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin-top:10px;font-size:12px;color:var(--muted)}
.legend i{display:inline-block;width:14px;height:8px;border-radius:2px;margin-right:5px;vertical-align:middle}
/* карточка события при наведении */
.card{position:fixed;z-index:50;display:none;pointer-events:none;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden;box-shadow:0 10px 30px rgba(0,0,0,.45);width:200px}
.card img{width:100%;display:block;background:#000;min-height:40px}
.card div{padding:6px 9px;font-size:13px}
/* список событий */
.events{margin-top:14px}
.events h2{font-size:15px;margin:0 0 8px;display:flex;align-items:center;gap:8px}
.evlist{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.evi{background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden;cursor:pointer;transition:transform .12s,border-color .12s}
.evi:hover{transform:translateY(-2px);border-color:var(--acc)}
.evi img{width:100%;aspect-ratio:16/10;object-fit:cover;display:block;background:#000}
.evi .t{padding:5px 8px;font-size:12px;display:flex;gap:6px;align-items:center}
.evi .dot{width:8px;height:8px;border-radius:50%;flex:none}
.hint{color:var(--muted);font-size:13px;margin-top:14px}
@media (max-width:600px){.tl-labels{width:56px}.tl-label{font-size:12px}.osd b{font-size:17px}}
</style></head><body>
<header><h1><span class="rec"></span>Архив камер</h1><span class="sub">последний час · <span id="upd">…</span></span><span style="flex:1"></span>
<div class="bar" style="margin:0"><div class="grp"><button id="m264" class="on" title="Перекодирование в H.264 — играет везде, новая минута готовится ~10 с">H.264</button>
<button id="mhevc" title="Оригинальный HEVC без перекодирования — быстро, но играет не в каждом браузере">Оригинал</button></div></div></header>
<main>
<div class="player"><video id="v" playsinline controls></video>
  <div class="osd" id="osd"><b id="osdt"></b><span id="osdc"></span></div>
  <div class="status" id="st"></div></div>
<div class="bar">
  <div class="grp"><button id="bm60" title="−1 минута (Shift+←)">⏪ 1 мин</button><button id="bm10" title="−10 секунд (←)">↶ 10 с</button>
  <button id="bpp" title="Пауза / воспроизведение (пробел)">⏯</button>
  <button id="bp10" title="+10 секунд (→)">10 с ↷</button><button id="bp60" title="+1 минута (Shift+→)">1 мин ⏩</button></div>
  <div class="grp" id="speeds" title="Скорость воспроизведения"></div>
  <button id="blive" title="К последней записанной минуте">⏭ К концу</button>
  <span class="sp"></span>
  <label class="muted"><input type="checkbox" id="auto" checked> подряд</label>
  <a id="dl" href="#" style="display:none">⬇ mkv</a>
</div>
<div class="tl" id="tl">
  <div class="tl-row"><div class="tl-labels" id="labels"></div><div class="tl-area" id="area"><div class="ticks" id="ticks"></div><div id="tracks"></div>
    <div class="head" id="head"></div><div class="hover" id="hover"><span></span></div></div></div>
  <div class="legend"><span><i style="background:var(--seg)"></i>записано</span><span><i style="background:var(--ready)"></i>подготовлено</span>
    <span><i style="background:var(--writing)"></i>пишется</span><span><i style="background:#ff4d6d"></i>человек</span><span><i style="background:#4ea1ff"></i>машина</span>
    <span><i style="background:#c678dd"></i>животное</span><span><i style="background:#e5c07b"></i>другое</span></div>
</div>
<div class="events"><h2>События <span class="sub" id="evn"></span></h2><div class="evlist" id="evlist"></div></div>
<div class="hint">Щелчок или протягивание по полосе — открыть камеру с этого места; щелчок по событию — за 3 с до него. Клавиши: ← → ±10 с, Shift ±1 мин, пробел — пауза, 1–4 — скорость.
Время по Израилю. Скачанный mkv открывается в VLC.</div>
</main>
<div class="card" id="card"><img id="cardimg" alt=""><div id="cardt"></div></div>
<script src="https://cdn.jsdelivr.net/npm/plyr@3.7.8/dist/plyr.min.js"></script>
<script>
const $=id=>document.getElementById(id), v=$('v');
const fmt=new Intl.DateTimeFormat('ru-RU',{timeZone:'Asia/Jerusalem',hour:'2-digit',minute:'2-digit',second:'2-digit'});
const fmtHM=new Intl.DateTimeFormat('ru-RU',{timeZone:'Asia/Jerusalem',hour:'2-digit',minute:'2-digit'});
const LABELS={person:'человек',car:'машина',truck:'грузовик',bicycle:'велосипед',motorcycle:'мотоцикл',dog:'собака',cat:'кошка',bird:'птица',face:'лицо',license_plate:'номер',package:'посылка'};
const COLORS={person:'#ff4d6d',car:'#4ea1ff',truck:'#4ea1ff',bicycle:'#4ea1ff',motorcycle:'#4ea1ff',dog:'#c678dd',cat:'#c678dd',bird:'#c678dd'};
const SPEEDS=[1,2,4,8];
let data={cameras:{},events:[],now:Date.now()}, cam=null, cur=null, mode='h264', pending=0, rate=1, t0=0, t1=1;

// Plyr — если CDN недоступен, остаётся обычный плеер браузера
let plyr=null;
if(window.Plyr){
  plyr=new Plyr(v,{controls:['play-large','play','progress','current-time','mute','volume','settings','pip','fullscreen'],
    settings:['speed'],speed:{selected:1,options:[0.5,1,2,4,8]},keyboard:{focused:false,global:false},tooltips:{controls:true,seek:true},
    i18n:{play:'Воспроизвести',pause:'Пауза',mute:'Выключить звук',unmute:'Включить звук',settings:'Настройки',speed:'Скорость',normal:'Обычная',
      enterFullscreen:'Во весь экран',exitFullscreen:'Выйти из полноэкранного режима',pip:'Картинка в картинке',volume:'Громкость',seek:'Перемотка',currentTime:'Текущее время'}});
  // OSD и статус — внутрь контейнера Plyr, чтобы были видны и во весь экран
  plyr.on('ready',()=>{const c=plyr.elements.container;c.appendChild($('osd'));c.appendChild($('st'))});
}

function st(t,spin){$('st').innerHTML=t?(spin?'<span class="spin"></span>':'')+t:'';$('st').style.display=t?'block':'none'}
function pct(t){return (t-t0)/(t1-t0)*100}
function list(c){return (data.cameras[c]||[])}
function evColor(l){return COLORS[l]||'#e5c07b'}
function evName(e){return (LABELS[e.label]||e.label)+(e.sub_label?' · '+e.sub_label:'')}

async function load(){
  try{const r=await fetch('api/list',{cache:'no-store'});data=await r.json()}catch(e){return}
  const cs=Object.keys(data.cameras);
  if(!cam||!cs.includes(cam))cam=cs[0]||null;
  if(cur){const f=list(cur.cam).find(x=>x.file===cur.file);if(f)Object.assign(cur,f)}
  let mn=Infinity;cs.forEach(c=>list(c).forEach(x=>{if(x.ts<mn)mn=x.ts}));
  t1=data.now;t0=isFinite(mn)?Math.floor(mn/60000)*60000:t1-3600000;
  if(t1-t0<600000)t0=t1-600000;
  $('upd').textContent='обновлено '+fmtHM.format(data.now);
  render();renderEvents();
}

function render(){
  const tk=$('ticks');tk.innerHTML='';
  const w=$('area').clientWidth||600, span=(t1-t0)/60000, labelStep=(w/span<9)?10:5;
  for(let m=Math.ceil(t0/60000)*60000;m<=t1;m+=60000){
    const major=(Math.round(m/60000)%labelStep)===0;
    const d=document.createElement('div');d.className='tk'+(major?' major':'');d.style.left=pct(m)+'%';tk.appendChild(d);
    if(major){const x=document.createElement('div');x.className='tx';x.style.left=pct(m)+'%';x.textContent=fmtHM.format(m);tk.appendChild(x)}
  }
  const tr=$('tracks');tr.innerHTML='';
  Object.keys(data.cameras).forEach(c=>{
    const t=document.createElement('div');t.className='track'+(c===cam?' on':'');t.dataset.cam=c;
    list(c).forEach(x=>{
      const s=document.createElement('div');
      s.className='seg'+(!x.done?' writing':(x.ready&&mode==='h264'?' ready':''))+(cur&&cur.cam===c&&cur.file===x.file?' play':'');
      s.style.left=pct(x.ts)+'%';s.style.width=Math.max(0.15,pct(x.end)-pct(x.ts))+'%';
      t.appendChild(s)});
    (data.events||[]).filter(e=>e.camera===c&&(e.end||data.now)>=t0).forEach(e=>{
      const m=document.createElement('div');m.className='ev';
      const a=Math.max(t0,e.start),b=Math.min(t1,e.end||data.now);
      m.style.left=pct(a)+'%';m.style.width=Math.max(0,pct(b)-pct(a))+'%';m.style.background=evColor(e.label);
      m.addEventListener('pointerdown',ev=>ev.stopPropagation());
      m.addEventListener('pointerenter',ev=>showCard(e,ev));m.addEventListener('pointermove',ev=>moveCard(ev));
      m.addEventListener('pointerleave',hideCard);
      m.addEventListener('click',ev=>{ev.stopPropagation();hideCard();seekAbs(c,e.start-3000)});
      t.appendChild(m)});
    bindTrack(t);tr.appendChild(t);
  });
  renderLabels();updateHead();
}

function renderLabels(){
  const col=$('labels');col.innerHTML='';
  Object.keys(data.cameras).forEach(c=>{
    const l=document.createElement('div');l.className='tl-label'+(c===cam?' on':'');l.textContent=c;l.title='К последней минуте '+c;
    l.onclick=()=>{cam=c;const last=list(c).filter(x=>x.done).pop();if(last)seekAbs(c,last.ts);else render()};
    col.appendChild(l)});
}

function renderEvents(){
  const cs=Object.keys(data.cameras), ev=(data.events||[]).filter(e=>cs.includes(e.camera)).sort((a,b)=>b.start-a.start);
  $('evn').textContent=ev.length?ev.length+' за час':'нет';
  const box=$('evlist');box.innerHTML='';
  ev.forEach(e=>{
    const d=document.createElement('div');d.className='evi';
    d.innerHTML=(e.thumb?`<img loading="lazy" src="thumb/${e.id}.jpg" alt="">`:'<img alt="">')+
      `<div class="t"><span class="dot" style="background:${evColor(e.label)}"></span><b>${evName(e)}</b><span class="muted">${e.camera}</span></div>`+
      `<div class="t muted">${fmt.format(e.start)}${e.score?' · '+Math.round(e.score*100)+' %':''}</div>`;
    d.onclick=()=>{seekAbs(e.camera,e.start-3000);window.scrollTo({top:0,behavior:'smooth'})};
    box.appendChild(d)});
}

function showCard(e,ev){
  $('cardimg').style.display=e.thumb?'block':'none';if(e.thumb)$('cardimg').src='thumb/'+e.id+'.jpg';
  const dur=e.end?Math.round((e.end-e.start)/1000)+' с':'идёт';
  $('cardt').innerHTML=`<b>${evName(e)}</b> · ${e.camera}<br>${fmt.format(e.start)} · ${dur}${e.score?' · '+Math.round(e.score*100)+' %':''}`+
    (e.zones&&e.zones.length?'<br><span class="muted">'+e.zones.join(', ')+'</span>':'');
  $('card').style.display='block';moveCard(ev);
}
function moveCard(ev){const c=$('card');let x=ev.clientX+14,y=ev.clientY-c.offsetHeight-14;if(x+210>innerWidth)x=ev.clientX-214;if(y<8)y=ev.clientY+18;c.style.left=x+'px';c.style.top=y+'px'}
function hideCard(){$('card').style.display='none'}

function timeAt(t,ev){const r=t.getBoundingClientRect();return t0+Math.min(1,Math.max(0,(ev.clientX-r.left)/r.width))*(t1-t0)}
function showHover(t,ev){const h=$('hover'),x=timeAt(t,ev);h.style.display='block';h.style.left=pct(x)+'%';h.firstChild.textContent=t.dataset.cam+' · '+fmt.format(x)}
function bindTrack(t){
  let down=false;
  t.addEventListener('pointerdown',ev=>{down=true;t.setPointerCapture(ev.pointerId);showHover(t,ev)});
  t.addEventListener('pointermove',ev=>showHover(t,ev));
  t.addEventListener('pointerup',ev=>{if(!down)return;down=false;const x=timeAt(t,ev);$('hover').style.display='none';seekAbs(t.dataset.cam,x)});
  t.addEventListener('pointerleave',()=>{if(!down)$('hover').style.display='none'});
}

function curAbs(){return cur?cur.ts+v.currentTime*1000:null}
function updateHead(){
  const h=$('head'),a=curAbs();
  if(a==null){h.style.display='none';$('osd').style.display='none';return}
  h.style.display='block';h.style.left=pct(Math.min(t1,Math.max(t0,a)))+'%';
  $('osd').style.display='block';$('osdt').textContent=fmt.format(a);$('osdc').textContent=cur.cam+' · '+cur.date+(rate!==1?' · '+rate+'×':'');
}

function seekAbs(c,t){
  const l=list(c).filter(x=>x.done);if(!l.length){st('У камеры '+c+' пока нет записанных минут');return}
  let ch=l.find(x=>t>=x.ts&&t<x.end);
  if(!ch){ch=l.find(x=>x.ts>t)||l[l.length-1];t=Math.max(ch.ts,Math.min(t,ch.end-1000))}
  cam=c;
  if(cur&&cur.cam===c&&cur.file===ch.file&&v.readyState>0){v.currentTime=Math.max(0,(t-ch.ts)/1000);v.play().catch(()=>{});updateHead();return}
  play(c,ch,(t-ch.ts)/1000);
}

function play(c,ch,offset){
  cur=Object.assign({cam:c},ch);pending=Math.max(0,offset||0);
  $('dl').href='download/'+c+'/'+ch.file;$('dl').style.display='';
  st(ch.ready||mode==='hevc'?'Загрузка…':'Готовлю минуту '+fmtHM.format(ch.ts)+'<br><span style="font-size:13px;opacity:.8">перекодирование ~10 с</span>',true);
  v.src='video/'+c+'/'+ch.file.replace('.mkv','.mp4')+(mode==='hevc'?'?mode=hevc':'');
  v.play().catch(()=>{});render();
}

v.addEventListener('loadedmetadata',()=>{v.playbackRate=rate;if(pending>0){v.currentTime=Math.min(pending,Math.max(0,v.duration-0.5));pending=0}});
v.addEventListener('playing',()=>{st('');load()});
v.addEventListener('canplay',()=>{if(/Загрузка|Готовлю/.test($('st').textContent))st('')});
v.addEventListener('timeupdate',updateHead);
v.addEventListener('ratechange',()=>{if(v.readyState>0&&v.playbackRate!==rate){rate=v.playbackRate;renderSpeeds()}});
v.addEventListener('error',()=>{if(!v.getAttribute('src'))return;st(mode==='hevc'?'Браузер не играет HEVC — переключитесь на H.264':'Не удалось открыть минуту (возможно, уже удалена)')});
v.addEventListener('ended',()=>{
  if(!$('auto').checked||!cur)return;
  const l=list(cur.cam),i=l.findIndex(x=>x.file===cur.file);
  if(i>=0&&i+1<l.length&&l[i+1].done)play(cur.cam,l[i+1],0);else st('Дальше записей пока нет');
});

function renderSpeeds(){const g=$('speeds');g.innerHTML='';SPEEDS.forEach(s=>{const b=document.createElement('button');b.textContent=s+'×';if(s===rate)b.className='on';b.onclick=()=>setRate(s);g.appendChild(b)});updateHead()}
function setRate(s){rate=s;v.playbackRate=s;if(plyr)plyr.speed=s;renderSpeeds()}
function jump(ms){const a=curAbs();if(a==null)return;seekAbs(cur.cam,a+ms)}
$('bm60').onclick=()=>jump(-60000);$('bm10').onclick=()=>jump(-10000);$('bp10').onclick=()=>jump(10000);$('bp60').onclick=()=>jump(60000);
$('bpp').onclick=()=>{if(!cur)return;v.paused?v.play():v.pause()};
$('blive').onclick=()=>{const c=cam;const last=list(c).filter(x=>x.done).pop();if(last)seekAbs(c,last.ts)};
document.addEventListener('keydown',ev=>{
  if(ev.target.tagName==='INPUT')return;
  if(ev.key==='ArrowLeft'){jump(ev.shiftKey?-60000:-10000);ev.preventDefault()}
  else if(ev.key==='ArrowRight'){jump(ev.shiftKey?60000:10000);ev.preventDefault()}
  else if(ev.key===' '&&cur){v.paused?v.play():v.pause();ev.preventDefault()}
  else if(['1','2','3','4'].includes(ev.key)){setRate(SPEEDS[+ev.key-1])}
});
function setMode(m){const a=curAbs();mode=m;$('m264').className=m==='h264'?'on':'';$('mhevc').className=m==='hevc'?'on':'';
  if(cur&&a!=null){const c=cur.cam;cur=null;seekAbs(c,a)}else render()}
$('m264').onclick=()=>setMode('h264');$('mhevc').onclick=()=>setMode('hevc');
window.addEventListener('resize',render);
renderSpeeds();load();setInterval(load,20000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "cam-archive-web"

    def log_message(self, fmt, *args):
        pass

    def send_bytes(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_file(self, path, ctype, download_name=None):
        size = os.path.getsize(path)
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        m = re.match(r"bytes=(\d*)-(\d*)$", rng or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0:
                buf = fh.read(min(262144, left))
                if not buf:
                    break
                try:
                    self.wfile.write(buf)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(buf)

    def parse_target(self, parts, ext):
        """/<раздел>/<камера>/<ГГГГММДД_ЧЧММСС><ext> → (камера, имя mkv) или None."""
        if len(parts) != 3 or not CAM_RE.match(parts[1]) or parts[1] not in cams():
            return None
        if not parts[2].endswith(ext):
            return None
        name = parts[2][: -len(ext)] + ".mkv"
        return (parts[1], name) if NAME_RE.match(name) else None

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        if not parts:
            return self.send_bytes(200, PAGE.encode(), "text/html; charset=utf-8")
        if parts == ["api", "list"]:
            res = {}
            for cam in cams():
                items = []
                lst = chunks(cam)
                for i, (f, start, size, done) in enumerate(lst):
                    loc = start.astimezone(TZ)
                    ready = os.path.exists(os.path.join(CACHE, cam, f[:-4] + ".mp4"))
                    # конец куска для таймлайна: начало следующего, у последнего — время последней записи в файл
                    if i + 1 < len(lst):
                        end = lst[i + 1][1].timestamp()
                    else:
                        try:
                            end = os.path.getmtime(os.path.join(ARCHIVE, cam, f))
                        except FileNotFoundError:
                            end = start.timestamp()
                    items.append({"file": f, "date": loc.strftime("%d.%m"), "time": loc.strftime("%H:%M:%S"),
                                  "ts": int(start.timestamp() * 1000), "end": int(end * 1000),
                                  "mb": round(size / 1048576, 1), "done": done, "ready": ready})
                res[cam] = items
            try:
                ev = json.load(open(os.path.join(EVENTS, "events.json")))
            except (FileNotFoundError, ValueError):
                ev = {"updated": None, "events": []}
            for e in ev.get("events", []):
                e["thumb"] = os.path.exists(os.path.join(EVENTS, e["id"] + ".jpg"))
            return self.send_bytes(200, json.dumps({"cameras": res, "now": int(time.time() * 1000),
                                                    "events": ev.get("events", []), "events_updated": ev.get("updated")},
                                                   ensure_ascii=False).encode(), "application/json; charset=utf-8")
        if parts[0] == "video":
            t = self.parse_target(parts, ".mp4")
            if not t:
                return self.send_bytes(404, "нет такой минуты".encode(), "text/plain; charset=utf-8")
            mode = "hevc" if parse_qs(u.query).get("mode") == ["hevc"] else "h264"
            p = ensure_mp4(t[0], t[1], mode)
            if not p:
                return self.send_bytes(404, "минута удалена или не перекодировалась".encode(), "text/plain; charset=utf-8")
            if mode == "h264" and self.headers.get("Range", "bytes=0-").startswith("bytes=0-"):
                prefetch_next(t[0], t[1])
            return self.send_file(p, "video/mp4")
        if parts[0] == "thumb" and len(parts) == 2 and parts[1].endswith(".jpg") and EVENT_ID_RE.match(parts[1][:-4]):
            p = os.path.join(EVENTS, parts[1])
            if os.path.exists(p):
                return self.send_file(p, "image/jpeg")
            return self.send_bytes(404, "нет миниатюры".encode(), "text/plain; charset=utf-8")
        if parts[0] == "download":
            t = self.parse_target(parts, ".mkv")
            src = t and os.path.join(ARCHIVE, t[0], t[1])
            if not src or not os.path.exists(src):
                return self.send_bytes(404, "нет такой минуты".encode(), "text/plain; charset=utf-8")
            return self.send_file(src, "video/x-matroska", f"{t[0]}_{t[1]}")
        return self.send_bytes(404, "не найдено".encode(), "text/plain; charset=utf-8")


if __name__ == "__main__":
    os.makedirs(CACHE, exist_ok=True)
    threading.Thread(target=cache_cleaner, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", 8080), Handler).serve_forever()
