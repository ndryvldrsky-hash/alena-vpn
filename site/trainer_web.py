#!/usr/bin/env python3
"""Тренажёр чтения иврита для телефона (30.09.2026) — то же, что окно на XPS (тренажёр_чтения.py), но в браузере.

Страница открывается на Витрине «Тренажёр иврита» (Май, карточка iframe с разрешением на микрофон) или отдельно.
  GET  /trainer/<SECRET>/                — страница (trainer_web.html рядом)
  GET  /trainer/<SECRET>/phrases         — фразы (trainer_phrases.json, кладёт аддон)
  GET  /trainer/<SECRET>/sample?i=N&fast=1 — образец произношения: Gemini TTS (Charon), кэш на диске
  POST /trainer/<SECRET>/judge?i=N       — WAV 16 кГц моно с телефона → Gemini оценивает → JSON; строка в журнал
  GET  /trainer/<SECRET>/log             — журнал попыток (JSONL) — аддон забирает для Витрины
Ключа Google здесь нет: аддон каждые 40 мин кладёт токен доступа на час в /var/lib/alena-chat/trainer/token.json.
Исходник — репозиторий alena-vpn (site/trainer_web.py); на VPS — /usr/local/lib/alena-chat/trainer.py, служба
trainer-web (127.0.0.1:8096), секрет пути — TRAINER_SECRET в /etc/alena-chat/env; nginx location /trainer/.
"""
import base64
import hashlib
import io
import json
import os
import threading
import time
import urllib.parse
import urllib.request
import wave
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

SECRET = os.environ.get("TRAINER_SECRET", "")
CODE = os.path.dirname(os.path.abspath(__file__))
DATA = "/var/lib/alena-chat/trainer"
TOKEN = f"{DATA}/token.json"
PHRASES = f"{DATA}/phrases.json"
SAMPLES = f"{DATA}/samples"
LOGF = f"{DATA}/trainer_log.jsonl"
MODEL = "gemini-2.5-flash"
locks = {}                                   # файл образца → замок (две вкладки не заказывают одно и то же дважды)
locks_guard = threading.Lock()

# Тот же промпт, что в окне на XPS (тренажёр_чтения.py) — менять вместе
PROMPT = """Кандидат (русскоязычный, учит иврит) читает вслух фразу для собеседования. Целевая фраза:
{he}  ({ru})
Сначала внимательно прослушай запись и запиши, что реально прозвучало, потом сравни с целевой фразой по словам.
Верни JSON:
{{"heard": "что прозвучало — русскими буквами с ударением (например: тОда шеиткашАрта)",
 "score": 1-5 (5 — носитель поймёт без усилий),
 "problems": [{{"word": "слово на иврите", "said": "как прозвучало (русскими буквами, ударная гласная заглавной)",
               "should": "как правильно (так же)", "fix": "что сделать, коротко по-русски"}}],
 "tip": "главный совет одной фразой по-русски"}}
Правила:
- Если ошибка в звуке, который русскими буквами не отличить (ר, ח/כ, ע), пометь звук в скобках, чтобы said и should
  различались: said «ледабЭр (р русское, раскатистое)», should «ледабЭр (ר горловое)».
- В problems — ТОЛЬКО слова, где прозвучавшее реально отличается от правильного (пропущен или лишний слог, не тот звук,
  не то ударение). Если said и should совпадают — это не ошибка, не включай. Нет ошибок — пустой список.
- Не выдумывай разницу между ивритскими и русскими звуками там, где её нет: ת/ט = русское «т», ד = «д», ב = «б»,
  ב без точки = «в», ל = «л», ש = «ш», שׂ/ס = «с». Настоящие отличия — ר (горловое, как французское r), ח/כ без точки
  («х» глубже), ע/א/ה (не глотать, не придыхать лишнего), ударение (в иврите чаще на последний слог).
- Если записи почти нет (тишина, шум) — score 0, heard пустой, tip — совет по технике (ближе к микрофону, громче).
- Не придирайся к акценту, если слово понятно. Не больше 4 пунктов, самые важные первыми."""


