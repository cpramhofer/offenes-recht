#!/usr/bin/env python3
"""Offenes Recht – Pilot-Server (nur Python-Standardbibliothek).

Start:   python3 server.py serve [--port 8790] [--db /var/lib/offenes-recht/app.db]
Gremium: python3 server.py gremium-add <email>     (macht ein bestehendes Konto zum Gremiumsmitglied)
         python3 server.py user-add <email> <name> (legt ein Bürgerkonto an, Passwort wird abgefragt)
"""
import argparse, getpass, hashlib, hmac, json, math, os, re, secrets, sqlite3, sys, threading, time
from http import cookies
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
DB_PATH = os.environ.get("OR_DB", os.path.join(HERE, "data", "app.db"))
SESSION_DAYS = 30
COOKIE = "or_sess"
SECURE_COOKIE = os.environ.get("OR_SECURE_COOKIE", "1") == "1"
_local = threading.local()

ROLES = ("citizen", "org", "bot")
RATE_VALUES = {"h": 1.0, "t": 0.5, "n": 0.0}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, email TEXT NOT NULL, name TEXT NOT NULL,
  pw_hash TEXT NOT NULL, role TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
  gremium INTEGER NOT NULL DEFAULT 0, verified INTEGER NOT NULL DEFAULT 0, example INTEGER NOT NULL DEFAULT 0,
  org_register TEXT, bot_principal TEXT, bot_mandate TEXT, bot_operator TEXT, bot_model TEXT,
  api_key_hash TEXT, api_key_once TEXT, decided_by INTEGER, decided_at INTEGER, decision_note TEXT,
  created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS contributions(
  id INTEGER PRIMARY KEY, key TEXT NOT NULL, type TEXT NOT NULL, text TEXT NOT NULL, why TEXT,
  w_quote TEXT, w_from INTEGER, w_to INTEGER, user_id INTEGER NOT NULL,
  seed_h INTEGER DEFAULT 0, seed_t INTEGER DEFAULT 0, seed_n INTEGER DEFAULT 0,
  seed_pw_wins REAL DEFAULT 0, seed_pw_n INTEGER DEFAULT 0, example INTEGER DEFAULT 0,
  via TEXT DEFAULT 'web', created INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_contrib_key ON contributions(key);
CREATE TABLE IF NOT EXISTS ratings(user_id INTEGER, cid INTEGER, v TEXT, created INTEGER, PRIMARY KEY(user_id, cid));
CREATE TABLE IF NOT EXISTS pairwise(user_id INTEGER, a INTEGER, b INTEGER, winner INTEGER, created INTEGER, PRIMARY KEY(user_id, a, b));
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, actor INTEGER, action TEXT, target INTEGER, note TEXT, created INTEGER);
CREATE TABLE IF NOT EXISTS laws(
  gnr TEXT PRIMARY KEY, kurztitel TEXT, titel TEXT, abk TEXT, typ TEXT, tgroup TEXT, idx TEXT,
  kundmachung TEXT, inkraft TEXT, eli TEXT, n_norms INTEGER, norms_listed INTEGER DEFAULT 0, synced INTEGER);
CREATE INDEX IF NOT EXISTS ix_laws_tgroup ON laws(tgroup);
CREATE TABLE IF NOT EXISTS norms(
  nor TEXT PRIMARY KEY, gnr TEXT NOT NULL, pid TEXT NOT NULL, label TEXT, sort INTEGER, inkraft TEXT,
  xml_url TEXT, title TEXT, body TEXT, fetched INTEGER);
CREATE INDEX IF NOT EXISTS ix_norms_gnr ON norms(gnr, sort);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY, cid INTEGER NOT NULL, user_id INTEGER NOT NULL, reason TEXT NOT NULL, note TEXT,
  created INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'open', decided_by INTEGER, decided_at INTEGER, UNIQUE(cid, user_id));
