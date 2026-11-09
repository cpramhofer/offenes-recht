#!/usr/bin/env bash
# Offenes Recht – Einrichtung auf einem eigenen Server (Ubuntu/Debian), als root ausführen.
#
#   bash einrichten.sh                 Erstinstallation (fragt vor jedem Schritt mit Wirkung nach außen)
#   bash einrichten.sh --zertifikat    nur HTTPS-Zertifikat holen (wenn DNS beim ersten Lauf noch nicht stimmte)
#   bash einrichten.sh --datenbank IP  Datenbank vom alten Server übernehmen (vorher dort alt-umzug.sh ausführen)
#
# Das Skript ist wiederholbar: Bereits Eingerichtetes wird erkannt und nicht doppelt angelegt.
set -euo pipefail
umask 022

DOMAIN="${DOMAIN:-offenesrecht.at}"
EMAIL="${EMAIL:-christoph.pramhofer@neos.eu}"          # nur für Let's Encrypt (Ablaufwarnungen)
OR_VERSION="1.8.3"
PKG_SHA="5c4dec66938cec46c280043ac25591c714c1900d99bb420b0b6bd179dd1f86f7"
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

zertifikat() {
  say "HTTPS-Zertifikat (Let's Encrypt)"
  local names=()
  dns_ok "$DOMAIN" && names+=(-d "$DOMAIN") || warn "$DOMAIN zeigt noch nicht auf diesen Server ($(public_ip))."
  dns_ok "www.$DOMAIN" && names+=(-d "www.$DOMAIN") || warn "www.$DOMAIN zeigt noch nicht auf diesen Server."
  if [[ ${#names[@]} -eq 0 ]]; then
    warn "Kein Zertifikat möglich. DNS-Einträge setzen, ein paar Minuten warten, dann: bash $0 --zertifikat"; return 0
  fi
  certbot --nginx --non-interactive --agree-tos -m "$EMAIL" --redirect "${names[@]}" || die "certbot ist fehlgeschlagen (siehe oben)."
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
  --zertifikat) zertifikat; exit 0 ;;
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
apt-get install -y -qq python3 python3-venv nginx certbot python3-certbot-nginx sqlite3 curl ca-certificates openssh-client >/dev/null
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
install_file /usr/local/sbin/or-ops 0750 <<'__EOF_OR__'
#!/usr/bin/env bash
# or-ops – eng begrenzte Betriebsbefehle für Offenes Recht.
# Läuft als root (systemd-Socket), wird vom Betriebszugang (MCP) aufgerufen.
# Jede Aktion ist fest verdrahtet; Argumente werden streng geprüft. Ausgabe: JSON.
set -euo pipefail
umask 027

SVC=offenes-recht
APP=/opt/offenes-recht
DATA=/var/lib/offenes-recht
DB=$DATA/app.db
OPS=/var/lib/offenes-recht-ops
INCOMING=$OPS/incoming/package.tar.gz
BK=$DATA/backups
PORT="$(systemctl show -p ExecStart "$SVC" 2>/dev/null | grep -oE -- '--port [0-9]+' | awk '{print $2}' | head -n1 || true)"
PORT="${PORT:-8790}"
LOG=$OPS/or-ops.log
REPO=cpramhofer/offenes-recht

j() { python3 -c 'import json,sys; c={"true":True,"false":False}; print(json.dumps({k:c.get(v,v) for k,v in zip(sys.argv[1::2], sys.argv[2::2])}, ensure_ascii=False))' "$@"; }
fail() { j ok false error "$1"; exit 1; }
audit() { echo "$(date -Is) ${SUDO_USER:-?} $*" >> "$LOG"; }
health() { curl -fsS -m 5 "http://127.0.0.1:$PORT/api/me" >/dev/null 2>&1; }
version() { cat "$APP/VERSION" 2>/dev/null || echo unbekannt; }

db_backup() {  # $1 = Kennzeichnung
  mkdir -p "$BK"; local f="$BK/app-$(date +%Y%m%d-%H%M%S)-$1.db"
  [[ -f "$DB" ]] || { echo ""; return; }
  python3 - "$DB" "$f" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2]); src.backup(dst); dst.close(); src.close()
PY
  chown offenes-recht:offenes-recht "$f"; chmod 0640 "$f"
  { ls -1t "$BK"/app-*.db 2>/dev/null || true; } | tail -n +31 | xargs -r rm -f --   # höchstens 30 Sicherungen behalten
  echo "$f"
}

ACTION="${1:-}"; shift || true
case "$ACTION" in
  status)
    st="$(systemctl is-active "$SVC" 2>/dev/null || true)"
    h=nein; health && h=ja
    since="$(systemctl show -p ActiveEnterTimestamp --value "$SVC" 2>/dev/null || true)"
    size="$(stat -c %s "$DB" 2>/dev/null || echo 0)"
    j ok true service "$st" health "$h" version "$(version)" since "$since" port "$PORT" db_bytes "$size"
    ;;

  logs)
    n="${1:-50}"; [[ "$n" =~ ^[0-9]{1,3}$ ]] || fail "lines: Zahl 1–999"
    journalctl -u "$SVC" -n "$n" --no-pager -o short-iso 2>&1 | python3 -c 'import json,sys; print(json.dumps({"ok":True,"lines":sys.stdin.read().splitlines()}, ensure_ascii=False))'
    ;;

  restart)
    audit restart
    systemctl restart "$SVC"
    for i in $(seq 1 20); do health && break; sleep 0.5; done
    health && j ok true message "Neu gestartet, App antwortet." || fail "Neu gestartet, aber die App antwortet nicht. Logs prüfen."
    ;;

  deploy)
    sha="${1:-}"; [[ "$sha" =~ ^[0-9a-f]{64}$ ]] || fail "sha256: 64 Hex-Zeichen erwartet"
    [[ -f "$INCOMING" ]] || fail "Kein hochgeladenes Paket vorhanden."
    actual="$(sha256sum "$INCOMING" | awk '{print $1}')"
    [[ "$actual" == "$sha" ]] || fail "Prüfsumme stimmt nicht (erwartet $sha, erhalten $actual)."
    [[ -f "$APP/server.py" ]] || fail "Offenes Recht ist noch nicht installiert. Erstinstallation mit install.sh."
    stage="$(mktemp -d "$OPS/stage.XXXX")"; trap 'rm -rf "$stage"' EXIT
    # Archiv prüfen: nur erwartete Pfade, keine absoluten Pfade, keine Links
    python3 - "$INCOMING" <<'PY' || fail "Paket enthält unerlaubte Einträge."
