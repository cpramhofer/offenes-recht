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
