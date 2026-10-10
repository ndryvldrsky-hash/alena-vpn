#!/bin/bash
# Выкладка архива камер и его плеера на alena-oblako (Oracle Cloud il-jerusalem-1, VM.Standard.A1.Flex 2 ядра / 12 ГБ,
# только тайлнет, вход ubuntu + sudo, ключ /config/.ssh/oracle_vm). 10.10.2026 — переезд с VPS alena-vpn.
#   deploy_oblako.sh deploy — поставить ffmpeg/python3, пользователя, скрипты, юниты, env; перезапустить запись и плеер
#   deploy_oblako.sh status — что пишется
# Потоки берутся с go2rtc Frigate на Кузнице по тайлнету (RTSP_HOST в secrets/rtsp-oblako.env).
set -euo pipefail
DIR=$(cd "$(dirname "$0")" && pwd)
SSH="ssh -F /config/.ssh/config oblako"
CAMS="xm530 x2_wq_bl tambur imac xps axis_2100"
case "${1:-status}" in
deploy)
  T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
  cp "$DIR"/cam-archive/cam-archive-{record,status,watchdog,pause} "$DIR"/cam-archive/cam_archive_web.py "$DIR"/systemd/cam-archive* "$T"/
  cp "$DIR"/secrets/rtsp-oblako.env "$T"/rtsp.env
  tar -C "$T" -cf - . | $SSH "set -e; rm -rf /tmp/ca; mkdir /tmp/ca; tar -C /tmp/ca -xf -; cd /tmp/ca
    command -v ffmpeg >/dev/null || { sudo apt-get update -qq; sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ffmpeg python3; }
    id camarchive >/dev/null 2>&1 || sudo useradd -r -s /usr/sbin/nologin -d /var/lib/cam-archive camarchive
    sudo install -d -o camarchive -g camarchive /var/lib/cam-archive
    sudo install -d -o ubuntu -g ubuntu -m 755 /var/lib/cam-archive/events   # события шлёт cam_archive_events_push.py под ubuntu
    sudo install -d -m 700 /etc/cam-archive; sudo install -m 600 rtsp.env /etc/cam-archive/rtsp.env
    for f in cam-archive-record cam-archive-status cam-archive-watchdog cam-archive-pause; do sudo install -m 755 \$f /usr/local/sbin/\$f; done
    sudo install -D -m 755 cam_archive_web.py /usr/local/lib/cam-archive-web/app.py
    sudo install -m 644 *.service *.timer /etc/systemd/system/
    sudo sed -i 's/ wg-quick@wg0.service/ tailscaled.service/' /etc/systemd/system/cam-archive@.service
    sudo systemctl daemon-reload
    for c in $CAMS; do sudo systemctl enable -q cam-archive@\$c; sudo systemctl restart cam-archive@\$c; done
    sudo systemctl enable -q --now cam-archive-clean.timer cam-archive-watchdog.timer; sudo systemctl enable -q cam-archive-web; sudo systemctl restart cam-archive-web
    sudo tailscale serve --bg --https=443 http://127.0.0.1:8080 >/dev/null
    rm -rf /tmp/ca; echo выложено"
  ;;
status) $SSH /usr/local/sbin/cam-archive-status ;;
*) echo "usage: $0 deploy|status" >&2; exit 2 ;;
esac
