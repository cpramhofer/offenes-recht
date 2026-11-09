#!/usr/bin/env bash
# Auf dem ALTEN Server als root: Offenes Recht anhalten und eine konsistente Kopie der Datenbank
# nach /root/umzug.db schreiben. Danach auf dem neuen Server: bash einrichten.sh --datenbank <IP>
set -euo pipefail
DB=/var/lib/offenes-recht/app.db
[[ $EUID -eq 0 ]] || { echo "Bitte als root ausführen."; exit 1; }
[[ -f "$DB" ]] || { echo "Keine Datenbank unter $DB."; exit 1; }
read -r -p "Offenes Recht auf diesem Server jetzt anhalten (Ausfall bis zum Umzug)? [j/N] " a; [[ "$a" =~ ^[jJ]$ ]] || exit 0
systemctl stop offenes-recht
python3 - "$DB" /root/umzug.db <<'PY'
import sqlite3, sys
s = sqlite3.connect(sys.argv[1]); d = sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()
c = sqlite3.connect(sys.argv[2])
print("Konten: %d, Beiträge: %d, Bewertungen: %d" % tuple(c.execute(q).fetchone()[0] for q in
      ("SELECT COUNT(*) FROM users", "SELECT COUNT(*) FROM contributions", "SELECT COUNT(*) FROM ratings")))
PY
chmod 0600 /root/umzug.db
echo "Kopie: /root/umzug.db ($(du -h /root/umzug.db | cut -f1)). Der Dienst bleibt gestoppt."
echo "Falls der Umzug abgebrochen wird: systemctl start offenes-recht"
