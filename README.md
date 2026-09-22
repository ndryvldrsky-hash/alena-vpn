# alena-vpn

Конфигурация арендованного сервера **alena-vpn** (DigitalOcean, Лондон, `167.99.95.129`, 1 vCPU / 512 МБ / 10 ГБ, Debian 13).
Сервер появился 17.09.2026: оптика Bezeq сбоит, а релей VS Code Tunnel в Лондоне из Израиля недоступен.

**Репозиторий публичный.** Всё с паролями, ключами и адресами сетей зашифровано git-crypt (`.gitattributes`).
Ключ — `/config/.claude/alena_vpn_config.key` на Оптиплексе (разблокировать: `git-crypt unlock <ключ>`).

## Что делает сервер

1. **Выход в интернет для всего дома.** WireGuard `wg0` (`10.77.0.1/24`, порт 51820) принимает туннель от Коина — шлюза сети 77;
   весь трафик сети идёт через Лондон. Каналы под туннелем (телефон по USB, телефон по Wi-Fi, оптика) выбирает служба
   `alena-vps` на Коине — она в `/config/coin_router`, не здесь.
2. **Тест скорости канала** — `iperf3-wg.service`, только внутри туннеля (`10.77.0.1:5201`).
   Второй пир (17.09.2026) — второй туннель Коина `wg-vps2` (`10.77.0.3/32`) через второй USB-телефон: соединения сети
   делятся между двумя туннелями (ECMP на Коине), ёмкость складывается. Сеть 77 остаётся за первым пиром.
3. **Внешняя копия камер: последние 6 часов.** Службы `cam-archive@<камера>` без перекодирования пишут с go2rtc Frigate
   (Оптиплекс `192.168.77.2:28554`, через туннель и Коин) минутными `.mkv` в `/var/lib/cam-archive/<камера>/`:
   субпотоки `<камера>_sub` у xm530, x2_wq_bl, tambur (HEVC), imac (VideoToolbox на Маке) и xps (libx264 на XPS), основной поток у axis_2100
   (480x640 с потолком 400 кбит/с в мосте на Оптиплексе — субпотока нет). `cam-archive-clean.timer` удаляет всё старше 6 часов (с 2026-09-18; axis — основной поток с потолком 400 кбит/с в мосте, imac_sub — VideoToolbox на Маке, xps_sub — libx264 на XPS, ~0,7 ГБ/ч на все шесть).
   `cam-archive-pause on|off` — пауза всех записей (флаг `.paused`); её дёргает автоматизация HA, когда туннель Коина
   идёт через телефон (архив забивал отдачу мобилы), и снимает на оптике.
4. **Веб-плеер архива** — https://alena-vpn.tail603bb5.ts.net, **только для устройств tailnet** (`tailscale serve` →
   `127.0.0.1:8080`, служба `cam-archive-web`). Таймлайн по камерам, события Frigate с миниатюрами, перекодирование
   выбранной минуты в H.264 по запросу, скорость до 8×.

5. **Чат с Алёной на kulagin.org** (с 22.09.2026) — служба `alena-chat` (`127.0.0.1:8090`, наружу через `location /api/chat`
   в nginx-конфиге сайта, поток SSE). Отдельный экземпляр Алёны на Claude API (`claude-opus-5`, ключ Anthropic в
   `/etc/alena-chat/env`), без инструментов и доступа в дом: объясняет посетителям суть идеи по `prompt.md` и собирает
   контакт. Историю диалога держит в памяти по id сессии; законченный диалог (посетитель ушёл или молчит 15 мин)
   сводится Claude в 2–3 предложения по-русски и уходит вебхуком в HA (`automations.yaml` → `site_chat_handoff`: push на
   телефоны, постоянное уведомление с полным текстом, письмо). Лимиты: 30 реплик/ч и 80/сутки на IP, 400/сутки всего,
   1500 символов на реплику. Учёт — `/var/lib/alena-chat/log/<дата>.jsonl` и `stats.json` (`chat-status` → сенсор HA
   `sensor.chat_na_saite`, вкладка «Чат на сайте»). Зависимости — venv `/usr/local/lib/alena-chat/venv` (`pip install
   anthropic`, при восстановлении с нуля — `apt install python3-venv`). Память: у дроплета 512 МБ, SDK ≈ 55 МБ RSS, поэтому
   22.09 добавлен `/swapfile` на 1 ГБ (`vm.swappiness=10`, `/etc/sysctl.d/91-swap.conf`); у службы `MemoryMax=200M`.
   Виджет чата — в `index.html` сайта (`/config/kulagin_site`, выкладка `vps_site_deploy`), на пяти языках.

## Файлы

