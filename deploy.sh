#!/bin/bash
# Выкладка конфигурации VPS alena-vpn (DigitalOcean lon1, 167.99.95.129) из этого репозитория. 2026-09-17.
#   /config/alena-vpn/deploy.sh deploy  — архив камер, веб-плеер, чат с Алёной и nginx сайта: скрипты, юниты systemd, env-файлы;
#                                         daemon-reload и перезапуск только тех служб, чьи файлы изменились
#   /config/alena-vpn/deploy.sh pull    — забрать текущие файлы с VPS в репозиторий (перед коммитом, если правили на сервере)
#   /config/alena-vpn/deploy.sh status  — службы, запись архива, туннель, Tailscale
# НЕ применяет nftables.conf, wireguard/ и sysctl/: через VPS идёт интернет всего дома, ошибка там = дом без сети.
# Эти файлы выкладываются руками и осознанно (см. README).
set -euo pipefail
DIR=$(cd "$(dirname "$0")" && pwd)
SSH="ssh -F /config/.ssh/config do-vpn"
SCP="scp -F /config/.ssh/config"

# файл в репозитории → путь на VPS, режим
FILES=(
  "cam-archive/cam-archive-record|/usr/local/sbin/cam-archive-record|755"
  "cam-archive/cam-archive-status|/usr/local/sbin/cam-archive-status|755"
  "cam-archive/cam-archive-watchdog|/usr/local/sbin/cam-archive-watchdog|755"
  "cam-archive/cam-archive-pause|/usr/local/sbin/cam-archive-pause|755"
  "cam-archive/vps-status|/usr/local/sbin/vps-status|755"
  "cam-archive/vps-control|/usr/local/sbin/vps-control|755"
  "site/site-status|/usr/local/sbin/site-status|755"
  "site/alena_chat.py|/usr/local/lib/alena-chat/app.py|644"
  "site/alena_chat_prompt.md|/usr/local/lib/alena-chat/prompt.md|644"
  "site/alena_chat_prompt_wa.md|/usr/local/lib/alena-chat/prompt_wa.md|644"
  "site/chat-status|/usr/local/sbin/chat-status|755"
  "site/chat-dialog|/usr/local/sbin/chat-dialog|755"
  "site/nginx-kulagin.org.conf|/etc/nginx/sites-available/kulagin.org|644"
  "systemd/alena-chat.service|/etc/systemd/system/alena-chat.service|644"
  "secrets/alena-chat.env|/etc/alena-chat/env|600"
  "systemd/cam-archive-watchdog.service|/etc/systemd/system/cam-archive-watchdog.service|644"
  "systemd/cam-archive-watchdog.timer|/etc/systemd/system/cam-archive-watchdog.timer|644"
  "cam-archive/cam_archive_web.py|/usr/local/lib/cam-archive-web/app.py|755"
  "systemd/cam-archive@.service|/etc/systemd/system/cam-archive@.service|644"
  "systemd/cam-archive-clean.service|/etc/systemd/system/cam-archive-clean.service|644"
  "systemd/cam-archive-clean.timer|/etc/systemd/system/cam-archive-clean.timer|644"
  "systemd/cam-archive-web.service|/etc/systemd/system/cam-archive-web.service|644"
  "systemd/iperf3-wg.service|/etc/systemd/system/iperf3-wg.service|644"
  "secrets/rtsp.env|/etc/cam-archive/rtsp.env|600"
)
# только забираются (pull), не выкладываются
PULL_ONLY=(
  "nftables.conf|/etc/nftables.conf"
  "wireguard/wg0.conf|/etc/wireguard/wg0.conf"
  "wireguard/server.key|/etc/wireguard/server.key"
  "sysctl/90-wg.conf|/etc/sysctl.d/90-wg.conf"
)

case "${1:-status}" in
deploy)
  changed=()
  for e in "${FILES[@]}"; do
    IFS='|' read -r src dst mode <<<"$e"
    local_md5=$(md5sum "$DIR/$src" | cut -c1-32)
    remote_md5=$($SSH "md5sum '$dst' 2>/dev/null | cut -c1-32" || true)
    [ "$local_md5" = "$remote_md5" ] && continue
    $SCP -q "$DIR/$src" do-vpn:/tmp/deploy.part
    $SSH "install -D -m $mode /tmp/deploy.part '$dst' && rm /tmp/deploy.part"
    echo "обновлён: $dst"; changed+=("$dst")
  done
  [ ${#changed[@]} -eq 0 ] && { echo "всё уже совпадает"; exit 0; }
  $SSH "systemctl daemon-reload"
  s="${changed[*]}"
  # запись: скрипт/юнит/пароль — перезапустить все камеры (потеряется до минуты записи)
  if [[ "$s" == *cam-archive-record* || "$s" == *cam-archive@.service* || "$s" == *rtsp.env* ]]; then
    $SSH 'systemctl restart $(systemctl list-units --plain --no-legend "cam-archive@*" | awk "{print \$1}")' && echo "перезапущена запись камер"
  fi
  [[ "$s" == *cam-archive-web* || "$s" == *app.py* ]] && $SSH "systemctl restart cam-archive-web" && echo "перезапущен веб-плеер"
  [[ "$s" == *cam-archive-clean* ]] && $SSH "systemctl restart cam-archive-clean.timer" && echo "перезапущен таймер очистки"
  [[ "$s" == *cam-archive-watchdog* ]] && $SSH "systemctl enable --now cam-archive-watchdog.timer >/dev/null 2>&1; systemctl restart cam-archive-watchdog.timer" && echo "перезапущен сторож записи"
  [[ "$s" == *iperf3-wg* ]] && $SSH "systemctl restart iperf3-wg" && echo "перезапущен iperf3"
  # чат с Алёной: код, промпт, окружение или юнит — перезапуск службы (открытые диалоги в памяти теряются, лог остаётся)
  [[ "$s" == *alena-chat* ]] && $SSH "systemctl enable --now alena-chat >/dev/null 2>&1; systemctl restart alena-chat" && echo "перезапущен чат с Алёной"
  # конфиг nginx сайта — проверка и мягкая перезагрузка, ошибочный конфиг не применится
  [[ "$s" == */etc/nginx/sites-available/kulagin.org* ]] && $SSH "nginx -t && systemctl reload nginx" && echo "перезагружен nginx"
  ;;
pull)
  for e in "${FILES[@]}" "${PULL_ONLY[@]}"; do
    IFS='|' read -r src dst _ <<<"$e"
    mkdir -p "$DIR/$(dirname "$src")"
    $SCP -q "do-vpn:$dst" "$DIR/$src" && echo "забран: $src"
  done
  ;;
status)
  $SSH 'echo "== службы"; for u in wg-quick@wg0 nftables tailscaled cam-archive-web cam-archive-clean.timer iperf3-wg alena-chat nginx $(systemctl list-units --plain --no-legend "cam-archive@*" | awk "{print \$1}"); do printf "  %-28s %s\n" "$u" "$(systemctl is-active $u)"; done
    echo "== архив"; /usr/local/sbin/cam-archive-status
    echo "== туннель"; wg show wg0 latest-handshakes | awk "{print \"  рукопожатие \" systime()-\$2 \" с назад\"}"
    echo "== Tailscale"; tailscale serve status 2>/dev/null | sed "s/^/  /"
    echo "== диск"; df -h / | tail -1'
  ;;
*) echo "использование: $0 deploy|pull|status"; exit 2 ;;
esac
