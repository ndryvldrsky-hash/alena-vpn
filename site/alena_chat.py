#!/usr/bin/env python3
"""Чат с Алёной на kulagin.org (2026-09-22).

Посетитель сайта разговаривает с «Алёной» — тем же характером и знанием проекта, что у агента, который
присматривает за домом, но это отдельный экземпляр на Claude API без каких-либо инструментов и без
доступа внутрь дома: он только объясняет суть идеи и собирает контакт. Суть идеи — в prompt.md рядом.

Как устроено:
  POST /api/chat        {session, lang, message} → ответ потоком SSE: data: {"t": "кусок"} … data: {"done": true}
  POST /api/chat/end    {session} → диалог завершён (sendBeacon при уходе со страницы), сразу передать Андрею
  GET  /api/chat/health → ok (для сторожа)
История диалога хранится здесь, в памяти, по id сессии (браузер присылает только новую реплику) — так клиент
не может подсунуть чужую историю или чужой system prompt. Сессии, молчащие дольше IDLE_FINALIZE, закрываются:
Claude делает короткую сводку по-русски (что хотел человек, какой контакт оставил), она уходит в вебхук HA
(HA_WEBHOOK_URL из /etc/alena-chat/env) → автоматизация site_chat_handoff шлёт push и письмо.

Защита публичной точки: лимит реплик на IP в час и в сутки, общий суточный потолок, длина реплики, глубина
истории. Ничего не хранит о посетителе, кроме соли+sha256 от IP (только для лимитов), языка и текста диалога.
Учёт: каждая реплика — строка в /var/lib/alena-chat/log/<дата>.jsonl с usage и ценой; сводка — stats.json
(её читает /usr/local/sbin/chat-status → сенсор HA на вкладке «Чат на сайте»).

Слушает только 127.0.0.1:8090; наружу — nginx (location /api/chat в конфиге kulagin.org, без буферизации).
Исходник — репозиторий alena-vpn (site/alena_chat.py), на VPS — /usr/local/lib/alena-chat/app.py (служба alena-chat,
venv /usr/local/lib/alena-chat/venv с пакетом anthropic).
"""
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from zoneinfo import ZoneInfo

import anthropic

MODEL = "claude-opus-5"
# цена claude-opus-5, $ за 1M токенов: вход, выход, чтение кеша (0,1×), запись кеша (1,25×)
PRICE_IN, PRICE_OUT, PRICE_CACHE_READ, PRICE_CACHE_WRITE = 5.0, 25.0, 0.5, 6.25
MAX_TOKENS = 1024            # виджет чата — короткие ответы, длиннее не нужно
EFFORT = "low"               # для беседы хватает; глубже — дороже и медленнее без пользы
STATE = os.environ.get("STATE_DIRECTORY", "/var/lib/alena-chat")
PROMPT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt.md")
HA_WEBHOOK_URL = os.environ.get("HA_WEBHOOK_URL", "")
PORT = int(os.environ.get("PORT", "8090"))
TZ = ZoneInfo("Asia/Jerusalem")

# лимиты публичной точки
PER_IP_HOUR, PER_IP_DAY, GLOBAL_DAY = 30, 80, 400
MAX_MSG_CHARS = 1500
MAX_HISTORY = 24             # реплик в истории (12 пар); старше — отбрасываются
IDLE_FINALIZE = 15 * 60      # с; молчание, после которого диалог считается законченным
SESSION_TTL = 6 * 3600       # с; пустые сессии (без реплик) забываем

