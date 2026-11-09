#!/usr/bin/env bash
# Offenes Recht – Einrichtung auf einem eigenen Server (Ubuntu/Debian), als root ausführen.
#
#   bash einrichten.sh                 Erstinstallation (fragt vor jedem Schritt mit Wirkung nach außen)
#   bash einrichten.sh --zertifikat    nur HTTPS-Zertifikat holen (wenn DNS beim ersten Lauf noch nicht stimmte)
#   bash einrichten.sh --datenbank IP  Datenbank vom alten Server übernehmen (vorher dort alt-umzug.sh ausführen)
#   bash einrichten.sh --pruefen       Erreichbarkeit von Website und Betriebszugang prüfen
#
# Das Skript ist wiederholbar: Bereits Eingerichtetes wird erkannt und nicht doppelt angelegt.
set -euo pipefail
umask 022

DOMAIN="${DOMAIN:-offenesrecht.at}"
EMAIL="${EMAIL:-christoph.pramhofer@neos.eu}"          # nur für Let's Encrypt (Ablaufwarnungen)
OR_VERSION="@@VERSION@@"
PKG_SHA="@@SHA@@"
REPO="cpramhofer/offenes-recht"
APP=/opt/offenes-recht
DATA=/var/lib/offenes-recht
ETC=/etc/offenes-recht
OPSDATA=/var/lib/offenes-recht-ops
PORT=8790

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   ✓ %s\n' "$*"; }
warn() { printf '   ! %s\n' "$*"; }
ask()  { local a; read -r -p "   $1 [j/N] " a </dev/tty; [[ "$a" =~ ^[jJyY]$ ]]; }
die()  { printf '\n\033[31mFehler: %s\033[0m\n' "$*"; exit 1; }
install_file() { local t="$1" m="$2"; mkdir -p "$(dirname "$t")"; cat > "$t.tmp"; chmod "$m" "$t.tmp"; mv -f "$t.tmp" "$t"; }

[[ $EUID -eq 0 ]] || die "Bitte als root ausführen."
OS_ID="$(. /etc/os-release 2>/dev/null; echo "${ID:-} ${ID_LIKE:-}")"
OS_NAME="$(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-unbekannt}")"
[[ "$OS_ID" =~ (debian|ubuntu) ]] || die "Nur für Ubuntu oder Debian gedacht (gefunden: $OS_NAME)."

public_ip() { curl -4 -fsS -m 8 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}'; }
dns_ok() {  # $1 = Name; stimmt der A-Eintrag mit diesem Server überein?
  local want got; want="$(public_ip)"; got="$(getent ahostsv4 "$1" | awk 'NR==1{print $1}')"
  [[ -n "$got" && "$got" == "$want" ]]
}

cert_has() {  # $1 = Name; enthält das Zertifikat diesen Namen?
  local c="/etc/letsencrypt/live/$DOMAIN/fullchain.pem"
  [[ -f "$c" ]] && openssl x509 -in "$c" -noout -ext subjectAltName 2>/dev/null | grep -q "DNS:$1\b"
}

proxy_locations() {
  local token; token="$(cat /etc/or-ops-mcp/token 2>/dev/null || true)"
  [[ -n "$token" ]] && cat <<EOF
    # Betriebszugang für Claude: nur unter der geheimen Adresse erreichbar
    location /$token/mcp {
        proxy_pass http://127.0.0.1:8791;
        proxy_set_header Host 127.0.0.1:8791;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_read_timeout 300s;
    }
EOF
  cat <<EOF
    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 120s;
    }
EOF
}

