#!/usr/bin/env bash
# Baut das Release-Paket offenes-recht-<VERSION>.tgz und gibt die SHA-256-Prüfsumme aus.
set -euo pipefail
cd "$(dirname "$0")"
V="$(cat VERSION)"
python3 build.py >/dev/null
python3 -m py_compile server.py
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
mkdir -p "$T/offenes-recht/static" dist
cp server.py seed.json VERSION "$T/offenes-recht/"
cp static/index.html "$T/offenes-recht/static/"
tar --owner=0 --group=0 --sort=name --mtime='2000-01-01' -C "$T" -cf - offenes-recht | gzip -9 -n > "dist/offenes-recht-$V.tgz"
sha256sum "dist/offenes-recht-$V.tgz"
