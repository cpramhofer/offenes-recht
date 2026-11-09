#!/usr/bin/env bash
# Offenes Recht – Konnektor reparieren (als root auf dem neuen Server).
# 1. App auf 1.8.3: /.well-known/... antwortet mit 404 statt der Startseite (OAuth-Erkennung von claude.ai)
# 2. Betriebszugang: keine Host-/Origin-Sperre mehr für Anfragen von claude.ai
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Bitte als root ausführen."; exit 1; }
DOMAIN=offenesrecht.at

echo "== 1. App aktualisieren"
/usr/local/sbin/or-ops update v1.8.3 5c4dec66938cec46c280043ac25591c714c1900d99bb420b0b6bd179dd1f86f7

echo "== 2. Betriebszugang aktualisieren"
cat > /opt/or-ops-mcp/ops_mcp.py.tmp <<'__EOF_OR__'
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
chmod 0644 /opt/or-ops-mcp/ops_mcp.py.tmp && mv -f /opt/or-ops-mcp/ops_mcp.py.tmp /opt/or-ops-mcp/ops_mcp.py
systemctl restart or-ops-mcp; sleep 3
systemctl is-active --quiet or-ops-mcp && echo "   läuft" || { journalctl -u or-ops-mcp -n 30 --no-pager; exit 1; }

echo "== 3. Prüfung über https://$DOMAIN"
TOKEN="$(cat /etc/or-ops-mcp/token)"
a=$(curl -s -o /dev/null -w "%{http_code}" "https://$DOMAIN/.well-known/oauth-protected-resource")
b=$(curl -s -o /dev/null -w "%{http_code}" -X POST "https://$DOMAIN/$TOKEN/mcp" -H "Origin: https://claude.ai" \
     -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"1"}}}')
echo "   OAuth-Erkennung: $a (soll 404)   Konnektor: $b (soll 200)"
[[ "$a" == 404 && "$b" == 200 ]] && echo "   Alles in Ordnung. Jetzt in claude.ai den Konnektor entfernen und mit derselben Adresse neu hinzufügen." \
  || echo "   Noch nicht in Ordnung – bitte die Ausgabe in den Chat kopieren (ohne die geheime Adresse)."
