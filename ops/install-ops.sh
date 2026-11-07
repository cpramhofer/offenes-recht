#!/usr/bin/env bash
# Richtet die Offenes-Recht-Werkzeuge für den ionos-ops-MCP-Server ein (einmalig, als root).
#   bash install-ops.sh
# Danach: zwei Zeilen im ionos-ops-Server ergänzen (wird am Ende angezeigt) und ihn neu starten.
set -euo pipefail
TTY="${OR_TTY:-/dev/tty}"
SRC="$(cd "$(dirname "$0")" && pwd)"
say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
die()  { printf '\n\033[31mAbbruch: %s\033[0m\n' "$*" >&2; exit 1; }
ask()  { local a; read -r -p "  $1 [j/N] " a <"$TTY"; [[ "$a" =~ ^[jJyY]$ ]]; }

[[ $EUID -eq 0 ]] || die "Bitte als root ausführen."
[[ -f /opt/offenes-recht/server.py ]] || die "Offenes Recht ist noch nicht installiert (zuerst install.sh)."
command -v sudo >/dev/null && command -v visudo >/dev/null || die "sudo/visudo fehlt."

say "1. Unter welchem Benutzer läuft der ionos-ops-MCP-Server?"
info "Laufende MCP-Dienste:"
{ systemctl list-units --type=service --no-legend 2>/dev/null | awk '{print $1}' | grep -iE 'mcp|ops' || true; } | while read -r u; do
  printf '    %-32s Benutzer: %s  Verzeichnis: %s\n' "$u" "$(systemctl show -p User --value "$u")" "$(systemctl show -p WorkingDirectory --value "$u")"
done || true
read -r -p "  Dienstname des ionos-ops-Servers: " OPSSVC <"$TTY"
OPSUSER="$(systemctl show -p User --value "$OPSSVC" 2>/dev/null || true)"; OPSUSER="${OPSUSER:-root}"
OPSDIR="$(systemctl show -p WorkingDirectory --value "$OPSSVC" 2>/dev/null || true)"
read -r -p "  Benutzer [$OPSUSER]: " x <"$TTY"; OPSUSER="${x:-$OPSUSER}"
read -r -p "  Verzeichnis des Server-Codes [$OPSDIR]: " x <"$TTY"; OPSDIR="${x:-$OPSDIR}"
id -u "$OPSUSER" >/dev/null 2>&1 || die "Benutzer $OPSUSER existiert nicht."
[[ -d "$OPSDIR" ]] || die "Verzeichnis $OPSDIR existiert nicht."

say "2. Geplante Änderungen"
info "/usr/local/sbin/or-ops                      Hilfsskript (root, nur Offenes-Recht-Aktionen)"
info "/etc/sudoers.d/offenes-recht-ops            $OPSUSER darf nur dieses Skript als root starten"
info "/var/lib/offenes-recht-ops/incoming/        Upload-Ordner für Pakete (schreibbar für $OPSUSER)"
info "$OPSDIR/offenes_recht_ops.py                Werkzeug-Modul"
ask "Einrichten?" || die "Abgebrochen, nichts geändert."

install -o root -g root -m 0755 "$SRC/or-ops" /usr/local/sbin/or-ops
install -d -o root -g root -m 0750 /var/lib/offenes-recht-ops
install -d -o "$OPSUSER" -g root -m 0750 /var/lib/offenes-recht-ops/incoming
# Der Upload-Ordner gehört dem MCP-Benutzer; or-ops liest daraus nur nach Prüfsummen- und Inhaltsprüfung.
[[ "$OPSUSER" != root ]] && chmod 0751 /var/lib/offenes-recht-ops
SUD="$(mktemp)"
echo "$OPSUSER ALL=(root) NOPASSWD: /usr/local/sbin/or-ops" > "$SUD"
echo "$OPSUSER ALL=(root) NOPASSWD: /usr/local/sbin/or-ops *" >> "$SUD"
visudo -cf "$SUD" >/dev/null || { rm -f "$SUD"; die "sudoers-Regel ungültig."; }
install -o root -g root -m 0440 "$SUD" /etc/sudoers.d/offenes-recht-ops; rm -f "$SUD"
install -o "$OPSUSER" -m 0644 "$SRC/offenes_recht_ops.py" "$OPSDIR/offenes_recht_ops.py"
info "Eingerichtet."

say "3. Test"
sudo -u "$OPSUSER" sudo -n /usr/local/sbin/or-ops status && info "Hilfsskript funktioniert für $OPSUSER."

say "4. Im ionos-ops-Server ergänzen (nach der Zeile, in der mcp = FastMCP(...) angelegt wird):"
printf '\n    from offenes_recht_ops import register as register_offenes_recht\n    register_offenes_recht(mcp)\n\n'
info "Danach: systemctl restart $OPSSVC"
info "Die neuen Werkzeuge heißen or_status, or_logs, or_restart, or_upload_package, or_deploy,"
info "or_backup, or_list_backups, or_restore_backup, or_stats, or_gremium_add."
info "Protokoll aller Aktionen: /var/lib/offenes-recht-ops/or-ops.log"
