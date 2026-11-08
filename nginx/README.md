# nginx на VPS — копии рабочих файлов (08.11.2026)

Источник правды — сам VPS (`/etc/nginx`), здесь копии для истории и восстановления:

- `stream.d_443.conf` → `/etc/nginx/stream.d/443.conf` — порт 443 делится по SNI (`ssl_preread`): `vpn.kulagin.org` →
  Xray на Кузнице (10.77.0.3:8443 через Коин), остальное → местный nginx `127.0.0.1:4444` → (proxy_protocol) → `127.0.0.1:4443`.
  В `nginx.conf` добавлен блок `stream { include /etc/nginx/stream.d/*.conf; }`, пакет `libnginx-mod-stream`.
- `sites-available_vpn.kulagin.org` — сайт-витрина для VPN Москвы (certbot --nginx); слушает `127.0.0.1:4443 ssl proxy_protocol`
  и публичный `167.99.95.129:4443 ssl` (dest/fallback для Xray на Кузнице; nft input 4443 accept).
- `conf.d_realip.conf` — настоящие адреса клиентов из proxy_protocol.
- Остальные сайты (`kulagin.org`, `v.kulagin.org`) переведены с `listen 167.99.95.129:443 ssl` на `127.0.0.1:4443 ssl proxy_protocol`.