CREATE TABLE IF NOT EXISTS resets(token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires INTEGER NOT NULL, used INTEGER DEFAULT 0);
"""

# ---------- Hilfsfunktionen ----------
def db():
    c = getattr(_local, "db", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA foreign_keys=ON")
        _local.db = c
    return c

def now(): return int(time.time())

def hash_pw(pw):
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$" + salt.hex() + "$" + h.hex()

def check_pw(pw, stored):
    try:
        _, salt, h = stored.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(calc.hex(), h)
    except Exception:
        return False

def sha(s): return hashlib.sha256(s.encode()).hexdigest()

def wilson(pos, n, z=1.96):
    """Untere und obere Grenze des 95-%-Konfidenzintervalls (Wilson)."""
    if n == 0: return 0.0, 1.0
    p = pos / n
    d = 1 + z*z/n
    c = p + z*z/(2*n)
    m = z*math.sqrt(p*(1-p)/n + z*z/(4*n*n))
    return max(0.0, (c-m)/d), min(1.0, (c+m)/d)

def bradley_terry(items, comps, iters=200):
    """items: Liste von IDs; comps: Liste (winner, loser, weight). Gibt Stärken (Summe 1) zurück."""
    if not items: return {}
    s = {i: 1.0/len(items) for i in items}
    wins = {i: 0.01 for i in items}  # Glättung gegen Division durch null
    pairs = {}
    for w, l, wt in comps:
        wins[w] += wt
        k = tuple(sorted((w, l))); pairs[k] = pairs.get(k, 0) + wt
    for _ in range(iters):
        new = {}
        for i in items:
            den = sum(n/(s[i]+s[j]) for (a, b), n in pairs.items() for j in ([b] if a == i else [a] if b == i else []))
            new[i] = wins[i]/den if den else s[i]
        t = sum(new.values()); s = {i: v/t for i, v in new.items()}
    return s

# ---------- Datenmodell ----------
def init_db(seed=True):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = db(); c.executescript(SCHEMA)
    cols = [r[1] for r in c.execute("PRAGMA table_info(contributions)")]
    if "base_text" not in cols:
        c.execute("ALTER TABLE contributions ADD COLUMN base_text TEXT")
    if "removed_at" not in cols:
        c.execute("ALTER TABLE contributions ADD COLUMN removed_at INTEGER")
        c.execute("ALTER TABLE contributions ADD COLUMN removed_by INTEGER")
        c.execute("ALTER TABLE contributions ADD COLUMN removed_reason TEXT")
        c.execute("ALTER TABLE contributions ADD COLUMN removed_note TEXT")
    ucols = [r[1] for r in c.execute("PRAGMA table_info(users)")]
    if "consent_at" not in ucols:
        c.execute("ALTER TABLE users ADD COLUMN consent_at INTEGER")
        c.execute("ALTER TABLE users ADD COLUMN consent_version TEXT")
    # E-Mail nur für Bürger- und Organisationskonten eindeutig; mehrere Bots dürfen dieselbe Adresse des Auftraggebers nutzen
    usql = (c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone() or [""])[0]
    if "email TEXT UNIQUE" in usql:
        c.commit(); c.execute("PRAGMA foreign_keys=OFF")
        c.execute("BEGIN")
        c.execute(usql.replace("CREATE TABLE users(", "CREATE TABLE users_new(", 1).replace("email TEXT UNIQUE NOT NULL", "email TEXT NOT NULL", 1))
        c.execute("INSERT INTO users_new SELECT * FROM users")
        c.execute("DROP TABLE users"); c.execute("ALTER TABLE users_new RENAME TO users")
        c.execute("COMMIT"); c.execute("PRAGMA foreign_keys=ON")
    c.execute("CREATE INDEX IF NOT EXISTS ix_users_email ON users(email)")
    for ix in ("CREATE UNIQUE INDEX IF NOT EXISTS ux_users_email_person ON users(email) WHERE role!='bot'",
               "CREATE UNIQUE INDEX IF NOT EXISTS ux_users_botname ON users(lower(name)) WHERE role='bot' AND status!='deleted'"):
        try: c.execute(ix)
        except sqlite3.Error as e: sys.stderr.write("Index nicht angelegt (doppelte Einträge?): %r\n" % e)
    if "limit_hour" not in ucols:
        c.execute("ALTER TABLE users ADD COLUMN limit_hour INTEGER")
        c.execute("ALTER TABLE users ADD COLUMN limit_day INTEGER")
    # Einmalig (7.11.2026, auf Anweisung von Christoph Pramhofer): fiktive Beispielbeiträge und Beispielkonten des Pilots entfernen
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone() and not c.execute("SELECT 1 FROM meta WHERE k='mig_remove_examples'").fetchone():
        ex = [r[0] for r in c.execute("SELECT id FROM contributions WHERE example=1")]
        for cid in ex:
            c.execute("DELETE FROM ratings WHERE cid=?", (cid,))
            c.execute("DELETE FROM pairwise WHERE a=? OR b=? OR winner=?", (cid, cid, cid))
        c.execute("DELETE FROM contributions WHERE example=1")
        n_u = c.execute("DELETE FROM users WHERE example=1 AND id NOT IN (SELECT user_id FROM contributions)").rowcount
        c.execute("INSERT INTO audit(actor,action,target,note,created) VALUES(NULL,'remove-examples',NULL,?,?)",
                  (f"{len(ex)} Beispielbeiträge und {n_u} Beispielkonten entfernt", now()))
        c.execute("INSERT OR REPLACE INTO meta VALUES('mig_remove_examples', ?)", (str(now()),))
    # Einmalig (7.11.2026, auf Anweisung von Christoph Pramhofer): Auftraggeber des Bots „Federico“
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone() and not c.execute("SELECT 1 FROM meta WHERE k='mig_federico_principal'").fetchone():
        r = c.execute("SELECT id, bot_principal FROM users WHERE role='bot' AND name='Federico' AND example=0").fetchone()
        if r:
            g = c.execute("SELECT id FROM users WHERE gremium=1 ORDER BY id LIMIT 1").fetchone()
            c.execute("UPDATE users SET bot_principal='Christoph Pramhofer' WHERE id=?", (r[0],))
            c.execute("INSERT INTO audit(actor,action,target,note,created) VALUES(?,?,?,?,?)",
                      (g[0] if g else None, "gremium:profile", r[0], f"Auftraggeber: {r[1]} → Christoph Pramhofer", now()))
        c.execute("INSERT OR REPLACE INTO meta VALUES('mig_federico_principal', ?)", (str(now()),))
    # Einmalig (7.11.2026, auf Anweisung des Gremiumsmitglieds Christoph Pramhofer): Bot „Federico“ 100 Beiträge pro Stunde
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone() and not c.execute("SELECT 1 FROM meta WHERE k='mig_federico_limit'").fetchone():
        r = c.execute("SELECT id FROM users WHERE role='bot' AND name='Federico' AND example=0").fetchone()
        if r:
            g = c.execute("SELECT id FROM users WHERE gremium=1 ORDER BY id LIMIT 1").fetchone()
            c.execute("UPDATE users SET limit_hour=100, limit_day=2400 WHERE id=?", (r[0],))
            c.execute("INSERT INTO audit(actor,action,target,note,created) VALUES(?,?,?,?,?)",
                      (g[0] if g else None, "gremium:limit", r[0], "100 pro Stunde, 2.400 pro Tag", now()))
        c.execute("INSERT OR REPLACE INTO meta VALUES('mig_federico_limit', ?)", (str(now()),))
    # Schlüssel aus Version 0.2 ("74/abs1") auf Gesetz + Paragraph umstellen ("10007517/p74/abs1")
    c.execute("UPDATE contributions SET key='10007517/p'||key WHERE key NOT GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]/*'")
    c.commit()
    # Beispielbeiträge des Pilots wurden am 7.11.2026 entfernt; neue Installationen starten leer.
    if seed and os.environ.get("OR_LOAD_EXAMPLES") == "1" and not c.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        load_seed(c)
    # Beispielbeiträge: Ausgangstext nachtragen (für korrekte Diffs nach Novellen)
    try:
        data = json.load(open(os.path.join(HERE, "seed.json"), encoding="utf-8"))
        for sd in data["SEED"]:
            if sd.get("base"):
                c.execute("UPDATE contributions SET base_text=? WHERE example=1 AND key=? AND text=? AND base_text IS NULL", (sd["base"], sd["key"], sd["text"]))
        c.commit()
    except Exception as e:
        sys.stderr.write("Seed-Basistext: %r\n" % e)

def load_seed(c):
    data = json.load(open(os.path.join(HERE, "seed.json"), encoding="utf-8"))
    bot = data["BOT1"]; ids = {}
    def user(name, kind, extra):
        if name in ids: return ids[name]
        email = "beispiel-%d@offenes-recht.invalid" % (len(ids)+1)
        cur = c.execute("""INSERT INTO users(email,name,pw_hash,role,status,verified,example,org_register,
             bot_principal,bot_mandate,bot_operator,bot_model,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (email, name, "!", kind, "active", 1, 1, extra.get("reg"), extra.get("principal"), extra.get("mandate"),
             extra.get("operator"), extra.get("model"), now()))
        ids[name] = cur.lastrowid; return ids[name]
    for s in data["SEED"]:
        extra = {}
        if s["kind"] == "bot":
            extra = {"principal": bot["auftraggeber"], "mandate": bot["auftrag"], "model": bot["modell"], "operator": "Institut für Legistik (Beispiel)"}
        if s["kind"] == "org":
            extra = {"reg": (s.get("verified") or "").replace("Organisation, ", "")}
        uid = user(s["author"], s["kind"], extra)
        g = [a+b for a, b in zip(s["r"]["g1"], s["r"]["g2"])]
        pw = s.get("pw") or {"wins": 0, "n": 0}
        w = s.get("w") or {}
        c.execute("""INSERT INTO contributions(key,type,text,why,w_quote,w_from,w_to,user_id,seed_h,seed_t,seed_n,
                     seed_pw_wins,seed_pw_n,example,created,base_text) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                  (s["key"], s["type"], s["text"], s.get("why"), w.get("quote"), w.get("from"), w.get("to"), uid,
                   g[0], g[1], g[2], pw["wins"], pw["n"], now()-86400*7, s.get("base")))
    c.commit()

def public_user(r):
    if r is None: return None
    if r["status"] == "deleted":
        return {"id": r["id"], "name": "Gelöschtes Konto", "role": "deleted", "status": "deleted", "verified": False, "example": False}
    u = {"id": r["id"], "name": r["name"], "role": r["role"], "status": r["status"], "verified": bool(r["verified"]),
         "example": bool(r["example"])}
    if r["role"] == "org": u["register"] = r["org_register"]
    if r["role"] == "bot":
        u["bot"] = {"principal": r["bot_principal"], "mandate": r["bot_mandate"], "operator": r["bot_operator"],
                    "model": r["bot_model"], "approved_at": r["decided_at"]}
    return u

def me_view(r):
    u = public_user(r)
    u.update({"email": r["email"], "gremium": bool(r["gremium"])})
    if r["role"] == "bot" and r["api_key_once"]:
        u["api_key_once"] = r["api_key_once"]
    u["can_post"] = r["status"] == "active"
    u["can_rate"] = r["status"] == "active" and r["role"] == "citizen"
    return u

def contribution_stats(c, rows):
    """Bewertungen ohne Gruppen: Zustimmungsquote mit Wilson-Untergrenze; Paarvergleich je Stelle (Bradley-Terry)."""
    ids = [r["id"] for r in rows]
    agg = {i: [0, 0, 0] for i in ids}
    if ids:
        q = ",".join("?"*len(ids))
        for r in c.execute(f"SELECT cid, v, COUNT(*) n FROM ratings WHERE cid IN ({q}) GROUP BY cid, v", ids):
            agg[r["cid"]]["htn".index(r["v"])] += r["n"]
    out = {}
    for r in rows:
        h, t, n = agg[r["id"]]
        h += r["seed_h"]; t += r["seed_t"]; n += r["seed_n"]
        tot = h+t+n; pos = h + 0.5*t
        lo, hi = wilson(pos, tot)
        share = pos/tot if tot else 0
        if tot < 10: status = "Neu"
        elif lo >= 0.55: status = "Breite Zustimmung"
        elif hi < 0.45: status = "Überwiegend abgelehnt"
        else: status = "Offen"
        out[r["id"]] = {"h": h, "t": t, "n": n, "total": tot, "share": round(share, 4), "lower": round(lo, 4),
                        "upper": round(hi, 4), "status": status, "score": round(lo, 4)}
    # Paarvergleich je Stelle
    props = {}
    for r in rows:
        if r["type"] == "prop": props.setdefault(r["key"], []).append(r)
    for key, lst in props.items():
        if len(lst) < 2: continue
        pid = [r["id"] for r in lst]
        comps = []
        q = ",".join("?"*len(pid))
        for p in c.execute(f"SELECT a,b,winner FROM pairwise WHERE a IN ({q}) AND b IN ({q})", pid+pid):
            if p["winner"] == 0:
                comps += [(p["a"], p["b"], .5), (p["b"], p["a"], .5)]
            else:
                comps.append((p["winner"], p["b"] if p["winner"] == p["a"] else p["a"], 1))
        # Beispielwerte: gegen die jeweils anderen Vorschläge verteilt
        for r in lst:
            if r["seed_pw_n"]:
                others = [x for x in pid if x != r["id"]]
                for o in others:
                    comps.append((r["id"], o, r["seed_pw_wins"]/len(others)))
        n_comp = sum(1 for _ in c.execute(f"SELECT 1 FROM pairwise WHERE a IN ({q})", pid)) + max((r["seed_pw_n"] for r in lst), default=0)
        st = bradley_terry(pid, comps)
        ranked = sorted(pid, key=lambda i: -st[i])
        for rank, i in enumerate(ranked, 1):
            out[i]["pw"] = {"rank": rank, "of": len(pid), "strength": round(st[i], 4), "comparisons": n_comp}
    return out

def key_label(c, key, _cache={}):
    """'10007517/p359b/abs5' -> 'GewO 1994 § 359b Abs. 5' (für Listen über mehrere Gesetze)."""
    parts = key.split("/")
    law = c.execute("SELECT abk, kurztitel FROM laws WHERE gnr=?", (parts[0],)).fetchone()
    n = c.execute("SELECT label FROM norms WHERE gnr=? AND pid=?", (parts[0], parts[1])).fetchone() if len(parts) > 1 else None
    out = ((law["abk"] or law["kurztitel"]) + " ") if law else ""
    out += n["label"] if n else (parts[1] if len(parts) > 1 else "")
    for p in parts[2:]:
        m = re.match(r"^(abs|z|lit)(\w+?)(_\d+)?$", p)
        if m: out += {"abs": " Abs. ", "z": " Z ", "lit": " lit. "}[m.group(1)] + m.group(2)
    return out

REMOVE_REASONS = {"illegal": "Rechtswidriger Inhalt", "attack": "Persönlicher Angriff", "spam": "Spam oder Werbung",
                  "offtopic": "Kein Bezug zur Norm", "bot": "Nicht gekennzeichneter Bot", "other": "Sonstiger Verstoß gegen die Regeln"}

def serialize(rows, stats, users):
    res = []
    for r in rows:
        if r["removed_at"]:
            res.append({"id": r["id"], "key": r["key"], "type": r["type"], "text": "", "why": None, "w": None, "author": None,
                        "example": False, "via": r["via"], "base": None, "where": key_label(db(), r["key"]), "created": r["created"],
                        "stats": {"h": 0, "t": 0, "n": 0, "total": 0, "share": 0, "lower": 0, "upper": 1, "status": "Entfernt", "score": -1},
                        "removed": {"at": r["removed_at"], "reason": REMOVE_REASONS.get(r["removed_reason"], r["removed_reason"]), "note": r["removed_note"]}})
            continue
        res.append({"id": r["id"], "key": r["key"], "type": r["type"], "text": r["text"], "why": r["why"],
                    "w": {"quote": r["w_quote"], "from": r["w_from"], "to": r["w_to"]} if r["w_quote"] else None,
                    "author": users.get(r["user_id"]), "example": bool(r["example"]), "via": r["via"], "base": r["base_text"],
                    "where": key_label(db(), r["key"]),
                    "created": r["created"], "stats": stats[r["id"]]})
    return res

def all_users(c):
    return {r["id"]: public_user(r) for r in c.execute("SELECT * FROM users")}

def log(actor, action, target=None, note=None):
    db().execute("INSERT INTO audit(actor,action,target,note,created) VALUES(?,?,?,?,?)", (actor, action, target, note, now()))

# ---------- RIS (Rechtsinformationssystem des Bundes) ----------
import urllib.request, urllib.parse, xml.etree.ElementTree as ET, datetime
from concurrent.futures import ThreadPoolExecutor

RIS_API = "https://data.bka.gv.at/ris/api/v2.6/Bundesrecht"
RIS_UA = "OffenesRecht-Pilot/0.3 (+https://deregulierung.milei.eu)"
RIS_DELAY = float(os.environ.get("OR_RIS_DELAY", "0.4"))   # Pause zwischen Anfragen im Hintergrund
PILOT_GNR = "10007517"   # Gewerbeordnung 1994
_ris_lock = threading.Lock()
_prefetching = set()
_pool = ThreadPoolExecutor(max_workers=3)
NS = "{http://www.bka.gv.at}"

def ris_get(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": RIS_UA, "Accept": "application/json, text/xml"})
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e; time.sleep(1.5 * (attempt + 1))
    raise last

def ris_search(params):
    q = {"Applikation": "BrKons", "DokumenteProSeite": "OneHundred", "FassungVom": datetime.date.today().isoformat()}
    q.update(params)
    d = json.loads(ris_get(RIS_API + "?" + urllib.parse.urlencode(q)))
    r = d.get("OgdSearchResult", {})
    if "Error" in r: raise RuntimeError("RIS: %s" % r["Error"])
    res = r.get("OgdDocumentResults", {})
    hits = int((res.get("Hits") or {}).get("#text", 0))
    docs = res.get("OgdDocumentReference") or []
    if isinstance(docs, dict): docs = [docs]
    return hits, docs

def _meta(doc):
    m = doc["Data"]["Metadaten"]; b = m.get("Bundesrecht", {}); k = b.get("BrKons", {})
    refs = (doc["Data"].get("Dokumentliste") or {}).get("ContentReference") or []
    if isinstance(refs, dict): refs = [refs]
    main = next((r for r in refs if r.get("ContentType") == "MainDocument"), refs[0] if refs else {})
    urls = (main.get("Urls") or {}).get("ContentUrl") or []
    if isinstance(urls, dict): urls = [urls]
    xml = next((u["Url"] for u in urls if u.get("DataType") == "Xml"), None)
    idx = (k.get("Indizes") or {}).get("item")
    if isinstance(idx, list): idx = idx[0] if idx else None
    return {"nor": m["Technisch"]["ID"], "gnr": k.get("Gesetzesnummer"), "kurztitel": (b.get("Kurztitel") or "").strip(),
            "titel": re.sub(r"<br\s*/?>", " · ", b.get("Titel") or ""), "abk": k.get("Abkuerzung"), "typ": k.get("Typ") or "",
            "idx": idx, "kundmachung": k.get("Kundmachungsorgan"), "inkraft": k.get("Inkrafttretensdatum"),
            "eli": b.get("Eli"), "label": k.get("ArtikelParagraphAnlage") or "", "xml": xml}

def type_group(t):
    t = (t or "").strip()
    if t.startswith("Vertrag"): return "vertrag"
    if t in ("V", "Vo") or t.startswith("V ") or t.startswith("V("): return "v"
    if t in ("BG", "BVG", "B-VG", "G", "F") or t.endswith("G"): return "g"
    if t.startswith("K"): return "k"
    return "sonst"

def pid_of(label):
    """'§ 74a' -> 'p74a', 'Art. 1' -> 'art1', 'Anl. 7' -> 'anl7'; sonst bereinigt."""
    l = label.strip()
    m = re.match(r"^(§|Art\.?|Artikel|Anl\.?|Anlage)\s*(.*)$", l)
    pre, rest = ({"§": "p"}.get(m.group(1), "art" if m and m.group(1).startswith("Art") else "anl"), m.group(2)) if m else ("x", l)
    rest = re.sub(r"[^0-9A-Za-z]", "", rest).lower() or "0"
    return pre + rest

def sort_key(label, i):
    m = re.match(r"^(§|Art|Anl)\D*(\d+)([a-z]*)", label.strip())
    if not m: return 300_000_000 + i
    base = {"§": 0, "Art": 100_000_000, "Anl": 200_000_000}[m.group(1)]
    suf = sum((ord(ch) - 96) * (27 ** (2 - n)) for n, ch in enumerate(m.group(3)[:3]))
    return base + int(m.group(2)) * 20000 + suf

def sync_catalog():
    """Alle in Kraft stehenden Rechtsvorschriften (je eine § 0-Norm) laden."""
    c = sqlite3.connect(DB_PATH, timeout=30); page = 1; n = 0; total = None
    while True:
        hits, docs = ris_search({"Abschnitt.Von": "0", "Abschnitt.Bis": "0", "Abschnitt.Typ": "Paragraph", "Seitennummer": page})
        total = hits
        for d in docs:
            try: x = _meta(d)
            except Exception as e:
                sys.stderr.write("RIS-Katalog, Eintrag übersprungen: %r\n" % e); continue
            if not x["gnr"]: continue
            c.execute("""INSERT INTO laws(gnr,kurztitel,titel,abk,typ,tgroup,idx,kundmachung,inkraft,eli,synced) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(gnr) DO UPDATE SET kurztitel=excluded.kurztitel,titel=excluded.titel,abk=excluded.abk,typ=excluded.typ,
                         tgroup=excluded.tgroup,idx=excluded.idx,kundmachung=excluded.kundmachung,inkraft=excluded.inkraft,eli=excluded.eli,synced=excluded.synced""",
                      (x["gnr"], x["kurztitel"], x["titel"], x["abk"], x["typ"], type_group(x["typ"]), x["idx"], x["kundmachung"], x["inkraft"], x["eli"], now()))
            n += 1
        c.commit()
        if page * 100 >= hits or not docs: break
        page += 1; time.sleep(RIS_DELAY)
    all_hits, _ = ris_search({"DokumenteProSeite": "Ten"})
    c.execute("INSERT OR REPLACE INTO meta VALUES('catalog_synced',?)", (str(now()),))
    c.execute("INSERT OR REPLACE INTO meta VALUES('total_laws',?)", (str(total),))
    c.execute("INSERT OR REPLACE INTO meta VALUES('total_norms',?)", (str(all_hits),))
    c.commit(); c.close()
    sys.stderr.write(f"RIS-Katalog: {n} Rechtsvorschriften, {all_hits} Normen\n")

def sweep_counts():
    """Normzahl je Rechtsvorschrift über alle geltenden Normen zählen (Hintergrund, ca. 1.600 Anfragen)."""
    counts = {}; page = 1
    while True:
        hits, docs = ris_search({"Seitennummer": page})
        for d in docs:
            g = ((d["Data"]["Metadaten"].get("Bundesrecht") or {}).get("BrKons") or {}).get("Gesetzesnummer")
            if g: counts[g] = counts.get(g, 0) + 1
        if page * 100 >= hits or not docs: break
        page += 1; time.sleep(RIS_DELAY)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.executemany("UPDATE laws SET n_norms=? WHERE gnr=? AND norms_listed=0", [(v, k) for k, v in counts.items()])
    c.execute("INSERT OR REPLACE INTO meta VALUES('counts_synced',?)", (str(now()),)); c.commit(); c.close()
    sys.stderr.write(f"RIS-Normzahlen: {len(counts)} Rechtsvorschriften gezählt\n")

def list_norms(gnr):
    """Normliste eines Gesetzes aus dem RIS holen (falls noch nicht vorhanden)."""
    c = db()
    if c.execute("SELECT norms_listed FROM laws WHERE gnr=?", (gnr,)).fetchone()[0]: return
    with _ris_lock:
        if db().execute("SELECT norms_listed FROM laws WHERE gnr=?", (gnr,)).fetchone()[0]: return
        page = 1; rows = []
        while True:
            hits, docs = ris_search({"Gesetzesnummer": gnr, "Seitennummer": page})
            for d in docs:
                try: rows.append(_meta(d))
                except Exception as e: sys.stderr.write("RIS-Norm übersprungen: %r\n" % e)
            if page * 100 >= hits or not docs: break
            page += 1
        seen = set()
        for i, x in enumerate(rows):
            pid = pid_of(x["label"])
            if pid in seen: pid = pid + "_" + x["nor"][-4:].lower()
            seen.add(pid)
            c.execute("""INSERT INTO norms(nor,gnr,pid,label,sort,inkraft,xml_url) VALUES(?,?,?,?,?,?,?)
                         ON CONFLICT(nor) DO UPDATE SET pid=excluded.pid,label=excluded.label,sort=excluded.sort,inkraft=excluded.inkraft,xml_url=excluded.xml_url""",
                      (x["nor"], gnr, pid, x["label"], sort_key(x["label"], i), x["inkraft"], x["xml"]))
        c.execute("UPDATE laws SET norms_listed=?, n_norms=? WHERE gnr=?", (now(), len(rows), gnr)); c.commit()

def _txt(el):
    """Text eines XML-Elements ohne Gliederungssymbol; gdash/tab werden zu Zeichen."""
    parts = []
    def walk(e):
        tag = e.tag.replace(NS, "")
        if tag in ("gldsym", "symbol"): pass
        elif tag == "gdash": parts.append("–")
        elif tag == "tab": parts.append(" ")
        elif tag in ("n", "fnz", "fnt"): pass
        else:
            if e.text: parts.append(e.text)
            for ch in e: walk(ch)
        if e is not el and e.tail: parts.append(e.tail)
    walk(el)
    return re.sub(r"\s+", " ", "".join(parts)).strip()

def parse_norm_xml(raw):
    """RIS-XML einer Norm -> Überschriften und adressierbare Einheiten (Absatz, Ziffer, litera)."""
    root = ET.fromstring(raw)
    body = root.find(f"{NS}nutzdaten")
    seq = list(body.iter()) if body is not None else []
    in_text = False; heads = []; units = []; cur_abs = None; cur_z = None; n_free = 0
    consumed = set()
    for e in seq:
        tag = e.tag.replace(NS, "")
        if id(e) in consumed: continue
        if tag == "ueberschrift":
            t = (e.get("typ") or "")
            if t == "titel":
                in_text = (_txt(e) == "Text")
                continue
            if in_text:
                txt = _txt(e)
                if txt: heads.append({"typ": t, "t": txt})
            continue
        if not in_text: continue
        if tag == "absatz":
            for d in e.iter(): consumed.add(id(d))
            typ = e.get("typ") or ""
            txt = _txt(e)
            if not txt or typ in ("erltext",): continue
            m = re.match(r"^\((\d+[a-z]?)\)\s*(.*)$", txt)
            if m and typ in ("abs", "satz", "abbo", "ziffer"):
                cur_abs = "abs" + m.group(1); cur_z = None
                units.append({"a": cur_abs, "n": "(" + m.group(1) + ")", "t": m.group(2), "lvl": 0})
            elif typ in ("schlussteil", "schluss") and cur_abs:
                units.append({"a": cur_abs + "/s" + str(sum(1 for u in units if u["a"].startswith(cur_abs + "/s")) + 1), "n": "", "t": txt, "lvl": 1})
            else:
                n_free += 1; cur_abs = cur_abs or None
                key = (cur_abs + "/t" if cur_abs else "t") + str(n_free)
                units.append({"a": key, "n": "", "t": txt, "lvl": 1 if cur_abs else 0})
        elif tag == "listelem":
            for d in e.iter(): consumed.add(id(d))
            sym_el = e.find(f"{NS}symbol"); sym = (sym_el.text or "").strip() if sym_el is not None else ""
            txt = _txt(e)
            if not txt: continue
            base = cur_abs or "t0"
            m1 = re.match(r"^(\d+[a-z]?)\.$", sym); m2 = re.match(r"^([a-z]{1,3})\)$", sym)
            if m1:
                cur_z = base + "/z" + m1.group(1)
                units.append({"a": cur_z, "n": sym, "t": txt, "lvl": 1})
            elif m2:
                parent = cur_z or base
                units.append({"a": parent + "/lit" + m2.group(1), "n": sym, "t": txt, "lvl": 2 if cur_z else 1})
            else:
                n_free += 1
                units.append({"a": base + "/e" + str(n_free), "n": sym, "t": txt, "lvl": 1})
        elif tag == "schlussteil":
            for d in e.iter(): consumed.add(id(d))
            txt = _txt(e)
            if txt and cur_abs:
                units.append({"a": cur_abs + "/s" + str(sum(1 for u in units if u["a"].startswith(cur_abs + "/s")) + 1), "n": "", "t": txt, "lvl": 1})
    # eindeutige Schlüssel sicherstellen
    seen = {}
    for u in units:
        if u["a"] in seen:
            seen[u["a"]] += 1; u["a"] = u["a"] + "_" + str(seen[u["a"]])
        else: seen[u["a"]] = 1
    title = next((h["t"] for h in reversed(heads) if h["typ"] in ("para", "g1min", "g2min")), None) or (heads[-1]["t"] if heads else "")
    return {"heads": heads, "units": units, "title": title}

def fetch_norm(nor):
    c = db()
    r = c.execute("SELECT * FROM norms WHERE nor=?", (nor,)).fetchone()
    if r is None: return None
    if r["body"]: return json.loads(r["body"])
    raw = ris_get(r["xml_url"])
    try:
        body = parse_norm_xml(raw)
    except Exception as e:
        body = {"heads": [], "units": [{"a": "t1", "n": "", "t": "(Text konnte nicht gelesen werden: %s)" % e, "lvl": 0}], "title": ""}
    c.execute("UPDATE norms SET body=?, title=?, fetched=? WHERE nor=?", (json.dumps(body, ensure_ascii=False), body["title"], now(), nor)); c.commit()
    return body

def prefetch_law(gnr):
    """Alle Normtexte eines Gesetzes im Hintergrund laden (3 parallel, höflich)."""
    if gnr in _prefetching or len(_prefetching) >= 4: return   # höchstens 4 Gesetze gleichzeitig vorladen
    _prefetching.add(gnr)
    def job():
        try:
            nors = [r[0] for r in db().execute("SELECT nor FROM norms WHERE gnr=? AND body IS NULL ORDER BY sort", (gnr,))]
            def one(n):
                try: fetch_norm(n); time.sleep(RIS_DELAY)
                except Exception as e: sys.stderr.write("RIS %s: %r\n" % (n, e))
            list(_pool.map(one, nors))
        finally:
            _prefetching.discard(gnr)
    threading.Thread(target=job, daemon=True).start()

def ris_background():
    """Beim Start: Katalog (wöchentlich) und Normzahlen; GewO vorab laden."""
    try:
        init_db(seed=False)
        c = db()
        last = c.execute("SELECT v FROM meta WHERE k='catalog_synced'").fetchone()
        if not last or now() - int(last[0]) > 7*86400:
            sync_catalog()
        if c.execute("SELECT 1 FROM laws WHERE gnr=?", (PILOT_GNR,)).fetchone():
            list_norms(PILOT_GNR); prefetch_law(PILOT_GNR)
        last = c.execute("SELECT v FROM meta WHERE k='counts_synced'").fetchone()
        if not last or now() - int(last[0]) > 7*86400:
            sweep_counts()
    except Exception as e:
        sys.stderr.write("RIS-Hintergrund: %r\n" % e)

def unit_text(key):
    """Aktueller Text einer Einheit 'gnr/pid/abs1/z2' – für den Ausgangstext eines Vorschlags."""
    parts = key.split("/")
    if len(parts) < 3: return None
    r = db().execute("SELECT body FROM norms WHERE gnr=? AND pid=?", (parts[0], parts[1])).fetchone()
    if not r or not r["body"]: return None
    a = "/".join(parts[2:])
    return next((u["t"] for u in json.loads(r["body"])["units"] if u["a"] == a), None)

# ---------- HTTP ----------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
KEY_RE = re.compile(r"^\d{8}/[a-z]+[0-9a-z_]*(/[a-z]+[0-9a-z_]*){1,4}$")
_attempts = {}

class ApiError(Exception):
    def __init__(self, code, msg): self.code, self.msg = code, msg

def need(cond, code, msg):
    if not cond: raise ApiError(code, msg)

class Handler(BaseHTTPRequestHandler):
    server_version = "OffenesRecht/1.2"

    def log_message(self, fmt, *a):
        # bewusst ohne IP-Adresse (Datenschutz); Fehlersuche über Pfad und Status
        sys.stderr.write("%s\n" % (fmt % a))

    def client_ip(self):
        # nginx setzt X-Real-IP; sonst der letzte (vom Proxy angehängte) Eintrag in X-Forwarded-For
        rip = (self.headers.get("X-Real-IP") or "").strip()
        if rip: return rip
        xff = [x.strip() for x in (self.headers.get("X-Forwarded-For") or "").split(",") if x.strip()]
        return xff[-1] if xff else self.client_address[0]

    def send_cors(self):
        if getattr(self, "cors", False):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Accept, Mcp-Session-Id, MCP-Protocol-Version")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Expose-Headers", "Mcp-Session-Id")
            self.send_header("Access-Control-Max-Age", "86400")

    # --- Antworten
    def send_json(self, code, obj, extra_headers=()):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for k, v in extra_headers: self.send_header(k, v)
        self.send_cors()
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def send_static(self, path):
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.realpath(os.path.join(STATIC, name))
        if not full.startswith(os.path.realpath(STATIC)) or not os.path.isfile(full):
            full = os.path.join(STATIC, "index.html")
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
                 ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}.get(os.path.splitext(full)[1], "application/octet-stream")
        data = open(full, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    # --- Anfrage
    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        need(n <= 100_000, 413, "Anfrage zu groß.")
        try: return json.loads(self.rfile.read(n) or b"{}")
        except Exception: raise ApiError(400, "Ungültiges JSON.")

    def session_user(self):
        c = cookies.SimpleCookie(self.headers.get("Cookie") or "")
        if COOKIE not in c: return None
        row = db().execute("""SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
                              WHERE s.token_hash=? AND s.expires>?""", (sha(c[COOKIE].value), now())).fetchone()
        return row

    def api_key_user(self):
        a = self.headers.get("Authorization") or ""
        if not a.startswith("Bearer "): return None
        row = db().execute("SELECT * FROM users WHERE api_key_hash=? AND role='bot'", (sha(a[7:].strip()),)).fetchone()
        need(row is not None, 401, "Unbekannter API-Schlüssel.")
        need(row["status"] == "active", 403, "Dieser Bot ist nicht (mehr) zugelassen.")
        return row

    def cookie_header(self, token, maxage):
        parts = [f"{COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={maxage}"]
        if SECURE_COOKIE: parts.append("Secure")
        return ("Set-Cookie", "; ".join(parts))

    def do_GET(self): self.dispatch("GET")
    def do_POST(self): self.dispatch("POST")
    def do_OPTIONS(self): self.dispatch("OPTIONS")
    def do_DELETE(self): self.dispatch("DELETE")

    def dispatch(self, method):
        u = urlparse(self.path); path = u.path
        from urllib.parse import parse_qs
        self.query = {k: v[0] for k, v in parse_qs(u.query).items()}
        self.cors = path == "/mcp" or path.startswith("/api/v1/")
        try:
            if self.cors and method == "OPTIONS":
                self.send_response(204); self.send_cors(); self.send_header("Content-Length", "0"); self.end_headers(); return
            if path == "/mcp":
                if method != "POST":
                    return self.send_json(405, {"error": "Der MCP-Server nimmt nur POST (JSON-RPC) entgegen. Anleitung: " + PUBLIC_URL + "/#/api"}, [("Allow", "POST, OPTIONS")])
                return mcp_http(self)
            if path.startswith("/api/v1/"):
                for m, rx, fn in API_PATTERNS:
                    mt = rx.match(path)
                    if mt and m == method:
                        return self.send_json(200, fn(self, *mt.groups()))
                raise ApiError(404, "Unbekannter Endpunkt. Übersicht: " + PUBLIC_URL + "/api/v1/openapi.json")
            if not path.startswith("/api/"):
                need(method == "GET", 405, "Methode nicht erlaubt.")
                return self.send_static(path)
            # Schutz gegen Cross-Site-Anfragen: Web-POSTs brauchen eigenen Header, Bot-API nutzt Bearer-Schlüssel
            if method == "POST" and not path.startswith("/api/v1/"):
                need(self.headers.get("X-OR") == "1", 403, "Fehlender Anfrage-Header.")
            route = ROUTES.get((method, path))
            need(route is not None, 404, "Unbekannter Endpunkt.")
            result = route(self)
            if isinstance(result, tuple): self.send_json(200, result[0], result[1])
            else: self.send_json(200, result)
        except ApiError as e:
            self.send_json(e.code, {"error": e.msg})
        except Exception as e:
            sys.stderr.write("FEHLER %r\n" % e)
            import traceback; traceback.print_exc()
            self.send_json(500, {"error": "Interner Fehler."})

# ---------- Endpunkte ----------
def r_me(h):
    u = h.session_user()
    return {"user": me_view(u) if u else None}

def r_register(h):
    d = h.body(); c = db()
    role = d.get("role")
    need(role in ROLES, 400, "Bitte eine Kontoart wählen.")
    email = (d.get("email") or "").strip().lower(); name = (d.get("name") or "").strip()
    pw = d.get("password") or ""
    need(EMAIL_RE.match(email), 400, "Bitte eine gültige E-Mail-Adresse angeben.")
    need(2 <= len(name) <= 80, 400, "Bitte einen Namen oder ein Pseudonym (2–80 Zeichen) angeben.")
    need(len(pw) >= 10, 400, "Das Passwort braucht mindestens 10 Zeichen.")
    need(d.get("consent") is True, 400, "Bitte stimmen Sie der Datenschutzerklärung zu.")
    if role == "bot":
        need(not c.execute("SELECT 1 FROM users WHERE role='bot' AND status!='deleted' AND lower(name)=lower(?)", (name,)).fetchone(), 409,
             "Ein Bot mit diesem Namen existiert bereits. Bitte einen anderen Namen wählen.")
    else:
        need(not c.execute("SELECT 1 FROM users WHERE email=? AND role!='bot'", (email,)).fetchone(), 409, "Diese E-Mail-Adresse ist bereits registriert.")
    vals = {"org_register": None, "bot_principal": None, "bot_mandate": None, "bot_operator": None, "bot_model": None}
    status = "active"
    if role == "org":
        vals["org_register"] = (d.get("register") or "").strip()[:120]
        need(len(vals["org_register"]) >= 3, 400, "Bitte Firmenbuch- oder ZVR-Nummer angeben.")
    if role == "bot":
        for k, label, mn in (("bot_principal", "Auftraggeber", 3), ("bot_mandate", "Auftrag", 20),
                             ("bot_operator", "Betreiber", 3), ("bot_model", "Modell", 2)):
            vals[k] = (d.get(k.replace("bot_", "")) or "").strip()[:2000]
            need(len(vals[k]) >= mn, 400, f"Bitte {label} angeben" + (" (mindestens ein Satz)." if k == "bot_mandate" else "."))
        status = "pending"
    cur = c.execute("""INSERT INTO users(email,name,pw_hash,role,status,org_register,bot_principal,bot_mandate,bot_operator,bot_model,created)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (email, name, hash_pw(pw), role, status, vals["org_register"], vals["bot_principal"],
                     vals["bot_mandate"], vals["bot_operator"], vals["bot_model"], now()))
    c.execute("UPDATE users SET consent_at=?, consent_version=? WHERE id=?", (now(), CONSENT_VERSION, cur.lastrowid))
    log(cur.lastrowid, "register", cur.lastrowid, role); c.commit()
    return start_session(h, cur.lastrowid)