write_nginx() {  # schreibt die komplette Konfiguration für $DOMAIN (mit HTTPS, sobald ein Zertifikat da ist)
  local f=/etc/nginx/sites-available/$DOMAIN bak="" live="/etc/letsencrypt/live/$DOMAIN"
  [[ -f "$f" ]] && { bak="$f.bak-$(date +%Y%m%d-%H%M%S)"; cp -a "$f" "$bak"; }
  {
    echo "# Offenes Recht – $DOMAIN. Von einrichten.sh geschrieben; certbot ändert diese Datei nicht."
    if cert_has "$DOMAIN"; then
      cat <<EOF
server {   # HTTP -> HTTPS (Zertifikatserneuerung über /.well-known/acme-challenge bleibt möglich)
    listen 80;
    listen [::]:80;
    server_name $DOMAIN www.$DOMAIN;
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 301 https://$DOMAIN\$request_uri; }
}
EOF
      cert_has "www.$DOMAIN" && cat <<EOF
server {   # www -> ohne www
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name www.$DOMAIN;
    ssl_certificate $live/fullchain.pem;
    ssl_certificate_key $live/privkey.pem;
    return 301 https://$DOMAIN\$request_uri;
}
EOF
      cat <<EOF
server {
    listen 443 ssl default_server;
    listen [::]:443 ssl default_server;
    http2 on;
    server_name $DOMAIN;
    ssl_certificate $live/fullchain.pem;
    ssl_certificate_key $live/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:or_ssl:10m;
    add_header Strict-Transport-Security "max-age=31536000" always;
    client_max_body_size 8m;
    gzip on;
    gzip_types text/css application/json application/javascript text/xml application/rss+xml;
EOF
    else
      cat <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name www.$DOMAIN;
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 301 http://$DOMAIN\$request_uri; }
}
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name $DOMAIN;
    location /.well-known/acme-challenge/ { root /var/www/html; }
    client_max_body_size 8m;
    gzip on;
    gzip_types text/css application/json application/javascript text/xml application/rss+xml;
EOF
    fi
    proxy_locations
    echo "}"
  } > "$f.new"
  mv -f "$f.new" "$f"
  mkdir -p /var/www/html
  ln -sf "$f" /etc/nginx/sites-enabled/$DOMAIN
  rm -f /etc/nginx/sites-enabled/default
  # Reste, die certbot früher in andere Dateien geschrieben hat, stören nicht mehr: nur unsere Datei bedient $DOMAIN
  if ! nginx -t >/dev/null 2>&1; then
    # ältere nginx-Versionen kennen "http2 on;" nicht
    sed -i 's/^    http2 on;$//; s/listen 443 ssl default_server;/listen 443 ssl http2 default_server;/; s/listen \[::\]:443 ssl default_server;/listen [::]:443 ssl http2 default_server;/' "$f"
  fi
  if ! nginx -t >/dev/null 2>&1; then
    nginx -t || true; [[ -n "$bak" ]] && cp -a "$bak" "$f"; die "nginx-Konfiguration fehlerhaft, alte Fassung wiederhergestellt."
  fi
  systemctl enable --now nginx >/dev/null 2>&1; systemctl reload nginx
}

pruefen() {
  say "Prüfung"
  local t a b c d e
  t="$(cat /etc/or-ops-mcp/token 2>/dev/null || true)"
  a=$(curl -s -o /dev/null -w "%{http_code}" -m 15 "https://$DOMAIN/api/me" || true)
  b=$(curl -s -o /dev/null -w "%{http_code} %{redirect_url}" -m 15 "http://$DOMAIN/" || true)
  c=$(curl -s -o /dev/null -w "%{http_code} %{redirect_url}" -m 15 "https://www.$DOMAIN/" || true)
  d=$(curl -s -o /dev/null -w "%{http_code}" -m 15 "https://$DOMAIN/.well-known/oauth-protected-resource" || true)
  e=$(curl -s -o /dev/null -w "%{http_code}" -m 15 -X POST "https://$DOMAIN/$t/mcp" -H "Origin: https://claude.ai" \
       -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
       -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"1"}}}' || true)
  echo "   https://$DOMAIN            $a   (soll 200)"
  echo "   http://$DOMAIN             $b   (soll 301 auf https://$DOMAIN/)"
  echo "   https://www.$DOMAIN        $c   (soll 301 auf https://$DOMAIN/)"
  echo "   OAuth-Erkennung            $d   (soll 404)"
  echo "   Betriebszugang (Konnektor) $e   (soll 200)"
  [[ "$a" == 200 && "$d" == 404 && "$e" == 200 ]] && ok "Alles in Ordnung." || warn "Noch nicht in Ordnung – bitte diese Ausgabe in den Chat kopieren."
}

