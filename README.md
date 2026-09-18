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
3. **Внешняя копия камер: последний час.** Службы `cam-archive@<камера>` без перекодирования пишут с go2rtc Frigate
   (Оптиплекс `192.168.77.2:28554`, через туннель и Коин) минутными `.mkv` в `/var/lib/cam-archive/<камера>/`:
   субпотоки `<камера>_sub` у xm530, x2_wq_bl, tambur (HEVC, ~1 Мбит/с на всех) и основные потоки у axis_2100, imac, xps
   (H.264, ~1,5–2 Мбит/с каждая — субпотоков у них нет, с 2026-09-18). `cam-archive-clean.timer` удаляет всё старше часа.
   `cam-archive-pause on|off` — пауза всех записей (флаг `.paused`); её дёргает автоматизация HA, когда туннель Коина
   идёт через телефон (архив забивал отдачу мобилы), и снимает на оптике.
4. **Веб-плеер архива** — https://alena-vpn.tail603bb5.ts.net, **только для устройств tailnet** (`tailscale serve` →
   `127.0.0.1:8080`, служба `cam-archive-web`). Таймлайн по камерам, события Frigate с миниатюрами, перекодирование
   выбранной минуты в H.264 по запросу, скорость до 8×.

## Файлы

| В репозитории | На сервере |
|---|---|
| `cam-archive/cam-archive-record` | `/usr/local/sbin/cam-archive-record` |
| `cam-archive/cam-archive-status` | `/usr/local/sbin/cam-archive-status` (JSON для датчика HA `sensor.arkhiv_kamer_na_vps`) |
| `cam-archive/cam-archive-pause` | `/usr/local/sbin/cam-archive-pause` (on/off/status — пауза записи, дёргает автоматизация HA на телефонном канале) |
| `cam-archive/vps-status` | `/usr/local/sbin/vps-status` (JSON о самом сервере для датчика HA `sensor.vps_alena_vpn`, вкладка «VPS») |
| `cam-archive/vps-control` | `/usr/local/sbin/vps-control` (restart_web/restart_archive/restart_tailscale/reboot — кнопки вкладки «VPS») |
| `cam-archive/cam_archive_web.py` | `/usr/local/lib/cam-archive-web/app.py` |
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
3. Разблокировать репозиторий, разложить файлы по таблице выше, `useradd --system --home /var/lib/cam-archive --shell /usr/sbin/nologin camarchive`.
4. `systemctl enable --now wg-quick@wg0 nftables iperf3-wg cam-archive-web cam-archive-clean.timer cam-archive-watchdog.timer cam-archive@xm530 cam-archive@x2_wq_bl cam-archive@tambur cam-archive@axis_2100 cam-archive@imac cam-archive@xps`.
5. `tailscale up --hostname=alena-vpn --accept-dns=false --accept-routes=false` (подтвердить вход по ссылке),
   `tailscale serve --bg --https=443 http://127.0.0.1:8080`.
6. Если сменился IP — поправить Endpoint в `/etc/wireguard/wg-vps.conf` на Коине и `/config/.ssh/config`.