def start_session(h, uid):
    token = secrets.token_urlsafe(32)
    db().execute("INSERT INTO sessions VALUES(?,?,?)", (sha(token), uid, now()+SESSION_DAYS*86400)); db().commit()
    u = db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return {"user": me_view(u)}, [h.cookie_header(token, SESSION_DAYS*86400)]

def r_login(h):
    d = h.body(); ip = h.client_ip()
    lst = [t for t in _attempts.get(ip, []) if t > now()-600]
    need(len(lst) < 10, 429, "Zu viele Anmeldeversuche. Bitte in zehn Minuten erneut versuchen.")
    email = (d.get("email") or "").strip().lower(); pw = d.get("password") or ""
    rows = db().execute("SELECT * FROM users WHERE email=? AND status!='deleted' ORDER BY role='bot', name", (email,)).fetchall()
    ok = [u for u in rows if check_pw(pw, u["pw_hash"])]
    if not ok:
        lst.append(now()); _attempts[ip] = lst
        raise ApiError(401, "E-Mail-Adresse oder Passwort stimmen nicht.")
    ok = [u for u in ok if u["status"] != "suspended"]
    need(ok, 403, "Dieses Konto ist gesperrt.")
    if len(ok) > 1:
        aid = int(d.get("account_id") or 0)
        pick = next((u for u in ok if u["id"] == aid), None)
        if not pick:
            return {"choose": [{"id": u["id"], "name": u["name"], "role": u["role"], "status": u["status"]} for u in ok]}
        return start_session(h, pick["id"])
    return start_session(h, ok[0]["id"])

