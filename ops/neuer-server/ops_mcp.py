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
