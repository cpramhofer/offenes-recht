#!/usr/bin/env bash
# Auf dem ALTEN Server als root, NACHDEM der neue Server mit der übernommenen Datenbank läuft:
# Offenes Recht unter der alten Adresse endgültig abschalten. Jeder Schritt wird einzeln bestätigt.
set -euo pipefail
OLD_DOMAIN="${OLD_DOMAIN:-deregulierung.milei.eu}"
[[ $EUID -eq 0 ]] || { echo "Bitte als root ausführen."; exit 1; }
ask() { local a; read -r -p "$1 [j/N] " a; [[ "$a" =~ ^[jJ]$ ]]; }

if ask "1. Dienst offenes-recht stoppen und deaktivieren?"; then
  systemctl disable --now offenes-recht 2>/dev/null || true; echo "   erledigt"
fi

if ask "2. Webserver-Eintrag für $OLD_DOMAIN entfernen (nginx, mit Sicherung)?"; then
  python3 - "$OLD_DOMAIN" <<'PY'
import glob, re, shutil, sys, time, subprocess
dom = sys.argv[1]; changed = []
files = ["/etc/nginx/nginx.conf"] + glob.glob("/etc/nginx/sites-enabled/*") + glob.glob("/etc/nginx/conf.d/*.conf")
for f in files:
    try: s = open(f, encoding="utf-8").read()
    except Exception: continue
    if dom not in s: continue
    out, i, removed = [], 0, 0
    for m in re.finditer(r"server\s*\{", s):
        if m.start() < i: continue
        depth, j = 0, m.end() - 1
        while j < len(s):
            if s[j] == "{": depth += 1
            elif s[j] == "}":
                depth -= 1
                if depth == 0: break
            j += 1
        block = s[m.start():j + 1]
        if re.search(r"server_name[^;]*\b" + re.escape(dom) + r"\b", block):
            out.append(s[i:m.start()]); i = j + 1; removed += 1
    if removed:
        out.append(s[i:]); bak = f + ".bak-" + time.strftime("%Y%m%d-%H%M%S"); shutil.copy2(f, bak)
        open(f, "w", encoding="utf-8").write("".join(out)); changed.append((f, bak, removed))
if not changed: print("   kein Eintrag gefunden"); sys.exit(0)
if subprocess.run(["nginx", "-t"], capture_output=True).returncode != 0:
    for f, bak, _ in changed: shutil.copy2(bak, f)
    print("   nginx-Prüfung fehlgeschlagen, Änderung zurückgenommen"); sys.exit(1)
subprocess.run(["systemctl", "reload", "nginx"], check=True)
for f, bak, n in changed: print(f"   {n} Block/Blöcke aus {f} entfernt (Sicherung {bak})")
PY
fi

if command -v certbot >/dev/null && certbot certificates 2>/dev/null | grep -q "$OLD_DOMAIN"; then
  if ask "3. Zertifikat für $OLD_DOMAIN löschen?"; then certbot delete --non-interactive --cert-name "$OLD_DOMAIN" || true; fi
fi

echo
echo "4. Daten löschen: Datenbank, 30 Sicherungen und Programm unter /var/lib/offenes-recht und /opt/offenes-recht."
echo "   Das enthält personenbezogene Daten (Konten, E-Mail-Adressen). Erst löschen, wenn der neue Server läuft."
read -r -p "   Zum endgültigen Löschen LÖSCHEN eintippen: " a
if [[ "$a" == "LÖSCHEN" ]]; then
  rm -rf /var/lib/offenes-recht /opt/offenes-recht /root/umzug.db /etc/offenes-recht
  rm -f /etc/systemd/system/offenes-recht.service; rm -rf /etc/systemd/system/offenes-recht.service.d
  systemctl daemon-reload; echo "   gelöscht"
else
  echo "   nichts gelöscht"
fi
echo
echo "Der Betriebszugang für Offenes Recht im MILEI-MCP (or-ops) wird damit funktionslos."
echo "Entfernen bei Gelegenheit: systemctl disable --now or-ops.socket; rm /usr/local/sbin/or-ops*"