zertifikat() {
  say "HTTPS-Zertifikat (Let's Encrypt)"
  local names=()
  dns_ok "$DOMAIN" && names+=(-d "$DOMAIN") || warn "$DOMAIN zeigt noch nicht auf diesen Server ($(public_ip))."
  dns_ok "www.$DOMAIN" && names+=(-d "www.$DOMAIN") || warn "www.$DOMAIN zeigt noch nicht auf diesen Server."
  if [[ ${#names[@]} -eq 0 ]]; then
    warn "Kein Zertifikat möglich. DNS-Einträge setzen, ein paar Minuten warten, dann: bash $0 --zertifikat"; return 0
  fi
  mkdir -p /var/www/html
  write_nginx   # zuerst eine saubere Konfiguration, damit die Prüfung von Let's Encrypt über HTTP ankommt
  certbot certonly --webroot -w /var/www/html --non-interactive --agree-tos -m "$EMAIL" \
    --cert-name "$DOMAIN" --expand "${names[@]}" || die "certbot ist fehlgeschlagen (siehe oben)."
  install_file /etc/letsencrypt/renewal-hooks/deploy/nginx-reload.sh 0755 <<'EOF'
#!/bin/sh
systemctl reload nginx
EOF
  # Erneuerung nur über webroot, ohne dass certbot die nginx-Konfiguration verändert
  sed -i '/^installer *=/d; s/^authenticator *=.*/authenticator = webroot/' "/etc/letsencrypt/renewal/$DOMAIN.conf" 2>/dev/null || true
  write_nginx
  sed -i "s#^OR_SECURE_COOKIE=.*#OR_SECURE_COOKIE=1#" "$ETC/app.env" 2>/dev/null || true
  systemctl restart offenes-recht
  ok "HTTPS aktiv: https://$DOMAIN (Erneuerung läuft automatisch über certbot.timer)"
}

datenbank() {  # $1 = IP oder Name des alten Servers
  local old="$1"; [[ -n "$old" ]] || die "Bitte die Adresse des alten Servers angeben: bash $0 --datenbank 1.2.3.4"
  say "Datenbank vom alten Server übernehmen ($old)"
  echo "   Voraussetzung: Auf dem alten Server lief alt-umzug.sh (Dienst gestoppt, /root/umzug.db erstellt)."
  ask "Jetzt per scp holen? Sie werden nach dem root-Passwort des alten Servers gefragt." || exit 0
  local tmp; tmp="$(mktemp -d)"
  scp -o StrictHostKeyChecking=accept-new "root@$old:/root/umzug.db" "$tmp/umzug.db" </dev/tty || die "scp fehlgeschlagen."
  python3 - "$tmp/umzug.db" <<'PY' || die "Die übertragene Datei ist keine gültige Datenbank."
import sqlite3, sys
c = sqlite3.connect(sys.argv[1]); assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
print("   Konten: %d, Beiträge: %d, Bewertungen: %d" % tuple(c.execute(q).fetchone()[0] for q in
      ("SELECT COUNT(*) FROM users", "SELECT COUNT(*) FROM contributions", "SELECT COUNT(*) FROM ratings")))
PY
  ask "Diese Datenbank übernehmen? Die bisherige auf diesem Server wird vorher gesichert." || { rm -rf "$tmp"; exit 0; }
  systemctl stop offenes-recht
  mkdir -p "$DATA/backups"
  [[ -f "$DATA/app.db" ]] && mv "$DATA/app.db" "$DATA/backups/app-$(date +%Y%m%d-%H%M%S)-vor-umzug.db"
  rm -f "$DATA/app.db-wal" "$DATA/app.db-shm"
  install -o offenes-recht -g offenes-recht -m 0640 "$tmp/umzug.db" "$DATA/app.db"
  rm -rf "$tmp"
  systemctl start offenes-recht; sleep 2
  curl -fsS -m 5 "http://127.0.0.1:$PORT/api/me" >/dev/null && ok "Offenes Recht läuft mit der übernommenen Datenbank." || die "Dienst antwortet nicht: journalctl -u offenes-recht -n 50"
  echo "   Wenn alles passt: Auf dem alten Server /root/umzug.db löschen und alt-abschalten.sh ausführen."
}

case "${1:-}" in
  --zertifikat) zertifikat; pruefen; exit 0 ;;
  --pruefen)    pruefen; exit 0 ;;
  --datenbank)  datenbank "${2:-}"; exit 0 ;;
  "") ;;
  *) die "Unbekannte Option: $1" ;;
