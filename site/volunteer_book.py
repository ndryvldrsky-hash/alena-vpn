#!/usr/bin/env python3
"""Запись на волонтёрскую встречу — v.kulagin.org (2026-09-26).

Андрей сейчас на пособии по безработице и не может брать деньги, поэтому помогает бесплатно: соседи сами выбирают
время встречи (удалённо или визит), а служба держит расписание и заявки. Только stdlib — venv не нужен.

  GET  /api/v/slots  → {"tz", "slot_minutes", "slots": ["2026-09-28T10:00", ...]}  — только свободные слоты
  GET  /api/v/ref?r= → {"lang": "he"} — язык страницы для метки; заодно засчитывается переход (событие visit)
  POST /api/v/book   ← {"slot", "name", "contact", "format": "remote"|"visit", "area", "topic", "lang", "r", "website"}
                     → {"ok": true, "id": "..."} | {"ok": false, "error": "..."}
«r» — непрозрачная метка из ссылки (v.kulagin.org/<метка>), 2026-09-26: 5 символов у объявлений, 7 — у QR (у каждого
экземпляра листовки своя). Что за ней (источник, язык объявления, № экземпляра, пометка) знает только таблица
$STATE_DIRECTORY/refs.json — её ведёт volunteer-admin ref-new; наружу метка ничего не раскрывает, в заявку сервер
кладёт расшифровку сам. «lang» — язык страницы, на котором заполнена форма.
«me» — ключ хозяина (OWNER_TOKEN) из браузера, помеченного ссылкой ?me=<ключ>: переход пишется как visit_owner,
заявка получает owner: true — в статистику меток не идут (2026-09-26). GET /api/v/me?me= → {"owner": true|false}.
«website» — ловушка для ботов (поле скрыто на странице): заполнено → делаем вид, что записали, и молчим.

Расписание — /etc/volunteer-book/schedule.json (дни недели → времена начала, горизонт записи, минимальный запас
до встречи, закрытые даты и слоты). Заявки — $STATE_DIRECTORY/bookings.json; через 30 дней после встречи
удаляются целиком (данные людей не копим). Новая заявка → вебхук HA (HA_WEBHOOK_URL, по tailnet) →
автоматизация volunteer_booking: push на телефоны, уведомление и письмо. Заявка — это просьба о встрече:
слот сразу закрывается, Андрей подтверждает лично (volunteer-admin confirm/cancel на VPS или кнопки на витрине).
"""
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

PORT = int(os.environ.get("PORT", "8091"))
STATE = os.environ.get("STATE_DIRECTORY", "/var/lib/volunteer-book")
SCHEDULE = os.environ.get("SCHEDULE", "/etc/volunteer-book/schedule.json")
HA_WEBHOOK_URL = os.environ.get("HA_WEBHOOK_URL", "")
OWNER_TOKEN = os.environ.get("OWNER_TOKEN", "")   # браузер хозяина: переходы и заявки не в статистику
BOOKINGS = os.path.join(STATE, "bookings.json")
LOG = os.path.join(STATE, "events.log")
REFS = os.path.join(STATE, "refs.json")
REF_RE = re.compile(r"^[a-z0-9]{5,8}$")
KEEP_DAYS = 30                 # заявки хранятся столько дней после встречи
PER_IP_DAY = 3                 # заявок с одного адреса за сутки
LOCK = threading.Lock()
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _log(ev):
    ev["t"] = dt.datetime.now().isoformat(timespec="seconds")
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except OSError:
        pass


def schedule():
    with open(SCHEDULE, encoding="utf-8") as f:
        return json.load(f)