LANG_NAMES = {"ru": "русский", "en": "английский", "he": "иврит", "ar": "арабский", "fr": "французский"}
REFUSAL_TEXT = {
    "ru": "На это я ответить не могу. Давайте вернёмся к вашей задаче по дому — камерам, сети, умному дому.",
    "en": "I can't help with that. Let's get back to your home project — cameras, network, smart home.",
    "he": "בזה אני לא יכולה לעזור. נחזור לפרויקט הבית שלכם — מצלמות, רשת, בית חכם.",
    "ar": "لا أستطيع المساعدة في ذلك. لنعد إلى مشروع منزلكم — الكاميرات والشبكة والمنزل الذكي.",
    "fr": "Je ne peux pas répondre à cela. Revenons à votre projet — caméras, réseau, maison connectée.",
}
BUSY_TEXT = {
    "ru": "Сейчас слишком много обращений, попробуйте чуть позже — или напишите Андрею напрямую в Telegram @kulagin_org.",
    "en": "Too many requests right now — please try again a bit later, or message Andrey directly on Telegram @kulagin_org.",
    "he": "יותר מדי פניות כרגע — נסו שוב מעט מאוחר יותר או כתבו לאנדריי ישירות בטלגרם @kulagin_org.",
    "ar": "هناك طلبات كثيرة الآن — حاولوا لاحقًا أو راسلوا أندريه مباشرة على تيليغرام @kulagin_org.",
    "fr": "Trop de demandes en ce moment — réessayez un peu plus tard, ou écrivez à Andreï sur Telegram @kulagin_org.",
}

client = anthropic.Anthropic()          # ключ — ANTHROPIC_API_KEY из /etc/alena-chat/env
SYSTEM_PROMPT = open(PROMPT_FILE, encoding="utf-8").read()
IP_SALT = secrets.token_hex(8)          # соль на время жизни процесса: хеши IP не сопоставимы между рестартами

_lock = threading.Lock()
_sessions = {}                           # id → сессия
_ip_hits = {}                            # хеш IP → [моменты реплик]
_global_hits = []                        # моменты всех реплик за сутки
_stats = None


# ---------- учёт ----------
def _stats_path():
    return os.path.join(STATE, "stats.json")


def _load_stats():
    global _stats
    try:
        _stats = json.load(open(_stats_path(), encoding="utf-8"))
    except Exception:
        _stats = {"today": {}, "total": {"messages": 0, "sessions": 0, "cost_usd": 0.0}, "last": []}
    _roll_day()


def _roll_day():
    """Суточные счётчики обнуляются по израильскому дню."""
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    if _stats["today"].get("date") != today:
        _stats["today"] = {"date": today, "messages": 0, "sessions": 0, "cost_usd": 0.0, "refusals": 0, "errors": 0}


def _save_stats():
    _roll_day()
    _stats["updated"] = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    tmp = _stats_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_stats, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _stats_path())


def _log(rec):
    rec["ts"] = datetime.now(TZ).isoformat(timespec="seconds")
    d = os.path.join(STATE, "log")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, datetime.now(TZ).strftime("%Y-%m-%d") + ".jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _cost(u):
    """Цена ответа по usage, $."""
    return (getattr(u, "input_tokens", 0) * PRICE_IN + getattr(u, "output_tokens", 0) * PRICE_OUT
            + (getattr(u, "cache_read_input_tokens", 0) or 0) * PRICE_CACHE_READ
            + (getattr(u, "cache_creation_input_tokens", 0) or 0) * PRICE_CACHE_WRITE) / 1e6


def _count(kind, cost=0.0):
    with _lock:
        _roll_day()
        _stats["today"][kind] = _stats["today"].get(kind, 0) + 1
        _stats["total"][kind] = _stats["total"].get(kind, 0) + 1
        if cost:
            _stats["today"]["cost_usd"] = round(_stats["today"].get("cost_usd", 0) + cost, 5)
            _stats["total"]["cost_usd"] = round(_stats["total"].get("cost_usd", 0) + cost, 5)
        _save_stats()


# ---------- лимиты ----------
def _ip_hash(ip):
    return hashlib.sha256((IP_SALT + ip).encode()).hexdigest()[:16]


def _allowed(iph):
    now = time.time()
    with _lock:
        hits = [t for t in _ip_hits.get(iph, []) if now - t < 86400]
        _ip_hits[iph] = hits
        _global_hits[:] = [t for t in _global_hits if now - t < 86400]
        if len(_global_hits) >= GLOBAL_DAY:
            return False
        if len(hits) >= PER_IP_DAY or sum(1 for t in hits if now - t < 3600) >= PER_IP_HOUR:
            return False
        hits.append(now)
        _global_hits.append(now)
        return True


