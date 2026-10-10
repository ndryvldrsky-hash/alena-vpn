#!/bin/bash
# Выкладка второго экземпляра запасного чата с Алёной на alena-azur (Azure, Ubuntu 24.04, 2 ядра / 0,9 ГБ,
# только тайлнет 100.74.101.87, вход alena + sudo, ключ /config/.ssh/azure_alena). 11.10.2026.
#   deploy_azur.sh deploy — python3-venv, пользователь, venv с anthropic, код, промпт, юнит, env; перезапуск службы; tailscale serve :8443
#   deploy_azur.sh status — служба, serve, память
# Основной экземпляр живёт на VPS alena-vpn (deploy.sh) — этот скрипт его не касается.
# Очередь поручений дому включена (OWNER_QUEUE=1 в юните) с 11.10.2026: дом обходит оба экземпляра.
set -euo pipefail
DIR=$(cd "$(dirname "$0")" && pwd)
SSH="ssh -F /config/.ssh/config azur"
ANTHROPIC_VER=1.7.0   # как в venv на VPS
case "${1:-status}" in
deploy)
  T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
  cp "$DIR"/site/alena_owner_tailnet.py "$T"/app.py
  cp "$DIR"/site/alena_owner_tailnet_prompt.md "$T"/owner_prompt.md
  cp "$DIR"/systemd/alena-owner-tailnet-azur.service "$T"/alena-owner-tailnet.service
  ( umask 077; grep -E '^(ANTHROPIC_API_KEY|BRIDGE_TOKEN)=' "$DIR"/secrets/alena-chat.env > "$T"/env )
  [ "$(wc -l < "$T"/env)" -eq 2 ] || { echo "в secrets/alena-chat.env нет ключа или токена (git-crypt открыт?)" >&2; exit 1; }
  tar -C "$T" -cf - . | $SSH "set -e; umask 077; rm -rf /tmp/ao; mkdir /tmp/ao; tar -C /tmp/ao -xf -; cd /tmp/ao
    python3 -c 'import ensurepip' 2>/dev/null || { sudo apt-get update -qq; sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv >/dev/null; }
    id alenachat >/dev/null 2>&1 || sudo useradd -r -s /usr/sbin/nologin -d /var/lib/alena-owner alenachat
    sudo install -d -m 755 /usr/local/lib/alena-owner
    [ -x /usr/local/lib/alena-owner/venv/bin/python3 ] || sudo python3 -m venv /usr/local/lib/alena-owner/venv
    sudo /usr/local/lib/alena-owner/venv/bin/python3 -c 'import anthropic,sys; sys.exit(anthropic.__version__ != \"$ANTHROPIC_VER\")' 2>/dev/null \
      || sudo /usr/local/lib/alena-owner/venv/bin/pip install -q --no-cache-dir anthropic==$ANTHROPIC_VER
    sudo install -m 644 app.py owner_prompt.md /usr/local/lib/alena-owner/
    sudo install -d -m 700 /etc/alena-owner; sudo install -m 600 env /etc/alena-owner/env
    sudo install -m 644 alena-owner-tailnet.service /etc/systemd/system/alena-owner-tailnet.service
    sudo systemctl daemon-reload; sudo systemctl enable -q alena-owner-tailnet; sudo systemctl restart alena-owner-tailnet
    sudo tailscale serve --bg --https=8443 http://127.0.0.1:8099 >/dev/null
    cd /; rm -rf /tmp/ao; sleep 2; systemctl is-active alena-owner-tailnet; echo выложено"
  ;;
status)
  $SSH 'systemctl is-active alena-owner-tailnet; systemctl show alena-owner-tailnet -p MemoryCurrent -p ActiveEnterTimestamp
    curl -s -m 5 http://127.0.0.1:8099/health; echo; sudo tailscale serve status; ls -la /var/lib/alena-owner; free -m | sed -n 2p' ;;
*) echo "usage: $0 deploy|status" >&2; exit 2 ;;
esac