def load():
    try:
        with open(BOOKINGS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save(items):
    tmp = BOOKINGS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    os.replace(tmp, BOOKINGS)


def prune(items, tz):
    cutoff = dt.datetime.now(tz) - dt.timedelta(days=KEEP_DAYS)
    return [b for b in items if dt.datetime.fromisoformat(b["slot"]).replace(tzinfo=tz) > cutoff]


def ref_info(r):
    """Расшифровка метки из refs.json или None (неизвестная метка — как прямой заход)."""
    r = str(r or "").lower()
    if not REF_RE.match(r):
        return None
    try:
        with open(REFS, encoding="utf-8") as f:
            return json.load(f).get(r)
    except (OSError, ValueError):
        return None


def is_owner(token):
    return bool(OWNER_TOKEN) and secrets.compare_digest(str(token or ""), OWNER_TOKEN)


def free_slots(sch, items):
    tz = ZoneInfo(sch.get("tz", "Asia/Jerusalem"))
    now = dt.datetime.now(tz)
    first = now + dt.timedelta(hours=sch.get("min_notice_hours", 20))
    taken = {b["slot"] for b in items if b.get("status") != "cancelled"}
    blocked_days = set(sch.get("blocked_dates", []))
    blocked = set(sch.get("blocked_slots", []))
    out = []
    for d in range(sch.get("days_ahead", 14) + 1):
        day = (now + dt.timedelta(days=d)).date()
        if day.isoformat() in blocked_days:
            continue
        for hm in sch.get("weekly", {}).get(DAYS[day.weekday()], []):
            h, m = map(int, hm.split(":"))
            start = dt.datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
            key = start.strftime("%Y-%m-%dT%H:%M")
            if start >= first and key not in taken and key not in blocked:
                out.append(key)
    return out


def notify(b):
    if not HA_WEBHOOK_URL:
        return
    try:
        req = urllib.request.Request(HA_WEBHOOK_URL, data=json.dumps(b, ensure_ascii=False).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:  # заявка уже сохранена — потерять можно только уведомление
        _log({"event": "webhook_error", "id": b["id"], "error": repr(e)[:200]})


def clean(s, n):
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", str(s or "")).strip()
    return s[:n]


class H(BaseHTTPRequestHandler):
    server_version = "volunteer-book"

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

    def do_GET(self):
        path, _, qs = self.path.partition("?")
        q = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
        if path == "/api/v/me":
            return self._json(200, {"owner": is_owner(q.get("me"))})
        if path == "/api/v/ref":
            r = q.get("r", "").lower()
            info = ref_info(r)
            if info:
                ip = self.headers.get("X-Real-IP", self.client_address[0])
                _log({"event": "visit_owner" if is_owner(q.get("me")) else "visit", "r": r,
                      "ip": hashlib.sha256(("vb" + ip).encode()).hexdigest()[:12]})
            return self._json(200, {"lang": (info or {}).get("ui_lang", "")})
        if path != "/api/v/slots":
            return self._json(404, {"ok": False})
        sch = schedule()
        with LOCK:
            items = load()
        self._json(200, {"tz": sch.get("tz"), "slot_minutes": sch.get("slot_minutes", 90),
                         "slots": free_slots(sch, items)})

    def do_POST(self):
        if self.path != "/api/v/book":
            return self._json(404, {"ok": False})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n > 8000:
                return self._json(413, {"ok": False, "error": "too_big"})
            d = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json(400, {"ok": False, "error": "bad_json"})
        ip = self.headers.get("X-Real-IP", self.client_address[0])
        iph = hashlib.sha256(("vb" + ip).encode()).hexdigest()[:12]
        if d.get("website"):                        # ловушка для ботов
            _log({"event": "honeypot", "ip": iph})
            return self._json(200, {"ok": True, "id": "x" + secrets.token_hex(3)})
        name, contact = clean(d.get("name"), 80), clean(d.get("contact"), 120)
        topic, area = clean(d.get("topic"), 1500), clean(d.get("area"), 120)
        fmt = d.get("format") if d.get("format") in ("remote", "visit") else "remote"
        lang = clean(d.get("lang"), 5) or "ru"
        r = str(d.get("r") or "").lower()
        info = ref_info(r) or {}
        r = r if info else ""
        slot = clean(d.get("slot"), 16)
        if not name or len(contact) < 5 or len(topic) < 5:
            return self._json(400, {"ok": False, "error": "fields"})
        if fmt == "visit" and not area:
            return self._json(400, {"ok": False, "error": "area"})
        sch = schedule()
        tz = ZoneInfo(sch.get("tz", "Asia/Jerusalem"))
        with LOCK:
            items = prune(load(), tz)
            day_ago = (dt.datetime.now(tz) - dt.timedelta(days=1)).isoformat()
            if sum(1 for b in items if b.get("ip") == iph and b["created"] > day_ago) >= PER_IP_DAY:
                return self._json(429, {"ok": False, "error": "limit"})
            if slot not in free_slots(sch, items):
                return self._json(409, {"ok": False, "error": "slot_taken"})
            b = {"id": secrets.token_hex(3), "slot": slot, "slot_minutes": sch.get("slot_minutes", 90),
                 "created": dt.datetime.now(tz).isoformat(timespec="seconds"), "status": "new",
                 "name": name, "contact": contact, "format": fmt, "area": area, "topic": topic,
                 "lang": lang, "r": r, "src": info.get("src", ""), "ad_lang": info.get("ad_lang", ""),
                 "serial": info.get("serial"), "note": info.get("note", ""), "owner": is_owner(d.get("me")), "ip": iph}
            items.append(b)
            save(items)
        _log({"event": "booked", "id": b["id"], "slot": slot, "format": fmt, "lang": lang, "r": r})
        threading.Thread(target=notify, args=({k: v for k, v in b.items() if k != "ip"},), daemon=True).start()
        self._json(200, {"ok": True, "id": b["id"]})


if __name__ == "__main__":
    os.makedirs(STATE, exist_ok=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
