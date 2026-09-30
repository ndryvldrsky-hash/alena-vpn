#!/usr/bin/env python3
"""«Рон» — рекрутер-тренажёр для проб перевода звонка (30.09.2026).

Twilio звонит на A54 (twilio_звонок.py в аддоне HA, параметр Url) и берёт указания отсюда:
  POST /twilio/ron/<SECRET>/start   — первая реплика Рона + <Gather input="speech" he-IL>
  POST /twilio/ron/<SECRET>/turn    — Twilio прислал SpeechResult (что ответил кандидат) → Claude в роли Рона пишет
                                       следующую реплику по смыслу ответа → <Say> + снова <Gather>; в конце — <Hangup>
Рон реагирует на ответы (просьба пользователя: «не тупо загонять фразы по порядку»). Разговор — по CallSid в памяти.
SECRET — случайный кусок пути (ссылку знает только аддон); Twilio-подпись не проверяем, но путь не угадать.
Слушает 127.0.0.1:8095; наружу — nginx location /twilio/ в конфиге kulagin.org.
Исходник — репозиторий alena-vpn (site/ron_twilio.py); на VPS — /usr/local/lib/alena-chat/ron.py, служба ron-twilio,
venv и ключ Anthropic — от alena-chat (/etc/alena-chat/env), секрет пути — RON_SECRET там же.
"""
import json
import os
import re
import threading
import time
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from xml.sax.saxutils import escape

import anthropic

MODEL = "claude-haiku-4-5"
SECRET = os.environ.get("RON_SECRET", "")
VOICE = "Google.he-IL-Standard-B"
MAX_TURNS = 10
calls = {}                       # CallSid → {"history": [...], "turns": n, "ts": время}
lock = threading.Lock()
client = anthropic.Anthropic()

SYSTEM = """Ты — Рон, рекрутер израильской компании «Гатер Груп» (слаботочные системы: камеры, сигнализации, сети).
Ты звонишь кандидату Андрею Володарскому, он откликнулся на вакансию техника слаботочных систем в Петах-Тикве.
Это тренировочный звонок: кандидат учит иврит и тренирует собеседования, поэтому:
- говори на простом разговорном иврите, 1–2 коротких предложения за раз, по одному вопросу;
- РЕАГИРУЙ на ответ кандидата: уточни, если ответ неполный; похвали или удивись по смыслу; если не понял — переспроси;
- если кандидат просит повторить или говорить медленнее — повтори проще;
- типичные темы: опыт с камерами и сетями, права и машина, когда может начать, где живёт, ожидания по зарплате,
  готовность к работе в поле и к сменам; порядок и выбор — по ходу разговора, не всё подряд;
- ТОЛЬКО иврит, ни одного слова на арабском, английском или другом языке;
- если ответ кандидата оборван или короткий — дай ему договорить: переспроси или уточни, не переходи к прощанию;
- НЕ заканчивай разговор раньше, чем задашь хотя бы 5 разных вопросов по теме; после 5–7 вопросов вежливо заверши: скажешь, что пришлёшь сообщение с деталями собеседования, и попрощаешься.
Верни ТОЛЬКО JSON: {"say": "реплика на иврите", "end": true|false} (end=true — это прощание, после него трубка кладётся)."""


def twiml_say_gather(text, action, end=False):
    # 30.09: медленнее (85 %) — пользователь тренируется, не успевает читать подсказки
    say = f'<Say voice="{VOICE}" language="he-IL"><prosody rate="85%">{escape(text)}</prosody></Say>'
    if end:
        return f"<Response>{say}<Pause length=\"1\"/><Hangup/></Response>"
    gather = (f'<Gather input="speech" language="he-IL" speechTimeout="2" timeout="10" action="{action}" '
              f'method="POST" actionOnEmptyResult="true">{say}</Gather>')
    return f"<Response><Pause length=\"1\"/>{gather}<Redirect method=\"POST\">{action}</Redirect></Response>"


def ron_reply(sid, heard):
    with lock:
        c = calls.setdefault(sid, {"history": [], "turns": 0, "ts": time.time()})
        c["turns"] += 1
        c["ts"] = time.time()
        if heard is not None:
            c["history"].append({"role": "user", "content": heard.strip() or "(кандидат молчит или не расслышан)"})
        msgs = list(c["history"]) or [{"role": "user", "content": "(звонок соединён, кандидат взял трубку и сказал «алло»)"}]
        turns = c["turns"]
    if turns > MAX_TURNS:
        msgs = msgs + [{"role": "user", "content": "(пора заканчивать разговор — попрощайся)"}]
    try:
        r = client.messages.create(model=MODEL, max_tokens=300, system=SYSTEM, messages=msgs)
        t = r.content[0].text
        d = json.loads(re.search(r"\{.*\}", t, re.S).group(0))
        say, end = d.get("say", "").strip(), bool(d.get("end")) or turns > MAX_TURNS + 1
        if end and turns < 6:                    # рано прощается — не даём (минимум 5 вопросов)
            end = False
    except Exception as e:
        print("claude:", repr(e)[:200], flush=True)
        say, end = "סליחה, יש בעיה בקו. אני אחזור אליך מאוחר יותר. להתראות!", True
    with lock:
        calls[sid]["history"].append({"role": "assistant", "content": json.dumps({"say": say, "end": end}, ensure_ascii=False)})
    print(time.strftime("%H:%M:%S"), sid[-6:], "услышал:", heard, "| Рон:", say, "| end" if end else "", flush=True)
    return say, end


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        parts = self.path.split("?")[0].strip("/").split("/")          # twilio/ron/<secret>/<действие>
        if len(parts) != 4 or parts[:2] != ["twilio", "ron"] or not SECRET or parts[2] != SECRET:
            self.send_response(404)
            self.end_headers()
            return
        n = int(self.headers.get("Content-Length") or 0)
        form = dict(urllib.parse.parse_qsl(self.rfile.read(n).decode()))
        sid = form.get("CallSid", "нет")
        action = f"/twilio/ron/{SECRET}/turn"
        if parts[3] == "start":
            say, end = ron_reply(sid, None)
        elif parts[3] == "turn":
            say, end = ron_reply(sid, form.get("SpeechResult", ""))
        else:
            self.send_response(404)
            self.end_headers()
            return
        body = twiml_say_gather(say, action, end).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        with lock:                                                   # старые разговоры — вон
            for k in [k for k, v in calls.items() if time.time() - v["ts"] > 3600]:
                calls.pop(k, None)


if __name__ == "__main__":
    print("ron-twilio: 127.0.0.1:8095", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8095), H).serve_forever()
