#!/usr/bin/env bash
# Offenes Recht – Installation bzw. Update auf dem Server
# Aufruf (als root):  bash install.sh
# Was passiert:
#   1. prüft Python, sucht die bestehende Konfiguration für $DOMAIN (nginx oder Apache)
#   2. zeigt, was gefunden wurde, und fragt vor jeder Änderung nach
#   3. sichert alte Konfiguration und altes Projekt nach /root/offenes-recht-backup-<Zeit>/
#   4. installiert die App nach /opt/offenes-recht (Daten in /var/lib/offenes-recht) als systemd-Dienst
#   5. leitet $DOMAIN per Reverse-Proxy auf die App um, testet die Konfiguration vor dem Neuladen
# Wiederholtes Ausführen aktualisiert nur die App-Dateien; die Datenbank bleibt erhalten.
set -euo pipefail
TTY="${OR_TTY:-/dev/tty}"

DOMAIN="${DOMAIN:-deregulierung.milei.eu}"
APP_DIR=/opt/offenes-recht
DATA_DIR=/var/lib/offenes-recht
PORT="${PORT:-8790}"
SVC=offenes-recht
SRC="$(cd "$(dirname "$0")" && pwd)"
TS="$(date +%Y%m%d-%H%M%S)"
BACKUP="/root/offenes-recht-backup-$TS"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
die()  { printf '\n\033[31mAbbruch: %s\033[0m\n' "$*" >&2; exit 1; }
ask()  { local a; read -r -p "  $1 [j/N] " a <"$TTY"; [[ "$a" =~ ^[jJyY]$ ]]; }

[[ $EUID -eq 0 ]] || die "Bitte als root ausführen (sudo bash install.sh)."
[[ -f "$SRC/server.py" && -f "$SRC/static/index.html" ]] || die "Paketdateien fehlen neben install.sh."

# ---------- 1. Bestandsaufnahme ----------
say "1. Bestandsaufnahme für $DOMAIN"
command -v python3 >/dev/null || die "python3 fehlt (apt install python3)."
python3 - <<'PY' || die "Python 3.8 oder neuer mit hashlib.scrypt nötig."
import sys, hashlib; assert sys.version_info >= (3, 8); hashlib.scrypt(b"x", salt=b"y", n=2, r=1, p=1)
PY
info "Python: $(python3 --version)"

[[ -d /usr/local/psa ]] && PLESK=1 || PLESK=0
[[ $PLESK -eq 1 ]] && info "Plesk erkannt: Webserver-Konfiguration wird über Plesk gesetzt (siehe Schritt 5)."

WEB=""; CONF=""; OLD_ROOT=""; OLD_PROXY=""
if command -v nginx >/dev/null && nginx -T 2>/dev/null | grep -qE "server_name[^;]*\b${DOMAIN//./\\.}\b"; then
  WEB=nginx
  CONF="$(nginx -T 2>/dev/null | awk -v d="$DOMAIN" '/^# configuration file /{f=$4; sub(/:$/,"",f)} $0 ~ "server_name" && index($0,d){print f}' | sort -u | head -n1)"
  OLD_ROOT="$(awk '/^[[:space:]]*root[[:space:]]/{gsub(/;/,"",$2); print $2; exit}' "$CONF" 2>/dev/null || true)"
  OLD_PROXY="$(grep -oE 'proxy_pass[[:space:]]+[^;]+' "$CONF" 2>/dev/null | awk '{print $2}' | head -n1 || true)"
elif command -v apachectl >/dev/null && apachectl -S 2>/dev/null | grep -q "$DOMAIN"; then
  WEB=apache
  CONF="$(apachectl -S 2>/dev/null | grep "$DOMAIN" | grep -oE '\(/[^:]+' | tr -d '(' | sort -u | head -n1)"
  OLD_ROOT="$(grep -iE '^[[:space:]]*DocumentRoot' "$CONF" | awk '{print $2}' | tr -d '"' | head -n1 || true)"
  OLD_PROXY="$(grep -ioE 'ProxyPass[[:space:]]+/[[:space:]]+[^[:space:]]+' "$CONF" | awk '{print $3}' | head -n1 || true)"