def r_logout(h):
    c = cookies.SimpleCookie(h.headers.get("Cookie") or "")
    if COOKIE in c: db().execute("DELETE FROM sessions WHERE token_hash=?", (sha(c[COOKIE].value),)); db().commit()
    return {"ok": True}, [h.cookie_header("", 0)]

def r_ack_key(h):
    u = h.session_user(); need(u, 401, "Bitte anmelden.")
    db().execute("UPDATE users SET api_key_once=NULL WHERE id=?", (u["id"],)); db().commit()
    return {"ok": True}

def r_state(h):
    c = db(); u = h.session_user()
    gnr = h.query.get("gnr", "")
    if re.fullmatch(r"\d{8}", gnr):
        rows = c.execute("SELECT * FROM contributions WHERE key LIKE ? ORDER BY id", (gnr + "/%",)).fetchall()
    else:
        rows = c.execute("SELECT * FROM contributions ORDER BY id").fetchall()
    res = {"contributions": serialize(rows, contribution_stats(c, rows), all_users(c)),
           "user": me_view(u) if u else None, "myRatings": {}, "myPairs": []}
    if u:
        res["myRatings"] = {str(r["cid"]): r["v"] for r in c.execute("SELECT cid,v FROM ratings WHERE user_id=?", (u["id"],))}
        res["myPairs"] = [[r["a"], r["b"]] for r in c.execute("SELECT a,b FROM pairwise WHERE user_id=?", (u["id"],))]
    return res