import sys, tarfile, re
ok = re.compile(r"^offenes-recht(/[A-Za-z0-9._-]+)*/?$")
with tarfile.open(sys.argv[1]) as t:
    for m in t.getmembers():
        if not ok.match(m.name) or ".." in m.name.split("/") or not (m.isfile() or m.isdir()):
            sys.exit(1)
PY
    tar xzf "$INCOMING" -C "$stage" --no-same-owner
    P="$stage/offenes-recht"
    for f in server.py seed.json static/index.html; do [[ -f "$P/$f" ]] || fail "Paket unvollständig: $f fehlt."; done
    python3 -m py_compile "$P/server.py" 2>/dev/null || fail "server.py hat Syntaxfehler."
    old="$(version)"; new="$(cat "$P/VERSION" 2>/dev/null || echo unbekannt)"
    bkf="$(db_backup vor-deploy)"
    rm -rf "$OPS/prev"; cp -a "$APP" "$OPS/prev"
    install -m 0644 "$P/server.py" "$P/seed.json" "$APP/"
    [[ -f "$P/VERSION" ]] && install -m 0644 "$P/VERSION" "$APP/"
    install -m 0644 "$P/static/index.html" "$APP/static/"
    audit "deploy $old -> $new sha=$sha"
    systemctl restart "$SVC"
    for i in $(seq 1 20); do health && break; sleep 0.5; done
    if health; then
      j ok true message "Version $new läuft (vorher $old)." db_backup "$bkf"
    else
      rm -rf "$APP"; cp -a "$OPS/prev" "$APP"; systemctl restart "$SVC"; sleep 1
      audit "deploy rollback -> $old"
      fail "Neue Version startet nicht. Version $old wiederhergestellt. Datenbank-Sicherung: $bkf"
    fi
    ;;

  update)
    # Paket aus einer GitHub-Release von cpramhofer/offenes-recht laden, Prüfsumme prüfen, dann wie deploy.
    tag="${1:-}"; sha="${2:-}"
    [[ "$tag" =~ ^v([0-9]{1,3}\.[0-9]{1,3}(\.[0-9]{1,3})?)$ ]] || fail "tag: Form v1.2 oder v1.2.3 erwartet"
    ver="${BASH_REMATCH[1]}"
    [[ "$sha" =~ ^[0-9a-f]{64}$ ]] || fail "sha256: 64 Hex-Zeichen erwartet"
    # Quelle 1: GitHub-Release; Quelle 2: Zweig „pakete“ im selben Repository. Die Prüfsumme entscheidet.
    part="$INCOMING.dl"; rm -f "$part"; url=""
    for u in "https://github.com/$REPO/releases/download/$tag/offenes-recht-$ver.tgz" \
             "https://raw.githubusercontent.com/$REPO/pakete/offenes-recht-$ver.tgz"; do
      curl -fsSL --proto '=https' --max-filesize 5242880 -m 60 -o "$part" "$u" 2>/dev/null && { url="$u"; break; }
    done
    [[ -n "$url" ]] || { rm -f "$part"; fail "Paket $tag weder als Release noch im Zweig pakete gefunden."; }
    actual="$(sha256sum "$part" | awk '{print $1}')"
    [[ "$actual" == "$sha" ]] || { rm -f "$part"; fail "Prüfsumme stimmt nicht (erwartet $sha, erhalten $actual)."; }
    mv -f "$part" "$INCOMING"
    audit "update $tag geladen von $url sha=$sha"
    exec "$0" deploy "$sha"
    ;;

  backup)
    f="$(db_backup manuell)"; audit "backup $f"
    [[ -n "$f" ]] && j ok true file "$(basename "$f")" || fail "Keine Datenbank vorhanden."
    ;;

  backups)
    { ls -1t "$BK"/app-*.db 2>/dev/null || true; } | while read -r f; do printf '%s\t%s\t%s\n' "$(basename "$f")" "$(stat -c %s "$f")" "$(stat -c %y "$f" | cut -d. -f1)"; done |
      python3 -c 'import json,sys; print(json.dumps({"ok":True,"backups":[dict(zip(("name","bytes","modified"),l.rstrip("\n").split("\t"))) for l in sys.stdin if l.strip()]}, ensure_ascii=False))'
    ;;

  restore)
    name="${1:-}"; [[ "$name" =~ ^app-[0-9]{8}-[0-9]{6}-[a-z-]+\.db$ && -f "$BK/$name" ]] || fail "Unbekannte Sicherung."
    cur="$(db_backup vor-restore)"
    systemctl stop "$SVC"
    rm -f "$DB-wal" "$DB-shm"
    cp -a "$BK/$name" "$DB"; chown offenes-recht:offenes-recht "$DB"
    systemctl start "$SVC"; audit "restore $name (vorher gesichert: $cur)"
    for i in $(seq 1 20); do health && break; sleep 0.5; done
    health && j ok true message "Sicherung $name eingespielt." previous "$(basename "$cur")" || fail "Eingespielt, aber die App antwortet nicht."
    ;;

  stats)
    python3 - "$DB" <<'PY'