# ---------- Claude ----------
def _system(lang):
    """Первый блок — постоянный (кешируется на час), второй — язык беседы, меняется от сессии к сессии."""
    return [
        {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral", "ttl": "1h"}},
        {"type": "text", "text": f"Язык интерфейса сайта у этого посетителя: {LANG_NAMES.get(lang, 'русский')}. "
                                 "Отвечай на том языке, на котором пишет посетитель; если он ещё ничего не написал "
                                 "по-человечески (например, только «привет»), — на языке интерфейса."},
    ]


def _stream_reply(sess, send):
    """Потоковый ответ Claude; send(кусок) вызывается по мере генерации. Возвращает (текст, usage, stop_reason)."""
    text = []
    with client.messages.stream(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=_system(sess["lang"]),
        messages=sess["messages"],
        output_config={"effort": EFFORT},
        # серверный запасной маршрут на случай отказа по политике — ответ придёт от другой модели вместо тишины
        extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
        extra_body={"fallbacks": "default"},
    ) as stream:
        for chunk in stream.text_stream:
            text.append(chunk)
            send(chunk)
        final = stream.get_final_message()
    return "".join(text), final.usage, final.stop_reason


DIALOGS = os.path.join(STATE, "dialogs")   # полный текст каждого законченного диалога (+ перевод), для вкладки «Чат на сайте»


def needs_translation(text):
    """Нужен ли перевод на русский: заметная доля не-кириллических букв."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    return sum(1 for c in letters if "\u0400" <= c <= "\u04ff") / len(letters) < 0.7


def translate_ru(transcript):
    """Перевод диалога на русский (решение пользователя 22.09: переведённый вариант нужен сразу — в письмо,
    уведомление HA и на вкладку). Маркеры 👤/🤖 и разбиение на реплики сохраняются."""
    try:
        r = client.messages.create(
            model=MODEL, max_tokens=4000, output_config={"effort": "low"},
            system="Переведи диалог на русский язык, естественно и близко к смыслу. Сохрани построчно маркеры 👤 (посетитель) "
                   "и 🤖 (Алёна) и разбиение на реплики; имена, адреса, телефоны, названия брендов и приложений не переводить. "
                   "Верни только перевод, без пояснений.",
            messages=[{"role": "user", "content": transcript[:12000]}],
        )
        return next(b.text for b in r.content if b.type == "text"), _cost(r.usage)
    except Exception as e:
        return f"(перевод не удался: {type(e).__name__})", 0.0


SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "2–3 предложения по-русски: кто, что хочет, что важно"},
        "contact": {"type": "string", "description": "контакт, который оставил посетитель (Telegram/телефон/почта/имя), или пустая строка"},
        "intent": {"type": "string", "enum": ["заявка", "вопрос", "просто поговорить", "спам"]},
    },
    "required": ["summary", "contact", "intent"],
    "additionalProperties": False,
}


def _summarize(sess):
    transcript = "\n".join(f"{'Посетитель' if m['role'] == 'user' else 'Алёна'}: {m['content']}" for m in sess["messages"])
    try:
        r = client.messages.create(
            model=MODEL, max_tokens=600, output_config={"effort": "low", "format": {"type": "json_schema", "schema": SUMMARY_SCHEMA}},
            system="Ты составляешь для Андрея (владельца kulagin.org) сводку диалога посетителя сайта с его ИИ-ассистенткой Алёной. "
                   "По-русски, коротко, по делу. Контакт — только если посетитель его реально написал.",
            messages=[{"role": "user", "content": transcript[:12000]}],
        )
        txt = next(b.text for b in r.content if b.type == "text")
        out = json.loads(txt)
        out["cost_usd"] = _cost(r.usage)
        return out
    except Exception as e:  # без сводки заявка всё равно уйдёт — с сырым текстом
        return {"summary": f"(сводка не удалась: {type(e).__name__}) " + transcript[:600], "contact": "", "intent": "вопрос", "cost_usd": 0.0}


def _handoff(sess, reason):
    """Передать законченный диалог в HA (вебхук → push + письмо)."""
    if not any(m["role"] == "user" for m in sess["messages"]):
        return
    summ = _summarize(sess)
    transcript = "\n".join(f"{'👤' if m['role'] == 'user' else '🤖'} {m['content']}" for m in sess["messages"])
    payload = {
        "session": sess["id"], "lang": sess["lang"], "reason": reason,
        "started": datetime.fromtimestamp(sess["started"], TZ).strftime("%d.%m %H:%M"),
        "turns": sum(1 for m in sess["messages"] if m["role"] == "user"),
        "summary": summ["summary"], "contact": summ["contact"], "intent": summ["intent"],
        "transcript": transcript[:3500], "cost_usd": round(sess["cost"] + summ["cost_usd"], 4),
    }
    # перевод на русский, если диалог не по-русски — сразу, чтобы был в письме, уведомлении HA и на вкладке
    ru, ru_cost = (translate_ru(transcript) if needs_translation(transcript) else (None, 0.0))
    summ["cost_usd"] += ru_cost
    payload["transcript_ru"] = ru[:3500] if ru else None
    payload["cost_usd"] = round(sess["cost"] + summ["cost_usd"], 4)
    # файл диалога для дашборда «май» (вкладка «Чат на сайте» → «Показать диалог»)
    try:
        os.makedirs(DIALOGS, exist_ok=True)
        with open(os.path.join(DIALOGS, re.sub(r"[^A-Za-z0-9_.-]", "_", sess["id"])[:80] + ".json"), "w", encoding="utf-8") as f:
            json.dump({**payload, "transcript": transcript, "transcript_ru": ru, "ended": datetime.now(TZ).strftime("%d.%m %H:%M")},
                      f, ensure_ascii=False, indent=1)
    except Exception as e:
        _log({"event": "dialog_save_error", "session": sess["id"], "error": repr(e)})
    ok = False
    if HA_WEBHOOK_URL:
        try:
            req = urllib.request.Request(HA_WEBHOOK_URL, data=json.dumps(payload, ensure_ascii=False).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=15)
            ok = True
        except Exception as e:
            payload["error"] = repr(e)
    _log({"event": "handoff", "ok": ok, **payload})
    with _lock:
        _roll_day()
        if summ["cost_usd"]:
            _stats["today"]["cost_usd"] = round(_stats["today"].get("cost_usd", 0) + summ["cost_usd"], 5)
            _stats["total"]["cost_usd"] = round(_stats["total"].get("cost_usd", 0) + summ["cost_usd"], 5)
        _stats["last"] = ([{"session": sess["id"], "started": payload["started"], "lang": sess["lang"], "turns": payload["turns"],
                            "intent": summ["intent"], "summary": summ["summary"], "contact": summ["contact"],
                            "handoff": "ok" if ok else "ошибка"}] + _stats.get("last", []))[:10]
        _save_stats()


def _finalizer():
    """Раз в минуту закрывает замолчавшие диалоги и забывает пустые сессии."""
    while True:
        time.sleep(60)
        now = time.time()
        done = []
        with _lock:
            for sid, s in list(_sessions.items()):
                if now - s["last"] > IDLE_FINALIZE and any(m["role"] == "user" for m in s["messages"]):
                    done.append(_sessions.pop(sid))
                elif now - s["last"] > SESSION_TTL:
                    _sessions.pop(sid)
        for s in done:
            try:
                _handoff(s, "idle")
            except Exception as e:
                _log({"event": "handoff_error", "session": s["id"], "error": repr(e)})


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "alena-chat/1"

    def log_message(self, fmt, *args):  # свой учёт есть, journal не засоряем
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 16384:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def _ip(self):
        return self.headers.get("X-Real-IP") or self.client_address[0]

    def do_GET(self):
        if self.path == "/api/chat/health":
            with _lock:
                n = len(_sessions)
            return self._json(200, {"ok": True, "sessions": n})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        try:
            data = self._body()
        except Exception:
            return self._json(400, {"error": "bad json"})
        if self.path == "/api/chat/end":
            with _lock:
                s = _sessions.pop(str(data.get("session", ""))[:64], None)
            self._json(200, {"ok": True})
            if s:
                _handoff(s, "closed")
            return
        if self.path != "/api/chat":
            return self._json(404, {"error": "not found"})

        sid = str(data.get("session", ""))[:64]
        lang = str(data.get("lang", "ru"))[:2]
        msg = str(data.get("message", "")).strip()
        if not sid or not msg:
            return self._json(400, {"error": "empty"})
        if lang not in LANG_NAMES:
            lang = "ru"
        msg = msg[:MAX_MSG_CHARS]
        iph = _ip_hash(self._ip())
        if not _allowed(iph):
            return self._json(429, {"error": BUSY_TEXT[lang]})

        # история из браузера принимается только для НОВОЙ сессии (после рестарта службы или закрытия по idle,
        # когда посетитель вернулся на страницу) — это его же реплики, которые он и так мог бы набрать заново
        history = []
        for m in (data.get("history") or [])[-MAX_HISTORY:]:
            if isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str) and m["content"].strip():
                if not history or history[-1]["role"] != m["role"]:
                    history.append({"role": m["role"], "content": m["content"].strip()[:MAX_MSG_CHARS * 2]})
        while history and history[0]["role"] != "user":
            history.pop(0)
        if history and history[-1]["role"] == "user":
            history.pop()

        with _lock:
            sess = _sessions.get(sid)
            if sess is None:
                sess = _sessions[sid] = {"id": sid, "lang": lang, "ip": iph, "started": time.time(), "last": time.time(),
                                         "messages": history, "cost": 0.0}
                _roll_day()
                _stats["today"]["sessions"] = _stats["today"].get("sessions", 0) + 1
                _stats["total"]["sessions"] = _stats["total"].get("sessions", 0) + 1
            sess["lang"] = lang
            sess["last"] = time.time()
            sess["messages"].append({"role": "user", "content": msg})
            sess["messages"] = sess["messages"][-MAX_HISTORY:]
            if sess["messages"][0]["role"] != "user":   # история должна начинаться с реплики посетителя
                sess["messages"] = sess["messages"][1:]
            snapshot = {**sess, "messages": list(sess["messages"])}

        # поток SSE
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()

        def send(chunk):
            self.wfile.write(("data: " + json.dumps({"t": chunk}, ensure_ascii=False) + "\n\n").encode())
            self.wfile.flush()

        try:
            text, usage, stop = _stream_reply(snapshot, send)
            cost = _cost(usage)
            if stop == "refusal":
                text = REFUSAL_TEXT[lang]
                send(text)
                _count("refusals")
            elif stop == "max_tokens":
                send(" …")
            with _lock:
                sess["messages"].append({"role": "assistant", "content": text})
                sess["cost"] += cost
                sess["last"] = time.time()
            _count("messages", cost)
            _log({"event": "turn", "session": sid, "lang": lang, "ip": iph, "user": msg, "assistant": text, "stop": stop,
                  "usage": {"in": usage.input_tokens, "out": usage.output_tokens,
                            "cache_read": usage.cache_read_input_tokens or 0, "cache_write": usage.cache_creation_input_tokens or 0},
                  "cost_usd": round(cost, 5)})
            self.wfile.write(("data: " + json.dumps({"done": True}) + "\n\n").encode())
        except anthropic.RateLimitError:
            self.wfile.write(("data: " + json.dumps({"error": BUSY_TEXT[lang]}, ensure_ascii=False) + "\n\n").encode())
            _count("errors")
        except Exception as e:
            _count("errors")
            _log({"event": "error", "session": sid, "error": repr(e)})
            with _lock:   # реплика без ответа — убрать, чтобы история не начиналась с двух user подряд
                if sess["messages"] and sess["messages"][-1]["role"] == "user":
                    sess["messages"].pop()
            try:
                self.wfile.write(("data: " + json.dumps({"error": BUSY_TEXT[lang]}, ensure_ascii=False) + "\n\n").encode())
            except Exception:
                pass


if __name__ == "__main__":
    os.makedirs(STATE, exist_ok=True)
    _load_stats()
    threading.Thread(target=_finalizer, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
