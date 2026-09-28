#!/usr/bin/env python3
"""Бот @kulagin_flyers_bot: фото повешенной листовки → её место в метках + фото на витрину (2026-09-26).

Пользователь на месте фотографирует листовку и шлёт боту фото КАК ФАЙЛ (иначе Telegram вырезает EXIF с GPS)
с подписью «3» (№ экземпляра — серый номер в углу листовки). Если к файлу подпись не добавить (в Telegram на телефоне
при отправке «Файлом» поля подписи бывает нет) — номер можно прислать отдельным сообщением «7»: до файла, после него
(в течение 30 мин, pending.json) или ответом на уже присланный файл. Инициатива — со стороны Telegram: вебхук
https://kulagin.org<WEBHOOK_PATH> (nginx → 127.0.0.1:8093), заголовок X-Telegram-Bot-Api-Secret-Token сверяется.

Что делает на каждое фото:
1. берёт только сообщения от OWNER_ID (пока OWNER_ID пуст — отвечает писавшему его id и больше ничего);
2. скачивает файл (getFile), читает GPS из EXIF (свой разбор JPEG/TIFF — Pillow на VPS нет);
3. адрес — обратное геокодирование OpenStreetMap (Nominatim, иврит), как flyer_place.py;
4. `volunteer-admin ref-geo <№> <шир> <долг> <точность> <адрес>` — то же, что пишет flyer_place.py;
5. кладёт фото в /var/lib/flyer-bot/photos/<№>.jpg и дёргает вебхук HA (tailnet) — HA тут же забирает его
   curl'ом с https://kulagin.org/tg-photo/<PHOTO_TOKEN>/<№>.jpg в /config/www/promo/places/; после выдачи
   фото на VPS удаляется;
6. отвечает в чат: «№3 — адрес, ±N м, карта» или что не так (нет подписи/координат).
Нет GPS в снимке — фото всё равно уходит на витрину, место не трогаем, в ответе просьба прислать файлом.
"""
import json
import os
import re
import struct
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_PATH = os.environ["WEBHOOK_PATH"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
OWNER_ID = os.environ.get("OWNER_ID", "").strip()
HA_WEBHOOK_URL = os.environ.get("HA_WEBHOOK_URL", "")
PHOTO_TOKEN = os.environ["PHOTO_TOKEN"]
STATE = os.environ.get("STATE_DIRECTORY", "/var/lib/flyer-bot")
PHOTOS = os.path.join(STATE, "photos")
LOG = os.path.join(STATE, "events.log")
API = f"https://api.telegram.org/bot{TOKEN}"
MAX_FILE = 20 * 1024 * 1024          # Bot API отдаёт файлы до 20 МБ


def log(ev):
    ev["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def tg(method, **params):
    req = urllib.request.Request(f"{API}/{method}", data=json.dumps(params).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def reply(chat_id, text, reply_to=None):
    try:
        tg("sendMessage", chat_id=chat_id, text=text, disable_web_page_preview=True,
           **({"reply_parameters": {"message_id": reply_to}} if reply_to else {}))
    except Exception as e:
        log({"event": "send_error", "error": repr(e)[:200]})


# ---------- EXIF GPS без Pillow ----------
def exif_gps(data):
    """(широта, долгота, точность_м|None) из EXIF JPEG или None."""
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return None
        marker, size = data[i + 1], struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker == 0xE1 and data[i + 4:i + 10] == b"Exif\x00\x00":
            return _tiff_gps(data[i + 10:i + 2 + size])
        if marker == 0xDA:              # начались данные изображения — EXIF уже не будет
            return None
        i += 2 + size
    return None


def _tiff_gps(t):
    end = "<" if t[:2] == b"II" else ">"
    u16 = lambda o: struct.unpack(end + "H", t[o:o + 2])[0]
    u32 = lambda o: struct.unpack(end + "I", t[o:o + 4])[0]

    def ifd(off):
        out = {}
        for k in range(u16(off)):
            e = off + 2 + 12 * k
            out[u16(e)] = (u16(e + 2), u32(e + 4), e + 8)
        return out

    def rationals(entry):
        typ, cnt, voff = entry
        off = u32(voff)
        return [u32(off + 8 * n) / (u32(off + 8 * n + 4) or 1) for n in range(cnt)]

    def ascii1(entry):
        return chr(t[entry[2]])

    ifd0 = ifd(u32(4))
    if 0x8825 not in ifd0:
        return None
    g = ifd(u32(ifd0[0x8825][2]))
    if 2 not in g or 4 not in g:
        return None
    d, m, s = rationals(g[2]); lat = d + m / 60 + s / 3600
    d, m, s = rationals(g[4]); lon = d + m / 60 + s / 3600
    if 1 in g and ascii1(g[1]) == "S":
        lat = -lat
    if 3 in g and ascii1(g[3]) == "W":
        lon = -lon
    acc = rationals(g[31])[0] if 31 in g else None     # GPSHPositioningError, м
    if abs(lat) < 1e-9 and abs(lon) < 1e-9:
        return None
    return lat, lon, acc


def address(lat, lon):
    q = urllib.parse.urlencode({"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 18, "accept-language": "he"})
    req = urllib.request.Request(f"https://nominatim.openstreetmap.org/reverse?{q}",
                                 headers={"User-Agent": "kulagin.org flyer-bot (a@kulagin.org)"})
    try:
        j = json.load(urllib.request.urlopen(req, timeout=20))
        a = j.get("address", {})
        parts = [" ".join(x for x in (a.get("road"), a.get("house_number")) if x), a.get("city") or a.get("town")]
        return ", ".join(p for p in parts if p) or j.get("display_name", "")[:100]
    except Exception as e:
        log({"event": "geocode_error", "error": repr(e)[:200]})
        return f"{lat:.7f}, {lon:.7f}"


def ha_notify(payload):
    if not HA_WEBHOOK_URL:
        return
    try:
        req = urllib.request.Request(HA_WEBHOOK_URL, data=json.dumps(payload, ensure_ascii=False).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        log({"event": "ha_webhook_error", "error": repr(e)[:200]})


def handle(msg):
    chat, frm, mid = msg["chat"]["id"], str(msg.get("from", {}).get("id", "")), msg.get("message_id")
    if not OWNER_ID:
        reply(chat, f"Бот ещё не привязан к хозяину. Ваш Telegram id: {frm}", mid)
        log({"event": "unbound", "from": frm})
        return
    if frm != OWNER_ID:
        log({"event": "foreign", "from": frm})
        return
    doc, photo = msg.get("document"), msg.get("photo")
    if not doc and not photo:
        text = msg.get("text") or ""
        if text.startswith("/start"):
            reply(chat, "Пришлите фото повешенной листовки КАК ФАЙЛ (скрепка → Файл) и её номер — в подписи "
                        "или отдельным сообщением (до или после файла), например 3.")
            return
        num = re.fullmatch(r"\s*(?:№|#|n)?\s*(\d+)\s*", text, re.I)
        if not num:
            return
        serial = int(num.group(1))
        # ответом на уже присланный файл — берём файл из того сообщения
        orig = msg.get("reply_to_message") or {}
        if orig.get("document") or orig.get("photo"):
            f = _file_of(orig)
            if f:
                process(chat, orig.get("message_id"), f[0], f[1], serial)
            else:
                reply(chat, "Это не картинка — пришлите фото листовки.", mid)
            return
        p = pending_swap("file", "serial", {"n": serial})
        if p:
            process(chat, p["mid"], p["fid"], p["as_file"], serial)
        else:
            reply(chat, f"№{serial} — жду файл с фото.", mid)
        return
    f = _file_of(msg)
    if not f:
        reply(chat, "Это не картинка — пришлите фото листовки.", mid)
        return
    serial = re.search(r"\d+", msg.get("caption") or "")
    if serial:
        pending_swap("serial")                           # подпись главнее — старый ждущий номер сбрасываем
        serial = int(serial.group())
    else:
        p = pending_swap("serial", "file", {"fid": f[0], "as_file": f[1], "mid": mid})
        if not p:
            reply(chat, "Файл получен. Какой это номер? Напишите цифру отдельным сообщением.", mid)
            return
        serial = p["n"]
    process(chat, mid, f[0], f[1], serial)


def _file_of(msg):
    """(file_id, как_файл) картинки из сообщения или None."""
    doc, photo = msg.get("document"), msg.get("photo")
    if doc:
        return (doc["file_id"], True) if (doc.get("mime_type") or "").startswith("image/") else None
    if photo:
        return max(photo, key=lambda p: p.get("file_size", 0))["file_id"], False
    return None


# ---------- номер и файл по отдельности: ждём вторую половину PENDING_TTL секунд ----------
PENDING = os.path.join(STATE, "pending.json")
PENDING_TTL = 30 * 60
_lock = threading.Lock()


def _pending_load():
    try:
        with open(PENDING, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def pending_swap(take, put=None, value=None):
    """Под одним замком: забрать свежую половину `take`, а если её нет — оставить свою `put`=value.
    Атомарно — номер и файл, пришедшие почти одновременно (разные потоки), не разминутся."""
    with _lock:
        d = _pending_load()
        p = d.pop(take, None)
        if p and time.time() - p.get("ts", 0) >= PENDING_TTL:
            p = None
        if p:
            d.pop(put, None)
        elif put:
            d[put] = dict(value, ts=time.time())
        with open(PENDING, "w", encoding="utf-8") as f:
            json.dump(d, f)
        return p


def process(chat, mid, fid, as_file, serial):
    doc = as_file
    info = tg("getFile", file_id=fid)["result"]
    if info.get("file_size", 0) > MAX_FILE:
        reply(chat, "Файл больше 20 МБ — Telegram не отдаёт такие ботам. Пришлите поменьше.", mid)
        return
    data = urllib.request.urlopen(f"https://api.telegram.org/file/bot{TOKEN}/{info['file_path']}", timeout=60).read()
    os.makedirs(PHOTOS, exist_ok=True)
    with open(os.path.join(PHOTOS, f"{serial}.jpg"), "wb") as f:
        f.write(data)
    gps = exif_gps(data)
    ev = {"event": "photo", "serial": serial, "bytes": len(data), "as_file": bool(doc), "gps": bool(gps)}
    if not gps:
        log(ev)
        ha_notify({"serial": serial, "photo": True, "geo": False})
        why = "Telegram сжал фото и вырезал координаты — пришлите его КАК ФАЙЛ." if not doc else \
              "в снимке нет координат — включите в камере «Теги местоположения»."
        reply(chat, f"№{serial}: фото сохранено, но место не записано: {why}", mid)
        return
    lat, lon, acc = gps
    addr = address(lat, lon)
    r = subprocess.run(["/usr/local/sbin/volunteer-admin", "ref-geo", str(serial), f"{lat:.7f}", f"{lon:.7f}",
                        f"{acc if acc is not None else 0:.4f}", addr], capture_output=True, text=True, timeout=30,
                       # без STATE_DIRECTORY: systemd даёт боту свой (/var/lib/flyer-bot), и volunteer-admin
                       # искал бы метки там, в пустоте, а не в /var/lib/volunteer-book (28.09, №7)
                       env={k: v for k, v in os.environ.items() if k != "STATE_DIRECTORY"})
    ok = r.returncode == 0
    ev.update({"lat": round(lat, 7), "lon": round(lon, 7), "acc": acc, "addr": addr, "ref_geo": ok})
    log(ev)
    ha_notify({"serial": serial, "photo": True, "geo": ok, "addr": addr})
    osm = f"https://www.openstreetmap.org/?mlat={lat:.7f}&mlon={lon:.7f}#map=19/{lat:.7f}/{lon:.7f}"
    acc_s = f"±{acc:.4f} м" if acc is not None else "точность не указана в снимке"
    if ok:
        reply(chat, f"✅ №{serial} — {addr}\n{acc_s}\n{osm}", mid)
    else:
        reply(chat, f"№{serial}: координаты есть ({addr}), но записать место не вышло — нет такой листовки? "
                    f"{(r.stdout or r.stderr).strip()[:200]}", mid)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        if self.path != WEBHOOK_PATH or self.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            self.send_response(404); self.end_headers(); return
        body = self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 1_000_000))
        self.send_response(200); self.end_headers()          # Telegram ждёт ответ быстро — работу делаем в потоке
        try:
            upd = json.loads(body)
        except ValueError:
            return
        msg = upd.get("message") or upd.get("edited_message")
        if msg:
            threading.Thread(target=self._safe, args=(msg,), daemon=True).start()

    @staticmethod
    def _safe(msg):
        try:
            handle(msg)
        except Exception as e:
            log({"event": "error", "error": repr(e)[:300]})
            try:
                reply(msg["chat"]["id"], f"Ошибка: {repr(e)[:200]}")
            except Exception:
                pass

    def do_GET(self):
        # /tg-photo/<PHOTO_TOKEN>/<№>.jpg — забирает HA; после выдачи файл удаляется
        m = re.fullmatch(r"/tg-photo/([\w-]+)/(\d+)\.jpg", self.path)
        if not m or m.group(1) != PHOTO_TOKEN:
            self.send_response(404); self.end_headers(); return
        p = os.path.join(PHOTOS, f"{m.group(2)}.jpg")
        if not os.path.exists(p):
            self.send_response(404); self.end_headers(); return
        data = open(p, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg"); self.send_header("Content-Length", str(len(data)))
        self.end_headers(); self.wfile.write(data)
        os.remove(p)
        log({"event": "photo_taken", "serial": int(m.group(2))})


if __name__ == "__main__":
    os.makedirs(PHOTOS, exist_ok=True)
    ThreadingHTTPServer(("127.0.0.1", 8093), H).serve_forever()