import sqlite3, json, sys
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
q = lambda s: c.execute(s).fetchone()[0]
print(json.dumps({"ok": True,
  "konten": {r[0]+("/"+r[1] if r[1] != "active" else ""): r[2] for r in c.execute("SELECT role,status,COUNT(*) FROM users WHERE example=0 GROUP BY role,status")},
  "gremium": q("SELECT COUNT(*) FROM users WHERE gremium=1"),
  "bots_wartend": q("SELECT COUNT(*) FROM users WHERE role='bot' AND status='pending'"),
  "beitraege": q("SELECT COUNT(*) FROM contributions WHERE example=0"),
  "beitraege_beispiel": q("SELECT COUNT(*) FROM contributions WHERE example=1"),
  "bewertungen": q("SELECT COUNT(*) FROM ratings"),
  "paarvergleiche": q("SELECT COUNT(*) FROM pairwise")}, ensure_ascii=False))
PY
    ;;

  gremium-add)
    email="${1:-}"; [[ "$email" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || fail "Ungültige E-Mail-Adresse."
    out="$(sudo -u offenes-recht python3 "$APP/server.py" gremium-add "$email" --db "$DB")"
    audit "gremium-add $email: $out"
    [[ "$out" == Gremiumsrecht* ]] && j ok true message "$out" || fail "$out"
    ;;

  *) fail "Unbekannte Aktion. Erlaubt: status, logs, restart, deploy, update, backup, backups, restore, stats, gremium-add." ;;
esac
__EOF_OR__
install_file /usr/local/sbin/or-ops-socket 0750 <<'__EOF_OR__'
#!/usr/bin/env bash
# Liest genau eine Zeile vom Socket (Felder durch Tabulator getrennt) und reicht sie als
# Argumente an or-ops weiter. Kein eval, keine Shell-Auswertung; or-ops prüft jedes Argument.
set -uo pipefail
IFS= read -r -t 10 -n 512 line || { echo '{"ok": false, "error": "Keine Anfrage erhalten."}'; exit 0; }
IFS=$'\t' read -r -a args <<< "$line"
(( ${#args[@]} >= 1 && ${#args[@]} <= 3 )) || { echo '{"ok": false, "error": "Ungültige Anfrage."}'; exit 0; }
SUDO_USER=orops exec /usr/local/sbin/or-ops "${args[@]}" 2>&1
__EOF_OR__
install_file /etc/systemd/system/or-ops.socket 0644 <<'__EOF_OR__'
[Unit]
Description=Offenes Recht – Betriebs-Socket

[Socket]
ListenStream=/run/or-ops.sock
SocketUser=root
SocketGroup=orops
SocketMode=0660
Accept=yes
MaxConnections=4

[Install]
WantedBy=sockets.target
__EOF_OR__
install_file /etc/systemd/system/or-ops@.service 0644 <<'__EOF_OR__'
[Unit]
Description=Offenes Recht – Betriebsaktion (%i)

[Service]
ExecStart=/usr/local/sbin/or-ops-socket
StandardInput=socket
StandardOutput=socket
StandardError=journal
TimeoutStartSec=0
RuntimeMaxSec=200
__EOF_OR__
install_file /opt/or-ops-mcp/ops_mcp.py 0644 <<'__EOF_OR__'
"""Betriebszugang für Offenes Recht (MCP, Streamable HTTP).

Läuft als eigener, unprivilegierter Benutzer (orops) nur auf 127.0.0.1. nginx reicht ihn unter einer
geheimen Adresse https://<domain>/<token>/mcp durch. Die Werkzeuge schicken ausschließlich fest verdrahtete
Aktionen an den Socket /run/or-ops.sock; dahinter prüft das Skript or-ops jede Aktion und jedes Argument.
Keine Shell, kein Dateizugriff, kein direkter Datenbankzugriff.
"""
import os
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
import offenes_recht_ops

TOKEN = open("/etc/or-ops-mcp/token").read().strip()
# Der Dienst lauscht nur auf 127.0.0.1 und ist ausschließlich über nginx unter der geheimen Adresse erreichbar.
# Die eingebaute Host-/Origin-Prüfung (gedacht für lokal laufende Server im Browser) würde Anfragen von claude.ai abweisen.
mcp = FastMCP("offenesrecht-ops", host="127.0.0.1", port=8791,
              streamable_http_path=f"/{TOKEN}/mcp", stateless_http=True, json_response=True,
              transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
offenes_recht_ops.register(mcp)

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
__EOF_OR__
install_file /opt/or-ops-mcp/offenes_recht_ops.py 0644 <<'__EOF_OR__'
"""Offenes-Recht-Werkzeuge für den Betriebszugang (MCP).

Einbinden in den bestehenden Server (FastMCP):

    from offenes_recht_ops import register
    register(mcp)

Alle Werkzeuge schicken nur eine Aktion an den Socket /run/or-ops.sock. Dahinter startet
systemd das Hilfsskript or-ops als root; es prüft jede Aktion und jedes Argument selbst.
Kein sudo nötig, der MCP-Dienst behält seine Härtung (no_new_privs).
"""
import base64, hashlib, json, os, subprocess

INCOMING = "/var/lib/offenes-recht-ops/incoming/package.tar.gz"
MAX_PACKAGE = 5 * 1024 * 1024


SOCKET = "/run/or-ops.sock"


def _run(*args, timeout=120):
    """Schickt eine Aktion an den or-ops-Dienst (Unix-Socket, nur für die Gruppe orops zugänglich)."""
    import socket
    if any("\t" in a or "\n" in a for a in args):
        return {"ok": False, "error": "Ungültiges Argument."}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(SOCKET)
            s.sendall(("\t".join(args) + "\n").encode())
            s.shutdown(socket.SHUT_WR)
            buf = b""
            while True:
                chunk = s.recv(65536)
                if not chunk: break
                buf += chunk
    except FileNotFoundError:
        return {"ok": False, "error": "or-ops-Dienst nicht eingerichtet (/run/or-ops.sock fehlt)."}
    except PermissionError:
        return {"ok": False, "error": "Kein Zugriff auf /run/or-ops.sock (Gruppe orops?)."}
    except socket.timeout:
        return {"ok": False, "error": "Zeitüberschreitung."}
    out = buf.decode(errors="replace").strip().splitlines()
    try:
        return json.loads(out[-1]) if out else {"ok": False, "error": "keine Ausgabe"}
    except json.JSONDecodeError:
        return {"ok": False, "error": "\n".join(out)[-1000:]}


def register(mcp):
    @mcp.tool()
    def or_status() -> dict:
        """Offenes Recht: Dienststatus, Erreichbarkeit der App, installierte Version, Datenbankgröße."""
        return _run("status")

    @mcp.tool()
    def or_logs(lines: int = 50) -> dict:
        """Offenes Recht: letzte Journal-Zeilen des Dienstes (max. 500)."""
        return _run("logs", str(max(1, min(int(lines), 500))))

    @mcp.tool()
    def or_restart() -> dict:
        """Offenes Recht: Dienst neu starten und prüfen, ob die App antwortet."""
        return _run("restart")

    @mcp.tool()
    def or_upload_package(content_base64: str, sha256: str) -> dict:
        """Offenes Recht: Installationspaket (tar.gz, Base64, max. 5 MB) hochladen. Wird erst mit or_deploy aktiv."""
        try:
            data = base64.b64decode(content_base64, validate=True)
        except Exception:
            return {"ok": False, "error": "Kein gültiges Base64."}
        if len(data) > MAX_PACKAGE:
            return {"ok": False, "error": "Paket größer als 5 MB."}
        actual = hashlib.sha256(data).hexdigest()
        if actual != sha256.lower():
            return {"ok": False, "error": f"Prüfsumme stimmt nicht: {actual}"}
        tmp = INCOMING + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, INCOMING)
        return {"ok": True, "bytes": len(data), "sha256": actual, "next": "or_deploy mit derselben Prüfsumme aufrufen"}

    @mcp.tool()
    def or_deploy(sha256: str) -> dict:
        """Offenes Recht: hochgeladenes Paket prüfen und installieren. Sichert vorher die Datenbank,
        startet neu und stellt die alte Version automatisch wieder her, falls die neue nicht startet.
        Ändert keine Webserver-Konfiguration."""
        return _run("deploy", sha256.lower(), timeout=180)

    @mcp.tool()
    def or_update(tag: str, sha256: str) -> dict:
        """Offenes Recht: Release von github.com/cpramhofer/offenes-recht installieren (z. B. tag="v1.2").
        Lädt offenes-recht-<version>.tgz aus der Release, prüft die Prüfsumme und installiert wie or_deploy
        (Datenbank-Sicherung, Neustart, automatische Rückkehr zur alten Version bei Fehler)."""
        return _run("update", tag.strip(), sha256.lower().strip(), timeout=190)

    @mcp.tool()
    def or_backup() -> dict:
        """Offenes Recht: Datenbank jetzt sichern (es werden höchstens 30 Sicherungen behalten)."""
        return _run("backup")

    @mcp.tool()
    def or_list_backups() -> dict:
        """Offenes Recht: vorhandene Datenbank-Sicherungen auflisten."""
        return _run("backups")

    @mcp.tool()
    def or_restore_backup(name: str) -> dict:
        """Offenes Recht: Datenbank-Sicherung einspielen. Der aktuelle Stand wird vorher gesichert.
        Überschreibt die laufende Datenbank – nur nach ausdrücklicher Zustimmung des Nutzers verwenden."""
        return _run("restore", name)

    @mcp.tool()
    def or_stats() -> dict:
        """Offenes Recht: Kennzahlen (Konten nach Art, wartende Bots, Beiträge, Bewertungen). Keine personenbezogenen Daten."""
        return _run("stats")

    @mcp.tool()
    def or_gremium_add(email: str) -> dict:
        """Offenes Recht: bestehendes Bürgerkonto zum Mitglied des Zulassungsgremiums machen.
        Nur nach ausdrücklicher Zustimmung des Nutzers verwenden."""
        return _run("gremium-add", email)
__EOF_OR__
install_file /etc/systemd/system/or-ops-mcp.service 0644 <<'__EOF_OR__'
[Unit]
Description=Offenes Recht – Betriebszugang (MCP)
After=network.target or-ops.socket

[Service]
User=orops
Group=orops
WorkingDirectory=/opt/or-ops-mcp
ExecStart=/opt/or-ops-mcp/venv/bin/python /opt/or-ops-mcp/ops_mcp.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/offenes-recht-ops/incoming

[Install]
WantedBy=multi-user.target
__EOF_OR__

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
install_file /etc/nginx/sites-available/$DOMAIN 0644 <<EOF
# Offenes Recht – $DOMAIN (certbot ergänzt den HTTPS-Teil)
server {
    listen 80;
    listen [::]:80;
    server_name www.$DOMAIN;
    return 301 \$scheme://$DOMAIN\$request_uri;
}
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    client_max_body_size 8m;
    gzip on;
    gzip_types text/css application/json application/javascript text/xml application/rss+xml;

    # Betriebszugang für Claude: nur unter der geheimen Adresse erreichbar
    location /$TOKEN/mcp {
        proxy_pass http://127.0.0.1:8791;
        proxy_set_header Host 127.0.0.1:8791;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_read_timeout 300s;
    }
    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 120s;
    }
}
EOF
ln -sf /etc/nginx/sites-available/$DOMAIN /etc/nginx/sites-enabled/$DOMAIN
if [[ -e /etc/nginx/sites-enabled/default ]]; then rm -f /etc/nginx/sites-enabled/default; ok "Standardseite von nginx deaktiviert"; fi
nginx -t 2>/dev/null || { nginx -t; die "nginx-Konfiguration fehlerhaft."; }
systemctl enable --now nginx >/dev/null 2>&1; systemctl reload nginx
ok "nginx leitet $DOMAIN an Offenes Recht weiter"
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then ufw allow 'Nginx Full' >/dev/null; ufw allow OpenSSH >/dev/null; ok "Firewall: HTTP, HTTPS und SSH offen"; fi

zertifikat

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