esac

cat <<EOF

Offenes Recht – Einrichtung auf $(hostname) ($(public_ip)), $OS_NAME
  Domain:      $DOMAIN (und www.$DOMAIN)
  Version:     $OR_VERSION aus github.com/$REPO (Prüfsumme wird kontrolliert)
  Installiert: python3, nginx, certbot, sqlite3 · Dienste offenes-recht, or-ops, or-ops-mcp
  Daten:       $DATA · Einstellungen $ETC
EOF
ask "Weiter?" || exit 0

say "1. Pakete"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv nginx certbot python3-certbot-nginx sqlite3 curl ca-certificates openssh-client openssl >/dev/null
ok "$(python3 --version), nginx $(nginx -v 2>&1 | sed 's#.*/##')"

say "2. Benutzer und Verzeichnisse"
id offenes-recht >/dev/null 2>&1 || useradd --system --home "$DATA" --shell /usr/sbin/nologin offenes-recht
id orops >/dev/null 2>&1 || useradd --system --home /opt/or-ops-mcp --shell /usr/sbin/nologin orops
install -d -o offenes-recht -g offenes-recht -m 0750 "$DATA" "$DATA/backups"
install -d -o root -g root -m 0755 "$APP" "$APP/static"
install -d -o root -g offenes-recht -m 0750 "$ETC"
install -d -o root -g root -m 0750 "$OPSDATA"
install -d -o orops -g orops -m 0750 "$OPSDATA/incoming"
chmod 0711 "$OPSDATA"
ok "offenes-recht (App), orops (Betriebszugang)"

say "3. Offenes Recht $OR_VERSION"
tmp="$(mktemp -d)"
curl -fsSL -m 60 -o "$tmp/p.tgz" "https://raw.githubusercontent.com/$REPO/pakete/offenes-recht-$OR_VERSION.tgz" || die "Download fehlgeschlagen."
echo "$PKG_SHA  $tmp/p.tgz" | sha256sum -c --quiet - || die "Prüfsumme stimmt nicht – Abbruch."
tar -xzf "$tmp/p.tgz" -C "$tmp"
install -m 0644 "$tmp/offenes-recht/server.py" "$tmp/offenes-recht/seed.json" "$tmp/offenes-recht/VERSION" "$APP/"
install -m 0644 "$tmp/offenes-recht/static/index.html" "$APP/static/"
rm -rf "$tmp"
ok "installiert in $APP (Prüfsumme ok)"

say "4. E-Mail-Versand"
if [[ -f "$ETC/mail.env" ]] && ! ask "Zugangsdaten sind schon hinterlegt. Neu eingeben?"; then
  ok "unverändert"
else
  read -r -p "   SMTP-Server [smtp.ionos.de]: " SMTP_HOST </dev/tty; SMTP_HOST="${SMTP_HOST:-smtp.ionos.de}"
  read -r -p "   Postfach [noreply@$DOMAIN]: " SMTP_USER </dev/tty; SMTP_USER="${SMTP_USER:-noreply@$DOMAIN}"
  while :; do
    read -r -s -p "   Passwort des Postfachs (wird nicht angezeigt): " P1 </dev/tty; echo
    read -r -s -p "   Passwort wiederholen: " P2 </dev/tty; echo
    [[ -n "$P1" && "$P1" == "$P2" ]] && break; warn "Leer oder nicht gleich, bitte nochmals."
  done
  umask 077
  printf 'OR_SMTP_HOST=%s\nOR_SMTP_PORT=587\nOR_SMTP_USER=%s\nOR_SMTP_PASSWORD=%s\nOR_MAIL_FROM=Offenes Recht <%s>\n' \
    "$SMTP_HOST" "$SMTP_USER" "$P1" "$SMTP_USER" > "$ETC/mail.env"
  unset P1 P2; umask 022
  chown root:offenes-recht "$ETC/mail.env"; chmod 0640 "$ETC/mail.env"
  python3 - "$ETC/mail.env" <<'PY' && ok "Anmeldung am SMTP-Server erfolgreich" || warn "Anmeldung am SMTP-Server fehlgeschlagen – später mit erneutem Lauf korrigieren."