def create_contribution(u, d, via):
    need(u["status"] == "active", 403, "Ihr Konto ist noch nicht freigeschaltet." if u["role"] == "bot" else "Konto nicht aktiv.")
    key = d.get("key") or ""; typ = d.get("type")
    need(KEY_RE.match(key), 400, "Ungültige Stelle im Normtext.")
    need(typ in ("com", "prop"), 400, "Ungültige Beitragsart.")
    text = (d.get("text") or "").strip(); why = (d.get("why") or "").strip()
    need(5 <= len(text) <= 8000, 400, "Bitte mindestens einen kurzen Satz schreiben.")
    if typ == "prop": need(len(why) >= 10, 400, "Bitte den Vorschlag in mindestens einem Satz begründen.")
    w = d.get("w") or {}
    wq = (w.get("quote") or "").strip()[:400] or None
    gnr, pid = key.split("/")[:2]
    norm = db().execute("SELECT nor, body FROM norms WHERE gnr=? AND pid=?", (gnr, pid)).fetchone()
    need(norm is not None, 400, "Diese Stelle gibt es im Normtext nicht.")
    if not norm["body"]: fetch_norm(norm["nor"])
    base = unit_text(key)
    need(base is not None, 400, "Diese Stelle gibt es im Normtext nicht.")
    cur = db().execute("""INSERT INTO contributions(key,type,text,why,w_quote,w_from,w_to,user_id,via,created,base_text)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                       (key, typ, text, why or None, wq, w.get("from") if wq else None, w.get("to") if wq else None, u["id"], via, now(), base))
    db().commit()
    return {"id": cur.lastrowid}

def r_contribute(h):
    u = h.session_user(); need(u, 401, "Bitte anmelden, um einen Beitrag zu schreiben.")
    return create_contribution(u, h.body(), "web")


def r_rate(h):
    u = h.session_user(); need(u, 401, "Bitte anmelden, um zu bewerten.")
    need(u["role"] == "citizen" and u["status"] == "active", 403, "Bewerten dürfen nur Bürgerinnen und Bürger." if u["role"] != "bot" else "Bots dürfen nicht bewerten.")
    d = h.body(); cid = int(d.get("cid") or 0); v = d.get("v")
    row = db().execute("SELECT user_id FROM contributions WHERE id=? AND removed_at IS NULL", (cid,)).fetchone()
    need(row, 404, "Beitrag nicht gefunden."); need(row["user_id"] != u["id"], 403, "Eigene Beiträge sind nicht bewertbar.")
    if v is None: db().execute("DELETE FROM ratings WHERE user_id=? AND cid=?", (u["id"], cid))
    else:
        need(v in RATE_VALUES, 400, "Ungültige Bewertung.")
        db().execute("INSERT OR REPLACE INTO ratings VALUES(?,?,?,?)", (u["id"], cid, v, now()))
    db().commit(); return {"ok": True}

def r_pair(h):
    u = h.session_user(); need(u, 401, "Bitte anmelden.")
    need(u["role"] == "citizen" and u["status"] == "active", 403, "Am Paarvergleich nehmen nur Bürgerinnen und Bürger teil.")
    d = h.body(); a, b = sorted((int(d.get("a") or 0), int(d.get("b") or 0))); win = int(d.get("winner") or 0)
    need(a != b and win in (0, a, b), 400, "Ungültiger Vergleich.")
    rows = db().execute("SELECT key,type,user_id FROM contributions WHERE id IN (?,?) AND removed_at IS NULL", (a, b)).fetchall()
    need(len(rows) == 2 and rows[0]["key"] == rows[1]["key"] and all(r["type"] == "prop" for r in rows), 400, "Nur Vorschläge zur selben Stelle sind vergleichbar.")
    need(all(r["user_id"] != u["id"] for r in rows), 403, "Vergleiche mit eigenen Vorschlägen zählen nicht.")
    db().execute("INSERT OR REPLACE INTO pairwise VALUES(?,?,?,?,?)", (u["id"], a, b, win, now())); db().commit()
    return {"ok": True}

def r_bots(h):
    rows = db().execute("SELECT * FROM users WHERE role='bot' AND status='active' ORDER BY decided_at DESC").fetchall()
    return {"bots": [public_user(r) for r in rows]}

def require_gremium(h):
    u = h.session_user(); need(u and u["gremium"], 403, "Nur für Mitglieder des Zulassungsgremiums.")
    return u

def r_g_pending(h):
    require_gremium(h); c = db()
    pend = c.execute("SELECT * FROM users WHERE (role='bot' AND status='pending') OR (role='org' AND verified=0 AND example=0) ORDER BY created").fetchall()
    hist = c.execute("""SELECT a.*, u.name tname, g.name gname FROM audit a LEFT JOIN users u ON u.id=a.target
                        LEFT JOIN users g ON g.id=a.actor WHERE a.action LIKE 'gremium:%' OR a.action LIKE 'moderation:%' ORDER BY a.id DESC LIMIT 40""").fetchall()
    def full(r):
        x = public_user(r); x["email"] = r["email"]; x["created"] = r["created"]; return x
    active = c.execute("SELECT * FROM users WHERE role='bot' AND status='active' AND example=0 ORDER BY name").fetchall()
    def lim(r):
        x = full(r); lh, ld = bot_limits(r)
        x["limits"] = {"per_hour": lh, "per_day": ld, "custom": bool(r["limit_hour"] or r["limit_day"]),
                       "used_last_hour": c.execute("SELECT COUNT(*) FROM contributions WHERE user_id=? AND created>?", (r["id"], now()-3600)).fetchone()[0],
                       "used_last_day": c.execute("SELECT COUNT(*) FROM contributions WHERE user_id=? AND created>?", (r["id"], now()-86400)).fetchone()[0]}
        return x
    reps = []
    for r in c.execute("""SELECT cid, COUNT(*) n, GROUP_CONCAT(reason) reasons, MIN(created) first FROM reports WHERE status='open'
                          GROUP BY cid ORDER BY n DESC, first""").fetchall():
        cr = c.execute("SELECT * FROM contributions WHERE id=?", (r["cid"],)).fetchone()
        if not cr or cr["removed_at"]:
            c.execute("UPDATE reports SET status='done' WHERE cid=? AND status='open'", (r["cid"],)); continue
        au = c.execute("SELECT name, role FROM users WHERE id=?", (cr["user_id"],)).fetchone()
        notes = [x["note"] for x in c.execute("SELECT note FROM reports WHERE cid=? AND status='open' AND note IS NOT NULL AND note!=''", (r["cid"],))]
        reps.append({"cid": r["cid"], "count": r["n"], "reasons": [REMOVE_REASONS.get(x, x) for x in sorted(set(r["reasons"].split(",")))],
                     "notes": notes[:5], "key": cr["key"], "where": key_label(c, cr["key"]), "type": cr["type"], "text": cr["text"][:600],
                     "author": (au["name"] + (" (KI-Bot)" if au["role"] == "bot" else "")) if au else "", "first": r["first"]})
    c.commit()
    return {"pending": [full(r) for r in pend], "bots": [lim(r) for r in active], "reports": reps, "reasons": REMOVE_REASONS,
            "defaults": {"per_hour": WRITE_LIMIT_PER_HOUR, "per_day": WRITE_LIMIT_PER_DAY},
            "history": [{"action": r["action"].split(":")[1], "target": r["tname"], "by": r["gname"], "note": r["note"], "at": r["created"]} for r in hist]}

def r_g_limit(h):
    g = require_gremium(h); d = h.body(); c = db()
    uid = int(d.get("user_id") or 0)
    t = c.execute("SELECT * FROM users WHERE id=? AND role='bot'", (uid,)).fetchone(); need(t, 404, "Bot nicht gefunden.")
    try: lh = int(d.get("per_hour")); ld = int(d.get("per_day"))
    except (TypeError, ValueError): raise ApiError(400, "Bitte ganze Zahlen angeben.")
    need(1 <= lh <= 1000 and 1 <= ld <= 20000, 400, "Erlaubt: 1–1.000 pro Stunde, 1–20.000 pro Tag.")
    need(ld >= lh, 400, "Das Tageslimit darf nicht kleiner sein als das Stundenlimit.")
    std = (lh == WRITE_LIMIT_PER_HOUR and ld == WRITE_LIMIT_PER_DAY)
    c.execute("UPDATE users SET limit_hour=?, limit_day=? WHERE id=?", (None if std else lh, None if std else ld, uid))
    note = (d.get("note") or "").strip()[:300]
    fmt = lambda n: f"{n:,}".replace(",", ".")
    log(g["id"], "gremium:limit", uid, f"{fmt(lh)} pro Stunde, {fmt(ld)} pro Tag" + (f" · {note}" if note else "")); c.commit()
    return {"ok": True}

_report_hits = {}
def r_report(h):
    u = h.session_user(); need(u, 401, "Bitte anmelden, um einen Beitrag zu melden.")
    need(u["status"] == "active", 403, "Ihr Konto ist nicht aktiv.")
    d = h.body(); c = db(); cid = int(d.get("cid") or 0)
    reason = d.get("reason"); need(reason in REMOVE_REASONS, 400, "Bitte einen Grund wählen.")
    note = (d.get("note") or "").strip()[:1000]
    if reason == "other": need(len(note) >= 10, 400, "Bitte kurz beschreiben, was gegen die Regeln verstößt.")
    r = c.execute("SELECT user_id, removed_at FROM contributions WHERE id=?", (cid,)).fetchone()
    need(r and not r["removed_at"], 404, "Beitrag nicht gefunden.")
    need(r["user_id"] != u["id"], 403, "Eigene Beiträge können Sie unter „Mein Konto“ löschen.")
    need(c.execute("SELECT COUNT(*) FROM reports WHERE user_id=? AND created>?", (u["id"], now() - 86400)).fetchone()[0] < 20, 429,
         "Sie haben heute bereits 20 Meldungen abgegeben.")
    need(not c.execute("SELECT 1 FROM reports WHERE cid=? AND user_id=?", (cid, u["id"])).fetchone(), 409, "Sie haben diesen Beitrag bereits gemeldet.")
    c.execute("INSERT INTO reports(cid,user_id,reason,note,created) VALUES(?,?,?,?,?)", (cid, u["id"], reason, note or None, now())); c.commit()
    return {"ok": True}

def r_g_moderate(h):
    g = require_gremium(h); d = h.body(); c = db()
    cid = int(d.get("cid") or 0); dec = d.get("decision")
    r = c.execute("SELECT * FROM contributions WHERE id=?", (cid,)).fetchone(); need(r, 404, "Beitrag nicht gefunden.")
    note = (d.get("note") or "").strip()[:500]
    if dec == "remove":
        reason = d.get("reason"); need(reason in REMOVE_REASONS, 400, "Bitte einen Grund wählen.")
        need(len(note) >= 10, 400, "Bitte die Entfernung kurz begründen; die Begründung wird öffentlich am Beitrag vermerkt.")
        need(not r["removed_at"], 409, "Der Beitrag ist bereits entfernt.")
        c.execute("UPDATE contributions SET removed_at=?, removed_by=?, removed_reason=?, removed_note=? WHERE id=?", (now(), g["id"], reason, note, cid))
        c.execute("UPDATE reports SET status='upheld', decided_by=?, decided_at=? WHERE cid=? AND status='open'", (g["id"], now(), cid))
        log(g["id"], "moderation:remove", r["user_id"], f"Beitrag {cid} ({key_label(c, r['key'])}): {REMOVE_REASONS[reason]} · {note}")
    elif dec == "dismiss":
        n = c.execute("UPDATE reports SET status='dismissed', decided_by=?, decided_at=? WHERE cid=? AND status='open'", (g["id"], now(), cid)).rowcount
        log(g["id"], "moderation:dismiss", r["user_id"], f"Beitrag {cid} ({key_label(c, r['key'])}): {n} Meldung(en) zurückgewiesen" + (f" · {note}" if note else ""))
    elif dec == "restore":
        need(r["removed_at"], 409, "Der Beitrag ist nicht entfernt.")
        need(len(note) >= 10, 400, "Bitte die Wiederherstellung kurz begründen.")
        c.execute("UPDATE contributions SET removed_at=NULL, removed_by=NULL, removed_reason=NULL, removed_note=NULL WHERE id=?", (cid,))
        log(g["id"], "moderation:restore", r["user_id"], f"Beitrag {cid} ({key_label(c, r['key'])}) wiederhergestellt · {note}")
    else:
        raise ApiError(400, "Ungültige Entscheidung.")
    c.commit(); return {"ok": True}

def r_moderation_log(h):
    """Öffentliches Moderationsprotokoll: Entfernungen und Wiederherstellungen, ohne Inhalte und ohne Namen der Meldenden."""
    c = db()
    removed = [{"id": r["id"], "where": key_label(c, r["key"]), "key": r["key"], "type": "proposal" if r["type"] == "prop" else "comment",
                "at": r["removed_at"], "reason": REMOVE_REASONS.get(r["removed_reason"], r["removed_reason"]), "note": r["removed_note"]}
               for r in c.execute("SELECT * FROM contributions WHERE removed_at IS NOT NULL ORDER BY removed_at DESC LIMIT 200")]
    stats = {"reports_total": c.execute("SELECT COUNT(*) FROM reports").fetchone()[0],
             "reports_open": c.execute("SELECT COUNT(*) FROM reports WHERE status='open'").fetchone()[0],
             "reports_upheld": c.execute("SELECT COUNT(*) FROM reports WHERE status='upheld'").fetchone()[0],
             "reports_dismissed": c.execute("SELECT COUNT(*) FROM reports WHERE status='dismissed'").fetchone()[0],
             "removed": c.execute("SELECT COUNT(*) FROM contributions WHERE removed_at IS NOT NULL").fetchone()[0],
             "restored": c.execute("SELECT COUNT(*) FROM audit WHERE action='moderation:restore'").fetchone()[0]}
    return {"removed": removed, "stats": stats}

def r_renew_key(h):
    u = h.session_user(); need(u, 401, "Bitte anmelden.")
    need(u["role"] == "bot", 403, "Nur KI-Bots haben einen API-Schlüssel.")
    need(u["status"] == "active", 403, "Ihr Bot ist nicht zugelassen.")
    need(check_pw(h.body().get("password") or "", u["pw_hash"]), 403, "Das Passwort stimmt nicht.")
    key = "or_" + secrets.token_urlsafe(32)
    db().execute("UPDATE users SET api_key_hash=?, api_key_once=? WHERE id=?", (sha(key), key, u["id"]))
    log(u["id"], "key-renew", u["id"], "API-Schlüssel erneuert; der alte ist ungültig"); db().commit()
    return {"ok": True}

def r_g_profile(h):
    """Gremium ändert das öffentliche Profil eines Bots; jede Änderung wird mit altem und neuem Wert protokolliert."""
    g = require_gremium(h); d = h.body(); c = db()
    uid = int(d.get("user_id") or 0)
    t = c.execute("SELECT * FROM users WHERE id=? AND role='bot'", (uid,)).fetchone(); need(t, 404, "Bot nicht gefunden.")
    fields = (("bot_principal", "principal", "Auftraggeber", 3), ("bot_mandate", "mandate", "Auftrag", 20),
              ("bot_operator", "operator", "Betreiber", 3), ("bot_model", "model", "Modell", 2))
    changes = []
    for col, key, label, mn in fields:
        if key not in d: continue
        v = (d.get(key) or "").strip()[:2000]
        if v == (t[col] or "").strip(): continue
        need(len(v) >= mn, 400, f"{label}: bitte mindestens {mn} Zeichen.")
        if True:
            c.execute(f"UPDATE users SET {col}=? WHERE id=?", (v, uid))
            changes.append(f"{label}: {(t[col] or '–')[:80]} → {v[:80]}")
    if changes: log(g["id"], "gremium:profile", uid, " · ".join(changes)[:500]); c.commit()
    return {"ok": True, "changed": len(changes)}

def r_g_decide(h):
    g = require_gremium(h); d = h.body(); c = db()
    uid = int(d.get("user_id") or 0); dec = d.get("decision"); note = (d.get("note") or "").strip()[:500]
    t = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone(); need(t, 404, "Konto nicht gefunden.")
    need(t["id"] != g["id"], 403, "Über das eigene Konto entscheidet ein anderes Gremiumsmitglied.")
    if t["role"] == "bot":
        need(dec in ("approve", "reject", "revoke"), 400, "Ungültige Entscheidung.")
        if dec == "approve":
            key = "or_" + secrets.token_urlsafe(32)
            c.execute("UPDATE users SET status='active', verified=1, api_key_hash=?, api_key_once=?, decided_by=?, decided_at=?, decision_note=? WHERE id=?",
                      (sha(key), key, g["id"], now(), note, uid))
        else:
            need(dec != "reject" or note, 400, "Bitte die Ablehnung begründen.")
            c.execute("UPDATE users SET status=?, api_key_hash=NULL, api_key_once=NULL, decided_by=?, decided_at=?, decision_note=? WHERE id=?",
                      ("rejected" if dec == "reject" else "suspended", g["id"], now(), note, uid))
    elif t["role"] == "org":
        need(dec in ("verify", "reject"), 400, "Ungültige Entscheidung.")
        c.execute("UPDATE users SET verified=?, decided_by=?, decided_at=?, decision_note=? WHERE id=?",
                  (1 if dec == "verify" else 0, g["id"], now(), note, uid))
        if dec == "reject": c.execute("UPDATE users SET status='suspended' WHERE id=?", (uid,))
    else:
        raise ApiError(400, "Für Bürgerkonten ist keine Zulassung vorgesehen.")
    log(g["id"], "gremium:" + dec, uid, note); c.commit()
    return {"ok": True}

# ---------- Mail, Passwort, Konto ----------
import smtplib, email.message, email.utils
PUBLIC_URL = os.environ.get("OR_PUBLIC_URL", "https://deregulierung.milei.eu")
CONSENT_VERSION = "2026-11-06"

def send_mail(to, subject, text):
    host = os.environ.get("OR_SMTP_HOST")
    if not host: raise ApiError(503, "Der Mailversand ist noch nicht eingerichtet.")
    m = email.message.EmailMessage()
    m["From"] = os.environ.get("OR_MAIL_FROM", os.environ.get("OR_SMTP_USER", ""))
    m["To"] = to; m["Subject"] = subject; m["Date"] = email.utils.formatdate(localtime=True)
    m["Message-ID"] = email.utils.make_msgid(domain=(os.environ.get("OR_SMTP_USER", "x@offenes-recht").split("@")[-1]))
    m.set_content(text)
    with smtplib.SMTP(host, int(os.environ.get("OR_SMTP_PORT", "587")), timeout=20) as sm:
        sm.starttls(); sm.login(os.environ["OR_SMTP_USER"], os.environ["OR_SMTP_PASSWORD"]); sm.send_message(m)

_reset_hits = {}
def r_reset_request(h):
    d = h.body(); ip = h.client_ip()
    lst = [t for t in _reset_hits.get(ip, []) if t > now()-3600]
    need(len(lst) < 5, 429, "Zu viele Anfragen. Bitte in einer Stunde erneut versuchen.")
    lst.append(now()); _reset_hits[ip] = lst
    email_ = (d.get("email") or "").strip().lower()
    us = db().execute("SELECT * FROM users WHERE email=? AND example=0 AND status NOT IN ('suspended','deleted') ORDER BY role='bot', name", (email_,)).fetchall()
    if us:
        kinds = {"citizen": "Bürgerkonto", "org": "Organisation", "bot": "KI-Bot"}
        lines = []
        for u in us:
            token = secrets.token_urlsafe(32)
            db().execute("INSERT INTO resets(token_hash,user_id,expires) VALUES(?,?,?)", (sha(token), u["id"], now()+3600))
            lines.append((f"„{u['name']}“ ({kinds.get(u['role'], u['role'])}):\n" if len(us) > 1 else "") + f"{PUBLIC_URL}/#/reset/{token}")
        db().commit()
        intro = ("Zu dieser Adresse gehören mehrere Konten. Über den jeweiligen Link legen Sie ein neues Passwort für dieses Konto fest. "
                 if len(us) > 1 else "Über diesen Link können Sie ein neues Passwort festlegen. ")
        try:
            send_mail(email_, "Offenes Recht: Passwort zurücksetzen",
                f"Hallo,\n\n{intro}Jeder Link ist eine Stunde gültig und kann einmal verwendet werden:\n\n" + "\n\n".join(lines) +
                f"\n\nWenn Sie das nicht angefordert haben, ignorieren Sie diese Nachricht; Ihr Passwort bleibt unverändert.\n\nOffenes Recht\n{PUBLIC_URL}\n")
        except ApiError: raise
        except Exception as e:
            sys.stderr.write("Mailversand fehlgeschlagen: %r\n" % e)
            raise ApiError(502, "Die E-Mail konnte gerade nicht versendet werden. Bitte später erneut versuchen.")
    # gleiche Antwort, ob es das Konto gibt oder nicht
    return {"ok": True, "message": "Wenn ein Konto mit dieser Adresse besteht, ist eine E-Mail mit einem Link unterwegs."}

def r_reset(h):
    d = h.body(); token = d.get("token") or ""; pw = d.get("password") or ""
    need(len(pw) >= 10, 400, "Das Passwort braucht mindestens 10 Zeichen.")
    r = db().execute("SELECT * FROM resets WHERE token_hash=? AND used=0 AND expires>?", (sha(token), now())).fetchone()
    need(r, 400, "Der Link ist abgelaufen oder wurde schon verwendet. Bitte einen neuen anfordern.")
    c = db()
    c.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_pw(pw), r["user_id"]))
    c.execute("UPDATE resets SET used=1 WHERE user_id=?", (r["user_id"],))
    c.execute("DELETE FROM sessions WHERE user_id=?", (r["user_id"],))
    log(r["user_id"], "password-reset", r["user_id"]); c.commit()
    return start_session(h, r["user_id"])

def r_change_pw(h):
    u = h.session_user(); need(u, 401, "Bitte anmelden.")
    d = h.body()
    need(check_pw(d.get("old") or "", u["pw_hash"]), 403, "Das bisherige Passwort stimmt nicht.")
    need(len(d.get("new") or "") >= 10, 400, "Das neue Passwort braucht mindestens 10 Zeichen.")
    db().execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_pw(d["new"]), u["id"]))
    db().execute("DELETE FROM sessions WHERE user_id=?", (u["id"],)); db().commit()
    return start_session(h, u["id"])

def r_delete_account(h):
    u = h.session_user(); need(u, 401, "Bitte anmelden.")
    d = h.body(); c = db()
    need(check_pw(d.get("password") or "", u["pw_hash"]), 403, "Das Passwort stimmt nicht.")
    if u["gremium"]:
        need(c.execute("SELECT COUNT(*) FROM users WHERE gremium=1").fetchone()[0] > 1, 409,
             "Sie sind das einzige Mitglied des Zulassungsgremiums. Bitte zuerst ein weiteres Mitglied ernennen.")
    uid = u["id"]
    c.execute("DELETE FROM ratings WHERE user_id=?", (uid,))
    c.execute("DELETE FROM pairwise WHERE user_id=?", (uid,))
    c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    c.execute("DELETE FROM resets WHERE user_id=?", (uid,))
    if d.get("delete_contributions"):
        ids = [r[0] for r in c.execute("SELECT id FROM contributions WHERE user_id=?", (uid,))]
        for i in ids:
            c.execute("DELETE FROM ratings WHERE cid=?", (i,))
            c.execute("DELETE FROM pairwise WHERE a=? OR b=? OR winner=?", (i, i, i))
        c.execute("DELETE FROM contributions WHERE user_id=?", (uid,))
        kept = 0
    else:
        kept = c.execute("SELECT COUNT(*) FROM contributions WHERE user_id=?", (uid,)).fetchone()[0]
    if kept:
        # Konto anonymisieren: Beiträge bleiben unter "Gelöschtes Konto" stehen
        c.execute("""UPDATE users SET email=?, name='Gelöschtes Konto', pw_hash='!', status='deleted', gremium=0, org_register=NULL,
                     bot_principal=NULL, bot_mandate=NULL, bot_operator=NULL, bot_model=NULL, api_key_hash=NULL, api_key_once=NULL,
                     decision_note=NULL WHERE id=?""", ("geloescht-%d-%s@offenes-recht.invalid" % (uid, secrets.token_hex(4)), uid))
    else:
        c.execute("DELETE FROM users WHERE id=?", (uid,))
    c.execute("INSERT INTO audit(actor,action,target,note,created) VALUES(NULL,'account-deleted',?,?,?)", (uid, "Beiträge gelöscht" if d.get("delete_contributions") else f"{kept} Beiträge anonymisiert", now()))
    c.commit()
    return {"ok": True}, [h.cookie_header("", 0)]

GROUP_NAMES = {"g": "Gesetze", "v": "Verordnungen", "vertrag": "Staatsverträge", "k": "Kundmachungen", "sonst": "Sonstige"}

def r_catalog(h):
    c = db(); q = h.query
    where, args = [], []
    if q.get("q"):
        for word in q["q"].split()[:5]:
            where.append("(kurztitel LIKE ? OR abk LIKE ? OR titel LIKE ?)"); args += ["%" + word + "%"] * 3
    if q.get("g") in GROUP_NAMES: where.append("tgroup=?"); args.append(q["g"])
    if q.get("idx"): where.append("idx LIKE ?"); args.append(q["idx"] + "%")
    if q.get("mine") == "1": where.append("gnr IN (SELECT DISTINCT substr(key,1,8) FROM contributions)")
    w = (" WHERE " + " AND ".join(where)) if where else ""
    order = {"norms": "COALESCE(n_norms,0) DESC, kurztitel", "contrib": "nc DESC, kurztitel", "new": "inkraft DESC"}.get(q.get("sort"), "kurztitel COLLATE NOCASE")
    off = max(0, int(q.get("offset") or 0)); lim = min(200, max(1, int(q.get("limit") or 60)))
    total = c.execute("SELECT COUNT(*) FROM laws" + w, args).fetchone()[0]
    rows = c.execute(f"""SELECT l.gnr,l.kurztitel,l.abk,l.typ,l.tgroup,l.idx,l.inkraft,l.n_norms,
                         (SELECT COUNT(*) FROM contributions WHERE key LIKE l.gnr||'/%' AND removed_at IS NULL) nc FROM laws l {w} ORDER BY {order} LIMIT ? OFFSET ?""",
                     args + [lim, off]).fetchall()
    m = {r["k"]: r["v"] for r in c.execute("SELECT * FROM meta")}
    groups = {r["tgroup"]: r["n"] for r in c.execute("SELECT tgroup, COUNT(*) n FROM laws GROUP BY tgroup")}
    return {"total": total, "rows": [dict(r) for r in rows],
            "stats": {"laws": int(m.get("total_laws") or 0) or sum(groups.values()), "norms": int(m.get("total_norms") or 0),
                      "groups": {k: groups.get(k, 0) for k in GROUP_NAMES}, "synced": int(m.get("catalog_synced") or 0),
                      "counted": bool(m.get("counts_synced")), "contributions": c.execute("SELECT COUNT(*) FROM contributions").fetchone()[0]}}

def r_sachgebiete(h):
    rows = db().execute("SELECT idx, COUNT(*) n FROM laws WHERE idx IS NOT NULL GROUP BY idx ORDER BY idx").fetchall()
    return {"items": [{"idx": r["idx"], "n": r["n"]} for r in rows]}

def r_law(h):
    gnr = h.query.get("gnr", ""); need(re.fullmatch(r"\d{8}", gnr), 400, "Ungültige Gesetzesnummer.")
    c = db(); law = c.execute("SELECT * FROM laws WHERE gnr=?", (gnr,)).fetchone(); need(law, 404, "Rechtsvorschrift nicht gefunden.")
    try: list_norms(gnr)
    except Exception as e: raise ApiError(502, "Das RIS antwortet gerade nicht. Bitte später erneut versuchen.")
    prefetch_law(gnr)
    counts = {}
    for r in c.execute("SELECT key FROM contributions WHERE key LIKE ? AND removed_at IS NULL", (gnr + "/%",)):
        p = r["key"].split("/")[1]; counts[p] = counts.get(p, 0) + 1
    norms = [{"pid": r["pid"], "label": r["label"], "title": r["title"], "loaded": bool(r["fetched"]), "inkraft": r["inkraft"], "nc": counts.get(r["pid"], 0)}
             for r in c.execute("SELECT pid,label,title,fetched,inkraft FROM norms WHERE gnr=? ORDER BY sort", (gnr,))]
    law = dict(c.execute("SELECT * FROM laws WHERE gnr=?", (gnr,)).fetchone())
    return {"law": law, "norms": norms, "loading": sum(1 for n in norms if not n["loaded"])}

def r_norm(h):
    gnr, pid = h.query.get("gnr", ""), h.query.get("pid", "")
    need(re.fullmatch(r"\d{8}", gnr) and re.fullmatch(r"[a-z]+[0-9a-z_]*", pid), 400, "Ungültige Angabe.")
    r = db().execute("SELECT nor,label,inkraft FROM norms WHERE gnr=? AND pid=?", (gnr, pid)).fetchone(); need(r, 404, "Norm nicht gefunden.")
    try: body = fetch_norm(r["nor"])
    except Exception: raise ApiError(502, "Das RIS antwortet gerade nicht. Bitte später erneut versuchen.")
    return {"pid": pid, "label": r["label"], "inkraft": r["inkraft"], "nor": r["nor"], **body}

# ---------- Öffentliche API v1 und MCP-Server ----------
# Lesen: frei, ohne Schlüssel (Mengenbegrenzung je IP). Schreiben: nur vom Gremium zugelassene Bots
# mit API-Schlüssel (Authorization: Bearer …). REST und MCP verwenden dieselben Kernfunktionen.
from urllib.parse import unquote

APP_VERSION = "1.2"
READ_LIMIT_PER_MIN = int(os.environ.get("OR_API_READ_LIMIT", "120"))
WRITE_LIMIT_PER_HOUR = int(os.environ.get("OR_API_WRITE_HOUR", "20"))
WRITE_LIMIT_PER_DAY = int(os.environ.get("OR_API_WRITE_DAY", "100"))
TYPE_OUT = {"com": "comment", "prop": "proposal"}
TYPE_IN = {"comment": "com", "proposal": "prop", "com": "com", "prop": "prop", "kommentar": "com", "vorschlag": "prop"}
KIND_OUT = {"citizen": "citizen", "org": "organisation", "bot": "bot", "deleted": "deleted"}
_rl = {}; _rl_lock = threading.Lock()

def rate_limit(h, bot=None):
    k = ("bot", bot["id"]) if bot else ("ip", h.client_ip())
    t = time.time()
    with _rl_lock:
        if len(_rl) > 20000: _rl.clear()
        q = [x for x in _rl.get(k, []) if x > t - 60]
        need(len(q) < READ_LIMIT_PER_MIN, 429, f"Zu viele Anfragen. Erlaubt sind {READ_LIMIT_PER_MIN} pro Minute.")
        q.append(t); _rl[k] = q

def bot_limits(u):
    """Limits eines Bots: vom Gremium festgelegt, sonst Standard."""
    k = u.keys() if hasattr(u, "keys") else []
    lh = u["limit_hour"] if "limit_hour" in k and u["limit_hour"] else WRITE_LIMIT_PER_HOUR
    ld = u["limit_day"] if "limit_day" in k and u["limit_day"] else WRITE_LIMIT_PER_DAY
    return lh, ld

def write_limit(u):
    c = db(); lh, ld = bot_limits(u)
    n_h = c.execute("SELECT COUNT(*) FROM contributions WHERE user_id=? AND created>?", (u["id"], now() - 3600)).fetchone()[0]
    n_d = c.execute("SELECT COUNT(*) FROM contributions WHERE user_id=? AND created>?", (u["id"], now() - 86400)).fetchone()[0]
    need(n_h < lh, 429, f"Limit erreicht: höchstens {lh} Beiträge pro Stunde für diesen Bot.")
    need(n_d < ld, 429, f"Limit erreicht: höchstens {ld} Beiträge pro Tag für diesen Bot. Mehr auf begründeten Antrag beim Gremium.")

def unit_suffix(a):
    out = ""
    for p in a.split("/"):
        m = re.match(r"^(abs|z|lit)([0-9a-z]+?)(_\d+)?$", p)
        if m: out += {"abs": " Abs. ", "z": " Z ", "lit": " lit. "}[m.group(1)] + m.group(2)
        elif re.match(r"^s\d+$", p): out += " Schlussteil"
    return out

def web_url(key): return f"{PUBLIC_URL}/#/g/{key}"
def iso(ts): return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat(timespec="seconds") if ts else None

def law_or_404(gnr):
    need(re.fullmatch(r"\d{8}", gnr or ""), 400, "Ungültige Gesetzesnummer (8 Ziffern, z. B. 10007517).")
    law = db().execute("SELECT * FROM laws WHERE gnr=?", (gnr,)).fetchone()
    need(law, 404, "Rechtsvorschrift nicht gefunden.")
    try: list_norms(gnr)
    except ApiError: raise
    except Exception: raise ApiError(502, "Das RIS antwortet gerade nicht. Bitte später erneut versuchen.")
    return db().execute("SELECT * FROM laws WHERE gnr=?", (gnr,)).fetchone()

def resolve_norm(gnr, paragraph):
    """Paragraph als pid ('p74'), Bezeichnung ('§ 74', 'Art. 1') oder nur Nummer ('74')."""
    law_or_404(gnr)
    p = unquote(str(paragraph or "")).strip()
    need(p, 400, "Bitte einen Paragraphen angeben, z. B. '§ 74' oder 'p74'.")
    c = db()
    r = c.execute("SELECT * FROM norms WHERE gnr=? AND pid=?", (gnr, p.lower())).fetchone()
    if not r:
        label = ("§ " + p) if re.fullmatch(r"\d+[a-z]*", p) else p
        r = c.execute("SELECT * FROM norms WHERE gnr=? AND pid=?", (gnr, pid_of(label))).fetchone()
    need(r, 404, f"'{p}' gibt es in dieser Rechtsvorschrift nicht. get_law bzw. GET /api/v1/laws/{gnr} listet alle Normen.")
    return r

def api_law_brief(r, nc=None):
    return {"gnr": r["gnr"], "short_title": r["kurztitel"], "abbreviation": r["abk"], "type": r["typ"],
            "category": GROUP_NAMES.get(r["tgroup"], r["tgroup"]), "subject_area": r["idx"],
            "in_force_since": r["inkraft"], "norm_count": r["n_norms"],
            **({"contributions": nc} if nc is not None else {}), "url": f"{PUBLIC_URL}/#/g/{r['gnr']}"}

def api_search_laws(query="", category="", subject_area="", limit=20, offset=0):
    c = db(); where, args = [], []
    for word in (query or "").split()[:5]:
        where.append("(kurztitel LIKE ? OR abk LIKE ? OR titel LIKE ?)"); args += ["%" + word + "%"] * 3
    cat = {"law": "g", "laws": "g", "gesetz": "g", "g": "g", "regulation": "v", "verordnung": "v", "v": "v",
           "treaty": "vertrag", "staatsvertrag": "vertrag", "vertrag": "vertrag", "notice": "k", "kundmachung": "k", "k": "k"}.get((category or "").lower())
    need(not category or cat, 400, "category: law, regulation, treaty oder notice.")
    if cat: where.append("tgroup=?"); args.append(cat)
    if subject_area: where.append("idx LIKE ?"); args.append(subject_area + "%")
    w = (" WHERE " + " AND ".join(where)) if where else ""
    limit = min(100, max(1, int(limit or 20))); offset = max(0, int(offset or 0))
    total = c.execute("SELECT COUNT(*) FROM laws" + w, args).fetchone()[0]
    # kurze, exakte Treffer (Abkürzung, Kurztitel) zuerst
    q0 = (query or "").strip()
    order = ("CASE WHEN abk=? OR kurztitel=? THEN 0 WHEN abk LIKE ? OR kurztitel LIKE ? THEN 1 ELSE 2 END, length(kurztitel), kurztitel COLLATE NOCASE"
             if q0 else "kurztitel COLLATE NOCASE")
    oargs = [q0, q0, "%" + q0 + "%", "%" + q0 + "%"] if q0 else []
    rows = c.execute(f"""SELECT l.*, (SELECT COUNT(*) FROM contributions WHERE key LIKE l.gnr||'/%' AND removed_at IS NULL) nc FROM laws l {w}
                         ORDER BY {order} LIMIT ? OFFSET ?""", args + oargs + [limit, offset]).fetchall()
    return {"total": total, "offset": offset, "results": [api_law_brief(r, r["nc"]) for r in rows]}

def api_get_law(gnr):
    law = law_or_404(gnr); c = db()
    prefetch_law(gnr)
    counts = {}
    for r in c.execute("SELECT key FROM contributions WHERE key LIKE ? AND removed_at IS NULL", (gnr + "/%",)):
        p = r["key"].split("/")[1]; counts[p] = counts.get(p, 0) + 1
    norms = [{"paragraph": r["label"], "pid": r["pid"], "title": r["title"], "in_force_since": r["inkraft"],
              "contributions": counts.get(r["pid"], 0)}
             for r in c.execute("SELECT * FROM norms WHERE gnr=? ORDER BY sort", (gnr,))]
    return {**api_law_brief(law, sum(counts.values())), "title": law["titel"], "eli": law["eli"],
            "ris_url": f"https://www.ris.bka.gv.at/GeltendeFassung.wxe?Abfrage=Bundesnormen&Gesetzesnummer={gnr}",
            "norms": norms, "note": "Titel fehlender Normen werden im Hintergrund nachgeladen." if any(n["title"] is None for n in norms) else None}

def api_get_norm(gnr, paragraph):
    r = resolve_norm(gnr, paragraph)
    try: body = fetch_norm(r["nor"])
    except Exception: raise ApiError(502, "Das RIS antwortet gerade nicht. Bitte später erneut versuchen.")
    law = db().execute("SELECT * FROM laws WHERE gnr=?", (gnr,)).fetchone()
    counts = {}
    for x in db().execute("SELECT key FROM contributions WHERE key LIKE ? AND removed_at IS NULL", (f"{gnr}/{r['pid']}/%",)):
        counts[x["key"]] = counts.get(x["key"], 0) + 1
    units = []
    for u in body["units"]:
        key = f"{gnr}/{r['pid']}/{u['a']}"
        units.append({"key": key, "label": r["label"] + unit_suffix(u["a"]), "number": u["n"], "level": u["lvl"],
                      "text": u["t"], "contributions": counts.get(key, 0)})
    return {"gnr": gnr, "law": law["abk"] or law["kurztitel"], "paragraph": r["label"], "pid": r["pid"],
            "title": body.get("title"), "headings": [x["t"] for x in body.get("heads", [])],
            "in_force_since": r["inkraft"], "ris_url": f"https://www.ris.bka.gv.at/Dokumente/Bundesnormen/{r['nor']}/{r['nor']}.html",
            "url": f"{PUBLIC_URL}/#/g/{gnr}/{r['pid']}", "units": units}

def api_contribution(r, st, users, c):
    a = users.get(r["user_id"]) or {}
    author = {"name": a.get("name"), "kind": KIND_OUT.get(a.get("role"), a.get("role")), "verified": a.get("verified", False),
              "example": a.get("example", False)}
    if a.get("role") == "bot": author["bot"] = a.get("bot")
    if a.get("role") == "org": author["register"] = a.get("register")
    rating = {"status": st["status"], "approval": st["share"], "approval_lower_bound": st["lower"], "votes": st["total"],
              "yes": st["h"], "partly": st["t"], "no": st["n"]}
    if st.get("pw"): rating["pairwise"] = {"rank": st["pw"]["rank"], "of": st["pw"]["of"], "comparisons": st["pw"]["comparisons"]}
    cur = unit_text(r["key"])
    out = {"id": r["id"], "key": r["key"], "address": key_label(c, r["key"]), "url": web_url(r["key"]),
           "type": TYPE_OUT[r["type"]], "text": r["text"], "author": author, "created": iso(r["created"]),
           "via": r["via"], "example": bool(r["example"]), "rating": rating}
    if r["type"] == "prop":
        out["reason"] = r["why"]; out["base_text"] = r["base_text"]
        out["norm_changed_since"] = bool(cur and r["base_text"] and cur != r["base_text"])
    if r["w_quote"]: out["quote"] = {"text": r["w_quote"], "word_from": r["w_from"], "word_to": r["w_to"]}
    return out

def _contrib_rows(gnr=None, key=None):
    c = db(); where, args = [], []
    if gnr:
        need(re.fullmatch(r"\d{8}", gnr), 400, "Ungültige Gesetzesnummer."); where.append("key LIKE ?"); args.append(gnr + "/%")
    if key:
        need(re.fullmatch(r"[0-9a-z_/]{8,80}", key), 400, "Ungültiger Schlüssel."); where.append("(key=? OR key LIKE ?)"); args += [key, key + "/%"]
    where.append("removed_at IS NULL")
    return c.execute("SELECT * FROM contributions WHERE " + " AND ".join(where) + " ORDER BY id", args).fetchall()

def api_list_contributions(gnr=None, key=None, type=None, sort="score", limit=20, offset=0):
    c = db(); rows = _contrib_rows(gnr, key)
    st = contribution_stats(c, rows); users = all_users(c)
    t = TYPE_IN.get((type or "").lower()) if type else None
    need(not type or t, 400, "type: comment oder proposal.")
    if t: rows = [r for r in rows if r["type"] == t]
    need(sort in ("score", "new", "old"), 400, "sort: score, new oder old.")
    if sort == "score": rows = sorted(rows, key=lambda r: (-st[r["id"]]["score"], -r["id"]))
    elif sort == "new": rows = sorted(rows, key=lambda r: -r["id"])
    limit = min(100, max(1, int(limit or 20))); offset = max(0, int(offset or 0))
    return {"total": len(rows), "offset": offset, "results": [api_contribution(r, st[r["id"]], users, c) for r in rows[offset:offset + limit]]}

def api_get_contribution(cid):
    c = db(); r = c.execute("SELECT * FROM contributions WHERE id=?", (int(cid),)).fetchone()
    need(r, 404, "Beitrag nicht gefunden.")
    need(not r["removed_at"], 410, "Dieser Beitrag wurde von der Moderation entfernt: " + REMOVE_REASONS.get(r["removed_reason"], r["removed_reason"] or "") + ".")
    rows = c.execute("SELECT * FROM contributions WHERE key=? AND removed_at IS NULL", (r["key"],)).fetchall()
    return api_contribution(r, contribution_stats(c, rows)[r["id"]], all_users(c), c)

def api_top_proposals(gnr=None, limit=10):
    return api_list_contributions(gnr=gnr, type="proposal", sort="score", limit=limit)

def _resolve_key(key):
    key = (key or "").strip()
    need(KEY_RE.match(key), 400, "Ungültiger Schlüssel. Format: Gesetzesnummer/Paragraph/Absatz…, z. B. 10007517/p74/abs2/z1. get_norm liefert die Schlüssel.")
    gnr, pid = key.split("/")[:2]
    resolve_norm(gnr, pid)
    r = db().execute("SELECT nor, body FROM norms WHERE gnr=? AND pid=?", (gnr, pid)).fetchone()
    if not r["body"]:
        try: fetch_norm(r["nor"])
        except Exception: raise ApiError(502, "Das RIS antwortet gerade nicht. Bitte später erneut versuchen.")
    t = unit_text(key)
    need(t is not None, 404, "Diese Stelle gibt es im Normtext nicht. get_norm liefert die gültigen Schlüssel.")
    return key, t

def api_post(bot, key, type, text, reason=None, quote=None):
    need(bot is not None, 401, "Schreiben erfordert den API-Schlüssel eines zugelassenen Bots (Authorization: Bearer …).")
    need(bot["role"] == "bot" and bot["status"] == "active", 403, "Dieser Bot ist nicht zugelassen.")
    key, unit = _resolve_key(key)
    t = TYPE_IN.get((type or "").lower()); need(t, 400, "type: comment oder proposal.")
    w = None
    if quote:
        q = re.sub(r"\s+", " ", quote).strip()
        i = unit.find(q); need(i >= 0, 400, "Das Zitat kommt im Text dieser Stelle nicht wörtlich vor.")
        fr = len(unit[:i].split()) + 1; w = {"quote": q, "from": fr, "to": fr + len(q.split()) - 1}
    if t == "prop": need((text or "").strip() != unit, 400, "Der vorgeschlagene Wortlaut ist identisch mit dem geltenden.")
    write_limit(bot)
    res = create_contribution(bot, {"key": key, "type": t, "text": text, "why": reason, "w": w}, "api")
    log(bot["id"], "api-post", res["id"]); db().commit()
    return api_get_contribution(res["id"])

def api_me(bot):
    need(bot is not None, 401, "Kein API-Schlüssel angegeben (Authorization: Bearer …).")
    c = db()
    return {**public_user(bot), "kind": "bot",
            "limits": {"per_hour": bot_limits(bot)[0], "per_day": bot_limits(bot)[1],
                       "used_last_hour": c.execute("SELECT COUNT(*) FROM contributions WHERE user_id=? AND created>?", (bot["id"], now() - 3600)).fetchone()[0],
                       "used_last_day": c.execute("SELECT COUNT(*) FROM contributions WHERE user_id=? AND created>?", (bot["id"], now() - 86400)).fetchone()[0]},
            "contributions": c.execute("SELECT COUNT(*) FROM contributions WHERE user_id=?", (bot["id"],)).fetchone()[0]}

# --- REST-Endpunkte
def _int(h, k, d):
    try: return int(h.query.get(k, d))
    except ValueError: raise ApiError(400, f"{k}: ganze Zahl erwartet.")
def _bot(h):
    b = h.api_key_user(); rate_limit(h, b); return b

def v1_laws(h):
    _bot(h); q = h.query
    return api_search_laws(q.get("q", ""), q.get("category", ""), q.get("subject_area", ""), _int(h, "limit", 20), _int(h, "offset", 0))
def v1_law(h, gnr): _bot(h); return api_get_law(gnr)
def v1_norm(h, gnr, p): _bot(h); return api_get_norm(gnr, p)
def v1_contribs(h):
    _bot(h); q = h.query
    return api_list_contributions(q.get("gnr"), q.get("key"), q.get("type"), q.get("sort", "score"), _int(h, "limit", 20), _int(h, "offset", 0))
def v1_contrib(h, cid): _bot(h); return api_get_contribution(cid)
def v1_top(h): _bot(h); return api_top_proposals(h.query.get("gnr"), _int(h, "limit", 10))
def v1_me(h): return api_me(_bot(h))
def v1_post(h):
    b = _bot(h); d = h.body()
    w = d.get("w") or {}
    return api_post(b, d.get("key"), d.get("type"), d.get("text"), d.get("reason") or d.get("why"), d.get("quote") or w.get("quote"))
def v1_openapi(h): return openapi_spec()

API_PATTERNS = [
    ("GET", re.compile(r"^/api/v1/laws$"), v1_laws),
    ("GET", re.compile(r"^/api/v1/laws/(\d{8})$"), v1_law),
    ("GET", re.compile(r"^/api/v1/laws/(\d{8})/norms/([^/]+)$"), v1_norm),
    ("GET", re.compile(r"^/api/v1/contributions$"), v1_contribs),
    ("POST", re.compile(r"^/api/v1/contributions$"), v1_post),
    ("GET", re.compile(r"^/api/v1/contributions/(\d+)$"), v1_contrib),
    ("GET", re.compile(r"^/api/v1/proposals/top$"), v1_top),
    ("GET", re.compile(r"^/api/v1/me$"), v1_me),
    ("GET", re.compile(r"^/api/v1/openapi\.json$"), v1_openapi),
]

# --- MCP (Model Context Protocol, Transport „Streamable HTTP“, zustandslos, JSON-Antworten)
MCP_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]
MCP_INSTRUCTIONS = (
    "Offenes Recht: Bürgerplattform zum Kommentieren und Verbessern des österreichischen Bundesrechts "
    "(tagesaktuelle Normtexte aus dem RIS). Ablauf: search_laws → get_law → get_norm. get_norm liefert für jeden "
    "Absatz, jede Ziffer und litera einen 'key'; diesen verwenden list_contributions, post_comment und propose_amendment. "
    "Lesen ist frei. Schreiben nur mit dem API-Schlüssel eines vom Zulassungsgremium zugelassenen Bots; jeder Beitrag "
    "erscheint öffentlich mit Bot-Kennzeichnung und dem Bot-Profil (Auftraggeber, Auftrag, Betreiber, Modell). "
    "Bots bewerten nicht. Bewertungsfrage für Vorschläge: 'Fördert diese Änderung Wachstum und Wohlstand für alle?'")

_RO = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
_WR = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
def _s(t, d, **kw): return {"type": t, "description": d, **kw}
MCP_TOOLS = [
    {"name": "search_laws", "title": "Rechtsvorschriften suchen",
     "description": "Durchsucht alle geltenden Bundes-Rechtsvorschriften (Gesetze, Verordnungen, Staatsverträge, Kundmachungen) nach Titel oder Abkürzung. Liefert die Gesetzesnummer (gnr) für get_law.",
     "inputSchema": {"type": "object", "properties": {
         "query": _s("string", "Suchbegriff(e), z. B. 'Gewerbeordnung', 'ASVG', 'Miete'"),
         "category": _s("string", "Optional: law, regulation, treaty oder notice", enum=["law", "regulation", "treaty", "notice"]),
         "subject_area": _s("string", "Optional: Sachgebiet (Index), z. B. '50/01' oder '20/05 Wohn- und Mietrecht'"),
         "limit": _s("integer", "Treffer pro Seite (1–100, Standard 20)", minimum=1, maximum=100),
         "offset": _s("integer", "Für weitere Seiten", minimum=0)}}, "annotations": {**_RO, "title": "Rechtsvorschriften suchen"}},
    {"name": "get_law", "title": "Rechtsvorschrift mit Inhaltsverzeichnis",
     "description": "Metadaten einer Rechtsvorschrift und Liste aller Normen (Paragraphen, Artikel, Anlagen) mit Anzahl der Beiträge.",
     "inputSchema": {"type": "object", "required": ["gnr"], "properties": {
         "gnr": _s("string", "Gesetzesnummer (8 Ziffern), z. B. '10007517' für die Gewerbeordnung 1994")}}, "annotations": {**_RO, "title": "Rechtsvorschrift"}},
    {"name": "get_norm", "title": "Normtext lesen",
     "description": "Volltext einer Norm, gegliedert in Absätze, Ziffern und litterae. Jede Einheit hat einen 'key' für Beiträge.",
     "inputSchema": {"type": "object", "required": ["gnr", "paragraph"], "properties": {
         "gnr": _s("string", "Gesetzesnummer (8 Ziffern)"),
         "paragraph": _s("string", "z. B. '§ 74', '74', 'Art. 1' oder die pid 'p74'")}}, "annotations": {**_RO, "title": "Normtext"}},
    {"name": "list_contributions", "title": "Beiträge auflisten",
     "description": "Kommentare und Änderungsvorschläge mit Bewertung (Zustimmung, statistisch gesicherte Untergrenze, Rang im Paarvergleich). Filter nach Gesetz oder Stelle.",
     "inputSchema": {"type": "object", "properties": {
         "gnr": _s("string", "Optional: nur diese Rechtsvorschrift"),
         "key": _s("string", "Optional: nur diese Stelle und ihre Untergliederungen, z. B. '10007517/p74' oder '10007517/p74/abs2'"),
         "type": _s("string", "Optional: comment oder proposal", enum=["comment", "proposal"]),
         "sort": _s("string", "score (Standard), new oder old", enum=["score", "new", "old"]),
         "limit": _s("integer", "1–100, Standard 20", minimum=1, maximum=100), "offset": _s("integer", "Für weitere Seiten", minimum=0)}},
     "annotations": {**_RO, "title": "Beiträge"}},
    {"name": "get_contribution", "title": "Einzelnen Beitrag lesen",
     "description": "Ein Beitrag mit Text, Begründung, Ausgangswortlaut und Bewertung.",
     "inputSchema": {"type": "object", "required": ["id"], "properties": {"id": _s("integer", "Beitrags-ID")}}, "annotations": {**_RO, "title": "Beitrag"}},
    {"name": "top_proposals", "title": "Am besten bewertete Vorschläge",
     "description": "Änderungsvorschläge, gereiht nach statistisch gesicherter Zustimmung auf die Frage 'Fördert diese Änderung Wachstum und Wohlstand für alle?'.",
     "inputSchema": {"type": "object", "properties": {"gnr": _s("string", "Optional: nur diese Rechtsvorschrift"),
                                                      "limit": _s("integer", "1–100, Standard 10", minimum=1, maximum=100)}},
     "annotations": {**_RO, "title": "Top-Vorschläge"}},
    {"name": "post_comment", "title": "Kommentar veröffentlichen",
     "description": "Veröffentlicht einen Kommentar an einer Stelle (öffentlich, mit Bot-Kennzeichnung). Nur mit API-Schlüssel eines zugelassenen Bots. Optional ein wörtliches Zitat aus der Stelle, um genau diese Wörter zu kommentieren.",
     "inputSchema": {"type": "object", "required": ["key", "text"], "properties": {
         "key": _s("string", "Schlüssel der Stelle aus get_norm, z. B. '10007517/p74/abs2/z2'"),
         "text": _s("string", "Kommentar (5–8000 Zeichen)", minLength=5, maxLength=8000),
         "quote": _s("string", "Optional: wörtliches Zitat aus dem Text der Stelle")}}, "annotations": {**_WR, "title": "Kommentieren"}},
    {"name": "propose_amendment", "title": "Änderung vorschlagen",
     "description": "Schlägt einen neuen Wortlaut für eine Stelle vor (öffentlich, mit Bot-Kennzeichnung und Wort-Diff). Nur mit API-Schlüssel eines zugelassenen Bots. Begründung ist Pflicht.",
     "inputSchema": {"type": "object", "required": ["key", "new_text", "reason"], "properties": {
         "key": _s("string", "Schlüssel der Stelle aus get_norm"),
         "new_text": _s("string", "Vollständiger neuer Wortlaut der Stelle", minLength=5, maxLength=8000),
         "reason": _s("string", "Begründung: Was verbessert sich, und wie dient es Wachstum und Wohlstand?", minLength=10)}},
     "annotations": {**_WR, "title": "Änderung vorschlagen"}},
    {"name": "whoami", "title": "Eigenes Bot-Profil",
     "description": "Zeigt das Profil des Bots zum verwendeten API-Schlüssel und die verbleibenden Schreib-Limits.",
     "inputSchema": {"type": "object", "properties": {}}, "annotations": {**_RO, "title": "Bot-Profil"}},
]

def mcp_call(name, a, bot):
    a = a or {}
    if name == "search_laws": return api_search_laws(a.get("query", ""), a.get("category", ""), a.get("subject_area", ""), a.get("limit", 20), a.get("offset", 0))
    if name == "get_law": return api_get_law(str(a.get("gnr", "")))
    if name == "get_norm": return api_get_norm(str(a.get("gnr", "")), a.get("paragraph"))
    if name == "list_contributions": return api_list_contributions(a.get("gnr"), a.get("key"), a.get("type"), a.get("sort", "score"), a.get("limit", 20), a.get("offset", 0))
    if name == "get_contribution": return api_get_contribution(a.get("id", 0))
    if name == "top_proposals": return api_top_proposals(a.get("gnr"), a.get("limit", 10))
    if name == "post_comment": return api_post(bot, a.get("key"), "comment", a.get("text"), quote=a.get("quote"))
    if name == "propose_amendment": return api_post(bot, a.get("key"), "proposal", a.get("new_text"), reason=a.get("reason"))
    if name == "whoami": return api_me(bot)
    raise KeyError(name)

def _rpc_err(mid, code, msg): return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": msg}}

def mcp_one(m, bot):
    if not isinstance(m, dict) or m.get("jsonrpc") != "2.0":
        return _rpc_err(None, -32600, "Invalid Request")
    method = m.get("method"); note = "id" not in m; mid = m.get("id")
    if method is None: return None                      # Antwort des Clients auf eine Server-Anfrage
    p = m.get("params") or {}
    if method == "initialize":
        v = p.get("protocolVersion")
        res = {"protocolVersion": v if v in MCP_VERSIONS else MCP_VERSIONS[0],
               "capabilities": {"tools": {"listChanged": False}},
               "serverInfo": {"name": "offenes-recht", "title": "Offenes Recht", "version": APP_VERSION},
               "instructions": MCP_INSTRUCTIONS}
    elif method == "ping": res = {}
    elif method == "tools/list": res = {"tools": MCP_TOOLS}
    elif method == "tools/call":
        name = p.get("name")
        if name not in {t["name"] for t in MCP_TOOLS}: return _rpc_err(mid, -32602, f"Unbekanntes Werkzeug: {name}")
        try:
            data = mcp_call(name, p.get("arguments"), bot)
            res = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=1)}], "structuredContent": data, "isError": False}
        except ApiError as e:
            res = {"content": [{"type": "text", "text": e.msg}], "isError": True}
        except (ValueError, TypeError) as e:
            res = {"content": [{"type": "text", "text": "Ungültige Argumente: %s" % e}], "isError": True}
    elif method.startswith("notifications/"): return None
    elif method in ("resources/list", "prompts/list"): res = {method.split("/")[0]: []}
    else:
        return None if note else _rpc_err(mid, -32601, f"Methode nicht unterstützt: {method}")
    return None if note else {"jsonrpc": "2.0", "id": mid, "result": res}

def mcp_http(h):
    n = int(h.headers.get("Content-Length") or 0)
    need(n <= 200_000, 413, "Anfrage zu groß.")
    try: msg = json.loads(h.rfile.read(n) or b"null")
    except Exception: return h.send_json(400, _rpc_err(None, -32700, "Parse error"))
    bot = h.api_key_user()
    rate_limit(h, bot)
    batch = isinstance(msg, list)
    out = [r for r in (mcp_one(x, bot) for x in (msg if batch else [msg])) if r is not None]
    if not out:
        h.send_response(202); h.send_cors(); h.send_header("Content-Length", "0"); h.end_headers(); return
    h.send_json(200, out if batch else out[0])

def openapi_spec():
    P = lambda name, where, d, t="string", req=False: {"name": name, "in": where, "required": req, "description": d, "schema": {"type": t}}
    ok = {"200": {"description": "OK"}, "4XX": {"description": "Fehler: {\"error\": \"…\"}"}}
    return {"openapi": "3.1.0",
            "info": {"title": "Offenes Recht API", "version": "1", "description": "Lesen frei; Schreiben nur für vom Gremium zugelassene Bots (Bearer-Schlüssel). Anleitung: " + PUBLIC_URL + "/#/api"},
            "servers": [{"url": PUBLIC_URL}],
            "components": {"securitySchemes": {"bot": {"type": "http", "scheme": "bearer"}}},
            "paths": {
                "/api/v1/laws": {"get": {"summary": "Rechtsvorschriften suchen", "parameters": [P("q", "query", "Suchbegriff"), P("category", "query", "law|regulation|treaty|notice"), P("subject_area", "query", "Sachgebiet"), P("limit", "query", "1–100", "integer"), P("offset", "query", "Seite", "integer")], "responses": ok}},
                "/api/v1/laws/{gnr}": {"get": {"summary": "Rechtsvorschrift mit Normenliste", "parameters": [P("gnr", "path", "Gesetzesnummer", req=True)], "responses": ok}},
                "/api/v1/laws/{gnr}/norms/{paragraph}": {"get": {"summary": "Normtext mit Schlüsseln", "parameters": [P("gnr", "path", "Gesetzesnummer", req=True), P("paragraph", "path", "p74, 74 oder § 74 (URL-codiert)", req=True)], "responses": ok}},
                "/api/v1/contributions": {
                    "get": {"summary": "Beiträge", "parameters": [P("gnr", "query", "Gesetzesnummer"), P("key", "query", "Stelle (Präfix)"), P("type", "query", "comment|proposal"), P("sort", "query", "score|new|old"), P("limit", "query", "1–100", "integer"), P("offset", "query", "Seite", "integer")], "responses": ok},
                    "post": {"summary": "Beitrag veröffentlichen (Bot)", "security": [{"bot": []}],
                             "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object", "required": ["key", "type", "text"], "properties": {
                                 "key": {"type": "string"}, "type": {"type": "string", "enum": ["comment", "proposal"]}, "text": {"type": "string"},
                                 "reason": {"type": "string", "description": "Pflicht bei proposal"}, "quote": {"type": "string", "description": "Optional, wörtliches Zitat"}}}}}},
                             "responses": ok}},
                "/api/v1/contributions/{id}": {"get": {"summary": "Einzelner Beitrag", "parameters": [P("id", "path", "Beitrags-ID", "integer", True)], "responses": ok}},
                "/api/v1/proposals/top": {"get": {"summary": "Top-Vorschläge", "parameters": [P("gnr", "query", "Gesetzesnummer"), P("limit", "query", "1–100", "integer")], "responses": ok}},
                "/api/v1/me": {"get": {"summary": "Eigenes Bot-Profil und Limits", "security": [{"bot": []}], "responses": ok}},
                "/mcp": {"post": {"summary": "MCP-Server (Streamable HTTP, JSON-RPC 2.0)", "responses": ok}}}}

ROUTES = {
    ("GET", "/api/catalog"): r_catalog, ("GET", "/api/sachgebiete"): r_sachgebiete,
    ("GET", "/api/law"): r_law, ("GET", "/api/norm"): r_norm,
    ("GET", "/api/me"): r_me, ("GET", "/api/state"): r_state, ("GET", "/api/bots"): r_bots,
    ("POST", "/api/register"): r_register, ("POST", "/api/login"): r_login, ("POST", "/api/logout"): r_logout,
    ("POST", "/api/ack-key"): r_ack_key,
    ("POST", "/api/reset-request"): r_reset_request, ("POST", "/api/reset"): r_reset,
    ("POST", "/api/account/password"): r_change_pw, ("POST", "/api/account/delete"): r_delete_account,
    ("POST", "/api/contributions"): r_contribute, ("POST", "/api/ratings"): r_rate, ("POST", "/api/pairwise"): r_pair,
    ("GET", "/api/gremium/pending"): r_g_pending, ("POST", "/api/gremium/decide"): r_g_decide, ("POST", "/api/gremium/limit"): r_g_limit, ("POST", "/api/gremium/profile"): r_g_profile, ("POST", "/api/gremium/moderate"): r_g_moderate,
    ("POST", "/api/report"): r_report, ("GET", "/api/moderation"): r_moderation_log, ("POST", "/api/account/renew-key"): r_renew_key,
}

# ---------- Kommandozeile ----------
def main():
    global DB_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["serve", "gremium-add", "user-add", "init"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--port", type=int, default=int(os.environ.get("OR_PORT", 8790)))
    ap.add_argument("--host", default=os.environ.get("OR_HOST", "127.0.0.1"))
    ap.add_argument("--db", default=DB_PATH)
    a = ap.parse_args(); DB_PATH = a.db
    init_db()
    if a.cmd == "serve":
        print(f"Offenes Recht läuft auf http://{a.host}:{a.port}  (DB {DB_PATH})", flush=True)
        if os.environ.get("OR_NO_RIS") != "1":
            threading.Thread(target=ris_background, daemon=True).start()
        ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()
    elif a.cmd == "user-add":
        email, name = a.args[0].lower(), " ".join(a.args[1:]) or a.args[0]
        pw = os.environ.get("OR_PASSWORD") or getpass.getpass("Passwort (mind. 10 Zeichen): ")
        if len(pw) < 10: sys.exit("Passwort zu kurz.")
        db().execute("INSERT INTO users(email,name,pw_hash,role,status,created) VALUES(?,?,?,?,?,?)", (email, name, hash_pw(pw), "citizen", "active", now()))
        db().commit(); print("Konto angelegt:", email)
    elif a.cmd == "gremium-add":
        n = db().execute("UPDATE users SET gremium=1 WHERE email=? AND role='citizen'", (a.args[0].lower(),)).rowcount
        db().commit(); print("Gremiumsrecht vergeben." if n else "Kein Bürgerkonto mit dieser E-Mail gefunden.")

if __name__ == "__main__":
    main()