fi

if [[ -n "$WEB" ]]; then
  info "Webserver:        $WEB"
  info "Konfiguration:    $CONF"
  info "Altes Webroot:    ${OLD_ROOT:-–}"
  info "Alter Proxy:      ${OLD_PROXY:-–}"
  if [[ "$OLD_PROXY" =~ :([0-9]+) ]]; then
    OLD_PORT="${BASH_REMATCH[1]}"
    info "Prozess an Port $OLD_PORT: $(ss -ltnpH "sport = :$OLD_PORT" 2>/dev/null | grep -oE 'users:\(\("[^"]+' | sed 's/users:(("//' | head -n1 || echo unbekannt)"
  fi
else
  info "Keine Webserver-Konfiguration für $DOMAIN gefunden (nginx/Apache)."
fi
if [[ "${OLD_ROOT:-}" == "$APP_DIR"* || "${OLD_PROXY:-}" == *":$PORT"* ]]; then
  info "→ $DOMAIN zeigt bereits auf Offenes Recht. Es wird nur aktualisiert."
  UPDATE_ONLY=1
else
  UPDATE_ONLY=0
fi
ss -ltnH "sport = :$PORT" 2>/dev/null | grep -q . && ! systemctl is-active -q "$SVC" && die "Port $PORT ist belegt. Anderen Port mit PORT=… bash install.sh wählen."

ask "Mit der Installation fortfahren?" || die "Vom Benutzer abgebrochen. Es wurde nichts geändert."

# ---------- 2. Sicherung ----------
say "2. Sicherung nach $BACKUP"
mkdir -p "$BACKUP"
[[ -n "$CONF" && -f "$CONF" ]] && cp -aL "$CONF" "$BACKUP/" && info "Konfiguration gesichert."
if [[ -d "$DATA_DIR" ]]; then cp -a "$DATA_DIR" "$BACKUP/data" && info "Bestehende Datenbank gesichert."; fi
if [[ $UPDATE_ONLY -eq 0 && -n "${OLD_ROOT:-}" && -d "$OLD_ROOT" ]]; then
  tar czf "$BACKUP/altes-projekt.tar.gz" -C "$(dirname "$OLD_ROOT")" "$(basename "$OLD_ROOT")"
  info "Altes Projekt archiviert: $BACKUP/altes-projekt.tar.gz ($(du -h "$BACKUP/altes-projekt.tar.gz" | cut -f1))"
fi

# ---------- 3. App installieren ----------
say "3. App installieren"
id -u offenes-recht >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin offenes-recht
mkdir -p "$APP_DIR/static" "$DATA_DIR"
install -m 0644 "$SRC/server.py" "$SRC/seed.json" "$APP_DIR/"
install -m 0644 "$SRC/static/index.html" "$APP_DIR/static/"
chown -R offenes-recht:offenes-recht "$DATA_DIR"; chmod 0750 "$DATA_DIR"

cat > /etc/systemd/system/$SVC.service <<EOF
[Unit]
Description=Offenes Recht – Pilot ($DOMAIN)
After=network.target

[Service]
User=offenes-recht
Group=offenes-recht
Environment=OR_DB=$DATA_DIR/app.db
Environment=OR_SECURE_COOKIE=1
ExecStart=/usr/bin/env python3 $APP_DIR/server.py serve --host 127.0.0.1 --port $PORT --db $DATA_DIR/app.db
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q "$SVC"
systemctl restart "$SVC"
for i in $(seq 1 20); do curl -fsS "http://127.0.0.1:$PORT/api/me" >/dev/null 2>&1 && break; sleep 0.5; done
curl -fsS "http://127.0.0.1:$PORT/api/me" >/dev/null || { journalctl -u "$SVC" -n 30 --no-pager; die "Die App startet nicht (Log oben)."; }
info "Dienst $SVC läuft auf 127.0.0.1:$PORT."

# ---------- 4. Gremiumskonto ----------
if ! sudo -u offenes-recht python3 - "$DATA_DIR/app.db" <<'PY'
import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); sys.exit(0 if c.execute("select 1 from users where gremium=1").fetchone() else 1)
PY
then
  say "4. Erstes Mitglied des Zulassungsgremiums anlegen"
  read -r -p "  E-Mail: " GEMAIL <"$TTY"
  read -r -p "  Name:   " GNAME <"$TTY"
  sudo -u offenes-recht env OR_PASSWORD="${OR_PASSWORD:-}" python3 "$APP_DIR/server.py" user-add "$GEMAIL" "$GNAME" --db "$DATA_DIR/app.db" <"$TTY"
  sudo -u offenes-recht python3 "$APP_DIR/server.py" gremium-add "$GEMAIL" --db "$DATA_DIR/app.db"