| В репозитории | На сервере |
|---|---|
| `cam-archive/cam-archive-record` | `/usr/local/sbin/cam-archive-record` |
| `cam-archive/cam-archive-status` | `/usr/local/sbin/cam-archive-status` (JSON для датчика HA `sensor.arkhiv_kamer_na_vps`) |
| `cam-archive/cam-archive-pause` | `/usr/local/sbin/cam-archive-pause` (on/off/status — пауза записи, дёргает автоматизация HA на телефонном канале) |
| `cam-archive/vps-status` | `/usr/local/sbin/vps-status` (JSON о самом сервере для датчика HA `sensor.vps_alena_vpn`, вкладка «VPS») |
| `cam-archive/vps-control` | `/usr/local/sbin/vps-control` (restart_web/restart_archive/restart_tailscale/reboot — кнопки вкладки «VPS») |
| `cam-archive/cam_archive_web.py` | `/usr/local/lib/cam-archive-web/app.py` |
| `site/site-status` | `/usr/local/sbin/site-status` (JSON о сайте для `sensor.sait_kulagin_org`, вкладка «Сайт») |
| `site/alena_chat.py` | `/usr/local/lib/alena-chat/app.py` — служба `alena-chat`, чат с Алёной |
| `site/alena_chat_prompt.md` | `/usr/local/lib/alena-chat/prompt.md` — кто такая Алёна и суть идеи (system prompt, кешируется на час) |
| `site/chat-status` | `/usr/local/sbin/chat-status` (JSON для `sensor.chat_na_saite`, вкладка «Чат на сайте») |
| `site/nginx-kulagin.org.conf` | `/etc/nginx/sites-available/kulagin.org` — сайт + `location /api/chat`; certbot дописывает сюда же, после его правок — `deploy.sh pull` |
| `secrets/alena-chat.env` 🔒 | `/etc/alena-chat/env` — ключ Anthropic и адрес вебхука HA (id вебхука — в `automations.yaml`) |
| `systemd/*` | `/etc/systemd/system/` |
| `secrets/rtsp.env` 🔒 | `/etc/cam-archive/rtsp.env` — логин и пароль go2rtc Frigate |
| `wireguard/wg0.conf`, `server.key` 🔒 | `/etc/wireguard/` |
| `nftables.conf` 🔒 | `/etc/nftables.conf` — снаружи открыты только SSH, WireGuard, UDP Tailscale; HTTPS — только с `tailscale0` |
| `sysctl/90-wg.conf` | `/etc/sysctl.d/` — `ip_forward` |

Живут вне репозитория: события Frigate для таймлайна отправляет раз в минуту `/config/.local/bin/cam_archive_events_push.py`
(cron аддона на Оптиплексе), правило доступа VPS к RTSP Frigate — в `/etc/wireguard/wg-vps.*` на Коине.

## Выкладка

```bash
/config/alena-vpn/deploy.sh status   # службы, архив, туннель, Tailscale, диск
/config/alena-vpn/deploy.sh deploy   # архив и плеер: только изменившиеся файлы, перезапуск только затронутых служб
/config/alena-vpn/deploy.sh pull     # забрать файлы с сервера, если правили прямо там
```

`deploy` **не трогает** `nftables.conf`, `wireguard/` и `sysctl/` — ошибка в них оставит дом без интернета. Выкладывать их
руками и осторожно. Внимание: в `nftables.conf` стоит `flush ruleset` — перезагрузка файла снесёт правила, которые добавляет
tailscaled; после неё перезапустить `tailscaled`.

## Восстановление с нуля

1. Дроплет Debian 13 в DigitalOcean, SSH-ключ `alena-ha` (`/config/.ssh/do_vpn`), адрес — в `/config/.ssh/config` (хост `do-vpn`).
2. `apt install wireguard-tools nftables iperf3 ffmpeg python3`; Tailscale — `curl -fsSL https://tailscale.com/install.sh | sh`.
3. Разблокировать репозиторий, разложить файлы по таблице выше, `useradd --system --home /var/lib/cam-archive --shell /usr/sbin/nologin camarchive`;
   для чата — `useradd --system --home /var/lib/alena-chat --shell /usr/sbin/nologin alenachat`, `apt install python3-venv nginx certbot python3-certbot-nginx`,
   `python3 -m venv /usr/local/lib/alena-chat/venv && /usr/local/lib/alena-chat/venv/bin/pip install anthropic`, `mkdir -p /etc/alena-chat /var/www/kulagin.org`.
4. `systemctl enable --now wg-quick@wg0 nftables iperf3-wg cam-archive-web cam-archive-clean.timer cam-archive-watchdog.timer cam-archive@xm530 cam-archive@x2_wq_bl cam-archive@tambur cam-archive@axis_2100 cam-archive@imac cam-archive@xps alena-chat nginx`.
5. `tailscale up --hostname=alena-vpn --accept-dns=false --accept-routes=false` (подтвердить вход по ссылке),
   `tailscale serve --bg --https=443 http://127.0.0.1:8080`.
6. Если сменился IP — поправить Endpoint в `/etc/wireguard/wg-vps.conf` на Коине и `/config/.ssh/config`.
