#!/usr/bin/env python3
"""Erzeugt einrichten.sh: ein Skript, das alle Betriebsdateien eingebettet enthält."""
import hashlib, os, sys
here = os.path.dirname(os.path.abspath(__file__))
def emb(name, target, mode):
    data = open(os.path.join(here, name), encoding="utf-8").read()
    assert "__EOF_OR__" not in data
    return f"install_file {target} {mode} <<'__EOF_OR__'\n{data.rstrip()}\n__EOF_OR__\n"
version, sha = sys.argv[1], sys.argv[2]
tpl = open(os.path.join(here, "einrichten.tpl.sh"), encoding="utf-8").read()
files = "".join([
    emb("or-ops", "/usr/local/sbin/or-ops", "0750"),
    emb("or-ops-socket", "/usr/local/sbin/or-ops-socket", "0750"),
    emb("or-ops.socket", "/etc/systemd/system/or-ops.socket", "0644"),
    emb("or-ops@.service", "/etc/systemd/system/or-ops@.service", "0644"),
    emb("ops_mcp.py", "/opt/or-ops-mcp/ops_mcp.py", "0644"),
    emb("offenes_recht_ops.py", "/opt/or-ops-mcp/offenes_recht_ops.py", "0644"),
    emb("or-ops-mcp.service", "/etc/systemd/system/or-ops-mcp.service", "0644"),
])
out = tpl.replace("@@VERSION@@", version).replace("@@SHA@@", sha).replace("@@FILES@@", files)
open(os.path.join(here, "einrichten.sh"), "w", encoding="utf-8").write(out)
print(hashlib.sha256(out.encode()).hexdigest())