import smtplib, sys
e = dict(l.rstrip("\n").split("=", 1) for l in open(sys.argv[1]) if "=" in l)
with smtplib.SMTP(e["OR_SMTP_HOST"], int(e["OR_SMTP_PORT"]), timeout=20) as s:
    s.starttls(); s.login(e["OR_SMTP_USER"], e["OR_SMTP_PASSWORD"])
PY
fi

say "5. Dienst offenes-recht"
[[ -f "$ETC/app.env" ]] || printf 'OR_PUBLIC_URL=https://%s\nOR_SECURE_COOKIE=0\n' "$DOMAIN" > "$ETC/app.env"
chown root:offenes-recht "$ETC/app.env"; chmod 0640 "$ETC/app.env"
install_file /etc/systemd/system/offenes-recht.service 0644 <<EOF
[Unit]
Description=Offenes Recht ($DOMAIN)
After=network-online.target
Wants=network-online.target

[Service]
User=offenes-recht
Group=offenes-recht
EnvironmentFile=$ETC/app.env
EnvironmentFile=-$ETC/mail.env
Environment=OR_DB=$DATA/app.db
ExecStart=/usr/bin/python3 $APP/server.py serve --host 127.0.0.1 --port $PORT --db $DATA/app.db
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$DATA

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now offenes-recht >/dev/null 2>&1
systemctl restart offenes-recht; sleep 2
curl -fsS -m 5 "http://127.0.0.1:$PORT/api/me" >/dev/null && ok "läuft auf 127.0.0.1:$PORT" || die "Dienst antwortet nicht: journalctl -u offenes-recht -n 50"

say "6. Betriebszugang (or-ops und MCP)"
@@FILES@@
systemctl daemon-reload
systemctl enable --now or-ops.socket >/dev/null 2>&1
[[ -x /opt/or-ops-mcp/venv/bin/python ]] || python3 -m venv /opt/or-ops-mcp/venv
/opt/or-ops-mcp/venv/bin/pip install -q --disable-pip-version-check "mcp==1.30.0"
install -d -o root -g orops -m 0750 /etc/or-ops-mcp
[[ -s /etc/or-ops-mcp/token ]] || { python3 -c 'import secrets; print(secrets.token_hex(24))' > /etc/or-ops-mcp/token; }
chown root:orops /etc/or-ops-mcp/token; chmod 0640 /etc/or-ops-mcp/token
TOKEN="$(cat /etc/or-ops-mcp/token)"
systemctl enable --now or-ops-mcp >/dev/null 2>&1; systemctl restart or-ops-mcp; sleep 3
systemctl is-active --quiet or-ops-mcp && ok "MCP-Dienst läuft (127.0.0.1:8791)" || die "MCP-Dienst startet nicht: journalctl -u or-ops-mcp -n 50"

say "7. Webserver"
write_nginx
ok "nginx leitet $DOMAIN an Offenes Recht weiter"
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then ufw allow 'Nginx Full' >/dev/null; ufw allow OpenSSH >/dev/null; ok "Firewall: HTTP, HTTPS und SSH offen"; fi

zertifikat
pruefen

say "Fertig"
cat <<EOF
   Offenes Recht:      https://$DOMAIN   (bis zum Zertifikat: http://$DOMAIN)
   Version:            $(cat $APP/VERSION)
   Betriebszugang:     https://$DOMAIN/$TOKEN/mcp
                       Diese Adresse in claude.ai unter Einstellungen → Konnektoren als eigenen
                       Konnektor hinzufügen (Name z. B. „offenesrecht-ops“). Sie ist geheim:
                       nicht weitergeben, nicht in den Chat schreiben.
   Nächster Schritt:   Datenbank übernehmen (alt-umzug.sh auf dem alten Server, dann hier
                       bash $0 --datenbank <IP des alten Servers>)
EOF
