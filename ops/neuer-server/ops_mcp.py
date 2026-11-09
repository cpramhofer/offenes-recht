"""Betriebszugang für Offenes Recht (MCP, Streamable HTTP).

Läuft als eigener, unprivilegierter Benutzer (orops) nur auf 127.0.0.1. nginx reicht ihn unter einer
geheimen Adresse https://<domain>/<token>/mcp durch. Die Werkzeuge schicken ausschließlich fest verdrahtete
Aktionen an den Socket /run/or-ops.sock; dahinter prüft das Skript or-ops jede Aktion und jedes Argument.
Keine Shell, kein Dateizugriff, kein direkter Datenbankzugriff.
"""
import os
from mcp.server.fastmcp import FastMCP
import offenes_recht_ops

TOKEN = open("/etc/or-ops-mcp/token").read().strip()
mcp = FastMCP("offenesrecht-ops", host="127.0.0.1", port=8791,
              streamable_http_path=f"/{TOKEN}/mcp", stateless_http=True, json_response=True)
offenes_recht_ops.register(mcp)

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