def vertex(model, body, timeout):
    t = json.load(open(TOKEN))
    req = urllib.request.Request(
        f"https://aiplatform.googleapis.com/v1/projects/{t['project']}/locations/global/publishers/google/models/"
        f"{model}:generateContent", data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {t['token']}", "Content-Type": "application/json"})
    last = None
    for _ in range(3):                       # Vertex изредка зависает — короткий срок и повтор
        try:
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except Exception as e:
            last = e
            if getattr(e, "code", 500) < 500 and getattr(e, "code", 500) != 429:
                break
    raise last


def phrases():
    return json.load(open(PHRASES, encoding="utf-8"))


def sample(i, fast):
    he = phrases()[i]["he"]
    f = f"{SAMPLES}/{hashlib.md5(he.encode()).hexdigest()}{'_fast' if fast else ''}.wav"
    with locks_guard:
        lk = locks.setdefault(f, threading.Lock())
    with lk:
        if not os.path.exists(f):
            body = {"contents": [{"role": "user", "parts": [{"text":
                        ("Произнеси на иврите в обычном разговорном темпе, естественно, как носитель языка в телефонном "
                         "разговоре:\n" if fast else
                         "Прочитай на иврите медленно, чётко и спокойно, как преподаватель для ученика, с правильным "
                         "ударением:\n") + he}]}],
                    "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {
                        "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Charon"}}}}}
            r = vertex("gemini-2.5-flash-tts", body, 45)
            pcm = base64.b64decode(r["candidates"][0]["content"]["parts"][0]["inlineData"]["data"])
            os.makedirs(SAMPLES, exist_ok=True)
            b = io.BytesIO()
            w = wave.open(b, "wb")
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(pcm)
            w.close()
            open(f + ".tmp", "wb").write(b.getvalue())
            os.replace(f + ".tmp", f)
    return open(f, "rb").read()


def judge(i, wav):
    p = phrases()[i]
    body = {"contents": [{"role": "user", "parts": [
                {"inlineData": {"mimeType": "audio/wav", "data": base64.b64encode(wav).decode()}},
                {"text": PROMPT.format(he=p["he"], ru=p["ru"])}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2,
                                 "thinkingConfig": {"thinkingBudget": 512}}}
    r = vertex(MODEL, body, 25)
    d = json.loads(r["candidates"][0]["content"]["parts"][0]["text"])
    probs = []
    for pr in d.get("problems") or []:       # «т вместо т» — не ошибка
        if isinstance(pr, dict):
            if pr.get("said", "").strip().lower().replace("ё", "е") == pr.get("should", "").strip().lower().replace("ё", "е"):
                continue
        probs.append(pr)
    d["problems"] = probs
    with open(LOGF, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "src": "телефон", "phrase": p["he"], **d},
                           ensure_ascii=False) + "\n")
    return d


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def route(self):
        u = urllib.parse.urlparse(self.path)
        parts = u.path.strip("/").split("/")       # trainer/<secret>/<действие>
        if len(parts) < 2 or parts[0] != "trainer" or not SECRET or parts[1] != SECRET:
            return None, {}
        return (parts[2] if len(parts) > 2 else ""), dict(urllib.parse.parse_qsl(u.query))

    def send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if "json" in ctype or "html" in ctype else "max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def err(self, code, msg):
        self.send(code, json.dumps({"error": msg}, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        act, q = self.route()
        if act is None:
            return self.err(404, "нет")
        if act == "" and not urllib.parse.urlparse(self.path).path.endswith("/"):
            self.send_response(301)
            self.send_header("Location", f"/trainer/{SECRET}/")
            self.end_headers()
            return
        try:
            if act == "":
                self.send(200, open(f"{CODE}/trainer.html", "rb").read(), "text/html; charset=utf-8")
            elif act == "phrases":
                self.send(200, json.dumps(phrases(), ensure_ascii=False).encode(), "application/json; charset=utf-8")
            elif act == "sample":
                self.send(200, sample(int(q.get("i", 0)), q.get("fast") == "1"), "audio/wav")
            elif act == "log":
                self.send(200, open(LOGF, "rb").read() if os.path.exists(LOGF) else b"", "application/json; charset=utf-8")
            else:
                self.err(404, "нет")
        except Exception as e:
            self.err(502, str(e)[:200])

    def do_POST(self):
        act, q = self.route()
        if act != "judge":
            return self.err(404, "нет")
        n = int(self.headers.get("Content-Length") or 0)
        if not 0 < n < 8_000_000:
            return self.err(400, "пустая или слишком длинная запись")
        wav = self.rfile.read(n)
        try:
            d = judge(int(q.get("i", 0)), wav)
            self.send(200, json.dumps(d, ensure_ascii=False).encode(), "application/json; charset=utf-8")
        except Exception as e:
            self.err(502, "Gemini: " + str(e)[:200])


if __name__ == "__main__":
    os.makedirs(SAMPLES, exist_ok=True)
    print("trainer-web: 127.0.0.1:8096", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8096), H).serve_forever()