else
  say "4. Gremiumskonto vorhanden – übersprungen"
fi

# ---------- 5. Webserver ----------
say "5. Webserver"
if [[ $UPDATE_ONLY -eq 1 ]]; then
  info "Bereits eingerichtet, nichts zu tun."
elif [[ $PLESK -eq 1 ]]; then
  info "In Plesk: Websites & Domains → $DOMAIN → Apache & nginx → „Zusätzliche nginx-Anweisungen“ eintragen:"
  printf '\n    location / {\n        proxy_pass http://127.0.0.1:%s;\n        proxy_set_header Host $host;\n        proxy_set_header X-Forwarded-For $remote_addr;\n        proxy_set_header X-Forwarded-Proto $scheme;\n    }\n\n' "$PORT"
  info "und „Proxymodus“ deaktivieren. Danach ist die App unter https://$DOMAIN erreichbar."
elif [[ "$WEB" == nginx ]]; then
  CERT="$(grep -E '^[[:space:]]*ssl_certificate[[:space:]]' "$CONF" | head -n1 | awk '{print $2}' | tr -d ';' || true)"
  KEY="$(grep -E '^[[:space:]]*ssl_certificate_key[[:space:]]' "$CONF" | head -n1 | awk '{print $2}' | tr -d ';' || true)"
  V6="$(grep -q "\[::\]" "$CONF" && echo 1 || true)"
  OTHER="$(grep -E '^[[:space:]]*server_name' "$CONF" | grep -v "$DOMAIN" || true)"
  [[ -n "$OTHER" ]] && die "$CONF enthält weitere Domains. Bitte den server-Block für $DOMAIN manuell ersetzen (Vorlage: $SRC/nginx.conf). Sicherung liegt in $BACKUP."
  info "Neue Konfiguration für $CONF (Zertifikat: ${CERT:-keines gefunden})."
  ask "Konfiguration jetzt ersetzen?" || die "Abgebrochen. App läuft, Webserver unverändert. Sicherung: $BACKUP"
  {
    if [[ -n "$CERT" && -n "$KEY" ]]; then
      echo "server { listen 80; ${V6:+listen [::]:80;} server_name $DOMAIN; return 301 https://\$host\$request_uri; }"
      echo "server { listen 443 ssl http2; ${V6:+listen [::]:443 ssl http2;} server_name $DOMAIN;"
      echo "  ssl_certificate $CERT; ssl_certificate_key $KEY;"
      [[ -f /etc/letsencrypt/options-ssl-nginx.conf ]] && echo "  include /etc/letsencrypt/options-ssl-nginx.conf;"
    else
      echo "server { listen 80; ${V6:+listen [::]:80;} server_name $DOMAIN;"
    fi
    sed "s/__PORT__/$PORT/" "$SRC/nginx.conf"
    echo "}"
  } > "$CONF"
  if ! nginx -t 2>/tmp/or-nginx-test; then
    cp -a "$BACKUP/$(basename "$CONF")" "$CONF"; cat /tmp/or-nginx-test
    die "nginx-Test fehlgeschlagen, alte Konfiguration wiederhergestellt."
  fi
  systemctl reload nginx
  if [[ -z "$CERT" ]]; then
    if command -v certbot >/dev/null && ask "HTTPS-Zertifikat mit certbot einrichten?"; then certbot --nginx -d "$DOMAIN" --redirect
    else info "ACHTUNG: kein HTTPS. Die Anmeldung funktioniert nur über HTTPS (sichere Cookies). Zertifikat einrichten: certbot --nginx -d $DOMAIN"; fi
  fi
  info "nginx neu geladen."
elif [[ "$WEB" == apache ]]; then
  a2enmod -q proxy proxy_http headers >/dev/null
  info "Proxy-Anweisungen werden in $CONF eingefügt; DocumentRoot wird deaktiviert."
  ask "Konfiguration jetzt ändern?" || die "Abgebrochen. App läuft, Webserver unverändert. Sicherung: $BACKUP"
  sed -i -E "s|^(\s*)(DocumentRoot.*)|\1# \2  # deaktiviert durch Offenes Recht $TS|I" "$CONF"
  sed -i -E "s|^(\s*)</VirtualHost>|\1  ProxyPreserveHost On\n\1  RequestHeader set X-Forwarded-Proto expr=%{REQUEST_SCHEME}\n\1  ProxyPass / http://127.0.0.1:$PORT/\n\1  ProxyPassReverse / http://127.0.0.1:$PORT/\n\1</VirtualHost>|" "$CONF"
  if ! apachectl configtest 2>/tmp/or-apache-test; then
    cp -a "$BACKUP/$(basename "$CONF")" "$CONF"; cat /tmp/or-apache-test
    die "Apache-Test fehlgeschlagen, alte Konfiguration wiederhergestellt."
  fi
  systemctl reload apache2 2>/dev/null || systemctl reload httpd
  info "Apache neu geladen."
else
  info "Bitte $DOMAIN im Webserver auf http://127.0.0.1:$PORT weiterleiten (Vorlage: $SRC/nginx.conf)."
fi

# ---------- 6. Altes Projekt entfernen ----------
if [[ $UPDATE_ONLY -eq 0 && -n "${OLD_ROOT:-}" && -d "$OLD_ROOT" ]]; then
  say "6. Altes Projekt"
  case "$OLD_ROOT" in
    /var/www/*|/srv/*|/home/*/public_html*|/var/www/vhosts/*) ;;
    *) info "$OLD_ROOT liegt außerhalb üblicher Webverzeichnisse und bleibt unangetastet."; OLD_ROOT="";;
  esac
  if [[ -n "$OLD_ROOT" ]] && grep -rqs -- "$OLD_ROOT" /etc/nginx /etc/apache2 /etc/httpd 2>/dev/null; then
    info "$OLD_ROOT wird noch von einer anderen Webserver-Konfiguration verwendet und bleibt erhalten."; OLD_ROOT=""
  fi
  if [[ -n "$OLD_ROOT" ]] && ask "Altes Webroot $OLD_ROOT löschen? (Archiv liegt in $BACKUP)"; then
    rm -rf --one-file-system -- "$OLD_ROOT"; info "Gelöscht."
  fi
fi
if [[ $UPDATE_ONLY -eq 0 && -n "${OLD_PORT:-}" && "$OLD_PORT" != "$PORT" ]]; then
  OLD_UNIT="$(systemctl status "$(ss -ltnpH "sport = :$OLD_PORT" | grep -oE 'pid=[0-9]+' | head -n1 | cut -d= -f2)" 2>/dev/null | head -n1 | grep -oE '[a-zA-Z0-9@_.-]+\.service' || true)"
  if [[ -n "$OLD_UNIT" && "$OLD_UNIT" != milei.service && "$OLD_UNIT" != milei-mcp.service ]] && ask "Alten Dienst $OLD_UNIT (Port $OLD_PORT) stoppen und deaktivieren?"; then
    systemctl disable --now "$OLD_UNIT"; info "$OLD_UNIT gestoppt und deaktiviert."
  fi
fi

say "Fertig."
info "App:       https://$DOMAIN"
info "Dienst:    systemctl status $SVC   ·   Log: journalctl -u $SVC -f"
info "Daten:     $DATA_DIR/app.db"
info "Sicherung: $BACKUP"
info "Weitere Gremiumsmitglieder: sudo -u offenes-recht python3 $APP_DIR/server.py gremium-add <email> --db $DATA_DIR/app.db"
