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
CREATE TABLE IF NOT EXISTS arg_ratings(user_id INTEGER, cid INTEGER, v TEXT, created INTEGER, PRIMARY KEY(user_id, cid));
CREATE TABLE IF NOT EXISTS contribution_links(a INTEGER NOT NULL, b INTEGER NOT NULL, kind TEXT NOT NULL, note TEXT, user_id INTEGER, created INTEGER,
  PRIMARY KEY(a, b, kind));
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
CREATE TABLE IF NOT EXISTS packages(id INTEGER PRIMARY KEY, title TEXT NOT NULL, summary TEXT NOT NULL, user_id INTEGER NOT NULL,
  created INTEGER NOT NULL, updated INTEGER NOT NULL, removed_at INTEGER, removed_by INTEGER, removed_reason TEXT, removed_note TEXT);
CREATE TABLE IF NOT EXISTS package_items(package_id INTEGER NOT NULL, cid INTEGER NOT NULL, pos INTEGER NOT NULL, PRIMARY KEY(package_id, cid));
CREATE TABLE IF NOT EXISTS package_ratings(user_id INTEGER, package_id INTEGER, v TEXT, created INTEGER, PRIMARY KEY(user_id, package_id));
CREATE TABLE IF NOT EXISTS drafts(gnr TEXT PRIMARY KEY, gp TEXT, inr INTEGER, title TEXT, ressort TEXT, ministry TEXT, frist TEXT,
  einlangen TEXT, url TEXT, active INTEGER DEFAULT 0, statements INTEGER DEFAULT 0, synced INTEGER);
CREATE TABLE IF NOT EXISTS follows(user_id INTEGER NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL, created INTEGER NOT NULL,
  seen_count INTEGER, seen_status TEXT, PRIMARY KEY(user_id, kind, ref));
CREATE TABLE IF NOT EXISTS stats_daily(day TEXT NOT NULL, metric TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(day, metric));
CREATE TABLE IF NOT EXISTS anomaly_reviews(fid TEXT PRIMARY KEY, decision TEXT, by_user INTEGER, at INTEGER, note TEXT);
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
    if "op" not in cols:
        c.execute("ALTER TABLE contributions ADD COLUMN op TEXT")          # 'repeal' = ersatzlos streichen
    lcols = [r[1] for r in c.execute("PRAGMA table_info(laws)")]
    if "aenderung" not in lcols:
        c.execute("ALTER TABLE laws ADD COLUMN aenderung TEXT")           # Novellenliste aus dem RIS (mit CELEX-Nummern)
        c.execute("DELETE FROM meta WHERE k='catalog_synced'")            # Katalog beim nächsten Start neu laden
    if "n_changes" not in lcols:
        for col in ("n_changes INTEGER", "n_celex INTEGER", "stf_year INTEGER", "last_year INTEGER"):
            c.execute("ALTER TABLE laws ADD COLUMN " + col)
    if "gold" not in cols:
        c.execute("ALTER TABLE contributions ADD COLUMN gold INTEGER DEFAULT 0")   # Übererfüllung von EU-Recht
        c.execute("ALTER TABLE contributions ADD COLUMN celex TEXT")
    if "stance" not in cols:                                             # Debatte: Richtung, Kategorie, Streitfrage, Quelle
        for col in ("stance TEXT", "category TEXT", "issue TEXT", "question TEXT", "summary TEXT", "src_org TEXT", "src_ref TEXT", "src_url TEXT"):
            c.execute("ALTER TABLE contributions ADD COLUMN " + col)
    ncols = [r[1] for r in c.execute("PRAGMA table_info(norms)")]
    if "kundmachung" not in ncols:
        c.execute("ALTER TABLE norms ADD COLUMN kundmachung TEXT")
    if c.execute("SELECT 1 FROM laws WHERE aenderung IS NOT NULL AND n_changes IS NULL LIMIT 1").fetchone():
        c.commit(); recompute_law_metrics(c)
    ucols = [r[1] for r in c.execute("PRAGMA table_info(users)")]
    if "last_seen" not in ucols:
        c.execute("ALTER TABLE users ADD COLUMN last_seen INTEGER")       # letzter Besuch von „Mein Konto“ (für „Neu seit …“)
    if "rating_hold" not in ucols:
        c.execute("ALTER TABLE users ADD COLUMN rating_hold INTEGER DEFAULT 0")    # Stimmen vorläufig nicht gezählt (Gremium)
    if "notify" not in ucols:
        c.execute("ALTER TABLE users ADD COLUMN notify TEXT DEFAULT 'daily'")    # Benachrichtigungen zu gefolgten Inhalten
        c.execute("ALTER TABLE users ADD COLUMN last_digest INTEGER")
    if not c.execute("SELECT 1 FROM meta WHERE k='secret'").fetchone():
        c.execute("INSERT INTO meta VALUES('secret',?)", (secrets.token_hex(32),))
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
        for r in c.execute(f"""SELECT cid, v, COUNT(*) n FROM ratings WHERE cid IN ({q})
                               AND user_id NOT IN (SELECT id FROM users WHERE rating_hold=1) GROUP BY cid, v""", ids):
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
        for p in c.execute(f"SELECT a,b,winner FROM pairwise WHERE a IN ({q}) AND b IN ({q}) AND user_id NOT IN (SELECT id FROM users WHERE rating_hold=1)", pid+pid):
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
    if len(parts) > 1 and parts[1] == "gesetz": return out + "(gesamte Rechtsvorschrift)"
    if parts[0].startswith("9") and len(parts) > 2 and parts[2] != "ganz":
        r = c.execute("SELECT label, body FROM norms WHERE gnr=? AND pid=?", (parts[0], parts[1])).fetchone()
        u = next((u for u in json.loads(r["body"])["units"] if u["a"] == parts[2]), None) if r and r["body"] else None
        return out + (r["label"] if r else parts[1]) + ((" · " + u["n"]) if u and u["n"] else "")
    out += n["label"] if n else (parts[1] if len(parts) > 1 else "")
    if parts[2:] == ["ganz"]: return out + " (gesamt)"
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
                    "where": key_label(db(), r["key"]), "op": r["op"], "amend": amend_for(db(), r),
                    "gold": {"celex": r["celex"], "url": eurlex(r["celex"]) if r["celex"] else None} if r["gold"] else None,
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
RIS_UA = "OffenesRecht/1 (+" + os.environ.get("OR_PUBLIC_URL", "https://offenesrecht.at") + ")"
RIS_DELAY = float(os.environ.get("OR_RIS_DELAY", "0.4"))   # Pause zwischen Anfragen im Hintergrund
PILOT_GNR = "10007517"   # Gewerbeordnung 1994
_ris_lock = threading.Lock()
_zahlen_cache = {}; _ref_cache = {}
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
    if q["Applikation"] != "BrKons": q.pop("FassungVom", None)
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
            "eli": b.get("Eli"), "label": k.get("ArtikelParagraphAnlage") or "", "xml": xml, "aenderung": k.get("Aenderung")}

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

# ---------- Kennzahlen aus der RIS-Änderungsliste ----------
_BGBL_RE = re.compile(r"BGBl\.\s*(?:I{1,3}\s*)?Nr\.\s*\d+/(\d{4})")
def celex_norm(x):
    """Alte CELEX-Schreibweise '390L0314' -> '31990L0314'; neue bleibt."""
    x = x.strip().upper()
    m = re.fullmatch(r"([0-9])(\d{2})([A-Z]{1,2})(\d{4})(\(\d+\))?", x)
    if m: return m.group(1) + ("19" if int(m.group(2)) > 50 else "20") + m.group(2) + m.group(3) + m.group(4) + (m.group(5) or "")
    return x if re.fullmatch(r"[0-9]\d{4}[A-Z]{1,2}\d{4}(\(\d+\))?", x) else None

def law_metrics(aenderung, titel=None, kundmachung=None):
    lines = [l.strip() for l in (aenderung or "").splitlines() if l.strip()]
    changes = [l for l in lines if l.startswith("BGBl")]
    years = []
    for l in changes:
        m = _BGBL_RE.search(l)
        if m: years.append(int(m.group(1)))
    celex = []
    for l in lines:
        for blk in re.findall(r"CELEX-Nr\.?:\s*([^\]]+)", l):
            for tok in re.split(r"[,;\s]+", blk):
                cx = celex_norm(tok) if tok else None
                if cx and cx not in celex: celex.append(cx)
    m = re.search(r"StF:\s*[^·/]*?\d+/(\d{4})", titel or "") or re.search(r"\d+/(\d{4})", kundmachung or "")
    stf = int(m.group(1)) if m else None
    return {"n_changes": len(changes), "years": years, "celex": celex, "stf_year": stf, "last_year": max(years) if years else stf}

def recompute_law_metrics(c):
    rows = c.execute("SELECT gnr, aenderung, titel, kundmachung FROM laws").fetchall()
    for r in rows:
        m = law_metrics(r["aenderung"], r["titel"], r["kundmachung"])
        c.execute("UPDATE laws SET n_changes=?, n_celex=?, stf_year=?, last_year=? WHERE gnr=?",
                  (m["n_changes"], len(m["celex"]), m["stf_year"], m["last_year"], r["gnr"]))
    c.commit(); _zahlen_cache.clear()

def eurlex(cx): return f"https://eur-lex.europa.eu/legal-content/DE/TXT/?uri=CELEX:{cx}"

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
            c.execute("""INSERT INTO laws(gnr,kurztitel,titel,abk,typ,tgroup,idx,kundmachung,inkraft,eli,synced,aenderung) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(gnr) DO UPDATE SET kurztitel=excluded.kurztitel,titel=excluded.titel,abk=excluded.abk,typ=excluded.typ,
                         tgroup=excluded.tgroup,idx=excluded.idx,kundmachung=excluded.kundmachung,inkraft=excluded.inkraft,eli=excluded.eli,synced=excluded.synced,
                         aenderung=excluded.aenderung""",
                      (x["gnr"], x["kurztitel"], x["titel"], x["abk"], x["typ"], type_group(x["typ"]), x["idx"], x["kundmachung"], x["inkraft"], x["eli"], now(), x["aenderung"]))
            n += 1
        c.commit()
        if page * 100 >= hits or not docs: break
        page += 1; time.sleep(RIS_DELAY)
    all_hits, _ = ris_search({"DokumenteProSeite": "Ten"})
    c.row_factory = sqlite3.Row; recompute_law_metrics(c)
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
    stale = lambda: c.execute("SELECT 1 FROM norms WHERE gnr=? AND kundmachung IS NULL LIMIT 1", (gnr,)).fetchone()
    if c.execute("SELECT norms_listed FROM laws WHERE gnr=?", (gnr,)).fetchone()[0] and not stale(): return
    with _ris_lock:
        if db().execute("SELECT norms_listed FROM laws WHERE gnr=?", (gnr,)).fetchone()[0] and not stale(): return
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
            c.execute("""INSERT INTO norms(nor,gnr,pid,label,sort,inkraft,xml_url,kundmachung) VALUES(?,?,?,?,?,?,?,?)
                         ON CONFLICT(nor) DO UPDATE SET pid=excluded.pid,label=excluded.label,sort=excluded.sort,inkraft=excluded.inkraft,xml_url=excluded.xml_url,
                         kundmachung=excluded.kundmachung""",
                      (x["nor"], gnr, pid, x["label"], sort_key(x["label"], i), x["inkraft"], x["xml"], x["kundmachung"] or ""))
        c.execute("UPDATE norms SET kundmachung='' WHERE gnr=? AND kundmachung IS NULL", (gnr,))
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
    """Aktueller Text einer Einheit 'gnr/pid/abs1/z2' – für den Ausgangstext eines Vorschlags.
    Sonderfälle: 'gnr/pid/ganz' = ganze Norm, 'gnr/gesetz/ganz' = ganze Rechtsvorschrift."""
    parts = key.split("/")
    if len(parts) < 3: return None
    if parts[1] == "gesetz":
        if parts[2:] != ["ganz"]: return None
        law = db().execute("SELECT kurztitel, n_norms FROM laws WHERE gnr=?", (parts[0],)).fetchone()
        return f"{law['kurztitel']} (gesamte Rechtsvorschrift)" if law else None
    r = db().execute("SELECT body FROM norms WHERE gnr=? AND pid=?", (parts[0], parts[1])).fetchone()
    if not r or not r["body"]: return None
    units = json.loads(r["body"])["units"]
    a = "/".join(parts[2:])
    if a == "ganz":
        return " ".join((u["n"] + " " + u["t"]).strip() for u in units) or None
    return next((u["t"] for u in units if u["a"] == a), None)

def whole_key(key):
    """'ganz' = ganze Norm, 'gesetz' = ganze Rechtsvorschrift, sonst None."""
    p = key.split("/")
    if len(p) == 3 and p[2] == "ganz": return "gesetz" if p[1] == "gesetz" else "ganz"
    return None

# ---------- Novellierungstext (legistischer Entwurf aus Vorschlägen) ----------
# Erzeugt Novellierungsanordnungen nach den Legistischen Richtlinien des Bundes (vereinfacht):
#   „In § 74 Abs. 2 Z 1 wird das Wort „X“ durch die Wortfolge „Y“ ersetzt.“ · „… wird nach … eingefügt.“
#   „In § 74 Abs. 2 entfällt die Wortfolge „X“.“ · „§ 74 Abs. 2 Z 1 lautet: …“ · „§ 74 Abs. 2 Z 1 entfällt.“
# Der Entwurf ist ein Ausgangspunkt für die legistische Prüfung, keine fertige Regierungsvorlage.
import difflib

def _q(t): return "„" + t + "“"
def _wf(words): return ("das Wort " if len(words) == 1 else "die Wortfolge ") + _q(" ".join(words))
def _wf_dat(words): return ("dem Wort " if len(words) == 1 else "der Wortfolge ") + _q(" ".join(words))

def _count(words, seq):
    n = len(seq)
    return sum(1 for i in range(len(words) - n + 1) if words[i:i + n] == seq) if n else 0

def norm_ref(c, key):
    """'10007517/p74/abs2/z1' -> '§ 74 Abs. 2 Z 1' (ohne Gesetz)."""
    parts = key.split("/")
    n = c.execute("SELECT label FROM norms WHERE gnr=? AND pid=?", (parts[0], parts[1])).fetchone()
    out = n["label"] if n else parts[1]
    for p in parts[2:]:
        m = re.match(r"^(abs|z|lit)(\w+?)(_\d+)?$", p)
        if m: out += {"abs": " Abs. ", "z": " Z ", "lit": " lit. "}[m.group(1)] + m.group(2)
        elif p.startswith("s"): out += " Schlussteil"
    return out

def _unit_no(c, key):
    parts = key.split("/")
    r = c.execute("SELECT body FROM norms WHERE gnr=? AND pid=?", (parts[0], parts[1])).fetchone()
    if not r or not r["body"]: return ""
    a = "/".join(parts[2:])
    u = next((u for u in json.loads(r["body"])["units"] if u["a"] == a), None)
    return (u or {}).get("n") or ""

def _has_heading(c, key):
    parts = key.split("/")
    r = c.execute("SELECT title FROM norms WHERE gnr=? AND pid=?", (parts[0], parts[1])).fetchone()
    return bool(r and r["title"])

def amendment_instruction(c, row):
    """Novellierungsanordnung für einen Vorschlag (sqlite-Row oder dict mit key, op, base_text, text)."""
    key, op, base, new = row["key"], row["op"], row["base_text"] or "", row["text"] or ""
    whole = whole_key(key)
    if whole == "gesetz": return None                       # Aufhebung eines ganzen Gesetzes: eigener Artikel, siehe draft_bill
    ref = norm_ref(c, key)
    if whole == "ganz":
        return f"{ref} samt Überschrift entfällt." if _has_heading(c, key) else f"{ref} entfällt."
    if op == "repeal": return f"{ref} entfällt."
    A, B = base.split(), new.split()
    if not A: return f"{ref} lautet:\n{_q(new)}"
    ops = [o for o in difflib.SequenceMatcher(None, A, B, autojunk=False).get_opcodes() if o[0] != "equal"]
    changed = sum(max(i2 - i1, j2 - j1) for _, i1, i2, j1, j2 in ops)
    if not ops: return None
    if len(ops) > 3 or changed > 0.5 * len(A):
        no = _unit_no(c, key)
        return f"{ref} lautet:\n{_q((no + ' ' if no else '') + new)}"
    out = []
    for tag, i1, i2, j1, j2 in reversed(ops):          # von hinten, damit Positionen stabil bleiben (Ausgabe danach umgedreht)
        old, ins = A[i1:i2], B[j1:j2]
        # Kontext erweitern, bis die zu ändernde Wortfolge im Ausgangstext eindeutig ist
        l, r = i1, i2; jl, jr = j1, j2
        while tag != "insert" and _count(A, A[l:r]) > 1 and (l > 0 or r < len(A)):
            if l > 0: l -= 1; jl -= 1
            elif r < len(A): r += 1; jr += 1
        if tag == "insert" and i1 == len(A) and A[-1].endswith((".", ":")) and ins and ins[0][:1].isupper():
            n_s = len(re.findall(r"[.!?](?=\s|$)", " ".join(ins)))
            out.append(f"Dem {ref} {'werden folgende Sätze' if n_s > 1 else 'wird folgender Satz'} angefügt:\n{_q(' '.join(ins))}")
        elif tag == "insert":
            if i1 > 0:
                k = i1 - 1
                while _count(A, A[k:i1]) > 1 and k > 0: k -= 1
                out.append(f"In {ref} wird nach {_wf_dat(A[k:i1])} {_wf(ins)} eingefügt.")
            else:
                k = 1
                while _count(A, A[0:k]) > 1 and k < len(A): k += 1
                out.append(f"In {ref} wird vor {_wf_dat(A[0:k])} {_wf(ins)} eingefügt.")
        elif tag == "delete" and (l, r) == (i1, i2):
            out.append(f"In {ref} entfällt {_wf(old)}.")
        else:
            o, n = A[l:r], B[jl:jr]
            if not n: out.append(f"In {ref} entfällt {_wf(o)}.")
            else: out.append(f"In {ref} wird {_wf(o)} durch {_wf(n)} ersetzt.")
    return " ".join(reversed(out))

_GENUS = (("ordnung", "die"), ("verordnung", "die"), ("novelle", "die"), ("vereinbarung", "die"), ("kundmachung", "die"),
          ("satzung", "die"), ("vertrag", "der"), ("kodex", "der"), ("tarif", "der"), ("abkommen", "das"), ("übereinkommen", "das"),
          ("protokoll", "das"), ("statut", "das"), ("gesetz", "das"), ("recht", "das"))
def law_article(name):
    """Bestimmter Artikel zum Kurztitel, z. B. 'die Gewerbeordnung 1994', 'das ASVG'."""
    w = [x for x in re.split(r"[\s\-–]+", name or "") if x and not re.fullmatch(r"[\d/.,()]+", x)]
    last = (w[-1] if w else "").lower()
    for suf, art in _GENUS:
        if last.endswith(suf): return art
    return "das" if name.isupper() or re.fullmatch(r"[A-ZÄÖÜ\-]+G", (name or "").split()[0]) else "die"
def _acc(art): return {"der": "den"}.get(art, art)
def _gen(art): return {"der": "des", "das": "des", "die": "der"}[art]

def last_change(aenderung):
    """Letzte Kundmachung aus der RIS-Änderungsliste, z. B. 'BGBl. I Nr. 56/2025'."""
    found = re.findall(r"BGBl\.\s*(?:I{1,3}\s*)?Nr\.\s*\d+/\d{4}", aenderung or "")
    return re.sub(r"\s+", " ", found[-1]) if found else None

def law_citation(c, gnr):
    law = c.execute("SELECT * FROM laws WHERE gnr=?", (gnr,)).fetchone()
    name = law["kurztitel"] if law else gnr
    stamm = law["kundmachung"] if law else None
    m = re.search(r"StF:\s*(BGBl\.\s*(?:I{1,3}\s*)?Nr\.\s*\d+/\d{4})", (law["titel"] if law else "") or "")
    if m: stamm = re.sub(r"\s+", " ", m.group(1))
    elif stamm: stamm = re.sub(r"\s*\(.*$", "", stamm).strip()
    last = last_change(law["aenderung"] if law else None)
    cit = f"{name}, {stamm}" if stamm else name
    if last and last != stamm: cit += f", zuletzt geändert durch {last}"
    return law, name, cit

def _sort_key(c, key):
    parts = key.split("/")
    if parts[1] == "gesetz": return (-1, -1)
    n = c.execute("SELECT sort, body FROM norms WHERE gnr=? AND pid=?", (parts[0], parts[1])).fetchone()
    if not n: return (10**12, 0)
    idx = 0
    if n["body"] and len(parts) > 2 and parts[2] != "ganz":
        a = "/".join(parts[2:]); units = json.loads(n["body"])["units"]
        idx = next((i for i, u in enumerate(units) if u["a"] == a), 0) + 1
    return (n["sort"], idx)

def draft_bill(c, rows, title=None):
    """Entwurf (Text) aus Vorschlägen: ein Bundesgesetz (Sammelnovelle bei mehreren Gesetzen), Verordnungen getrennt."""
    by = {}
    for r in rows:
        if r["type"] != "prop": continue
        by.setdefault(r["key"].split("/")[0], []).append(r)
    laws, other = [], []
    for gnr, lst in by.items():
        law = c.execute("SELECT tgroup FROM laws WHERE gnr=?", (gnr,)).fetchone()
        (laws if (law and law["tgroup"] == "g") or not law else other).append((gnr, sorted(lst, key=lambda r: _sort_key(c, r["key"]))))
    laws.sort(key=lambda x: (c.execute("SELECT kurztitel FROM laws WHERE gnr=?", (x[0],)).fetchone() or [""])[0])

    def body_for(gnr, lst, materials):
        law, name, cit = law_citation(c, gnr); art = law_article(name)
        rep = next((r for r in lst if whole_key(r["key"]) == "gesetz"), None)
        if rep:
            materials.append((f"Aufhebung {_gen(art)} {name}", rep["why"]))
            return [f"{art.capitalize()} {cit}, wird aufgehoben."], True
        lines = [f"{art.capitalize()} {cit}, wird wie folgt geändert:", ""]
        z = 0
        for r in lst:
            t = amendment_instruction(c, r)
            if not t: continue
            z += 1; lines += [f"{z}. {t}", ""]
            materials.append((f"Zu Z {z} ({norm_ref(c, r['key'])})", r["why"]))
        return lines[:-1], False

    out = ["ENTWURF – automatisch aus Vorschlägen auf Offenes Recht erzeugt. Vor einer Einbringung legistisch prüfen",
           "(Verweise, Inkrafttreten, Übergangsbestimmungen, Notifikation).", ""]
    if title: out += [title, ""]
    mats_all = []
    if laws:
        names = []
        for gnr, lst in laws:
            _, name, _ = law_citation(c, gnr); art = law_article(name)
            aufh = any(whole_key(r["key"]) == "gesetz" for r in lst)
            names.append((f"{_acc(art)} {name}", aufh))
        ch = [n for n, a in names if not a]; au = [n for n, a in names if a]
        def join(xs): return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " und " + xs[-1]
        verb = " und ".join(p for p in (
            (join(ch) + (" geändert wird" if len(ch) == 1 else " geändert werden")) if ch else "",
            (join(au) + (" aufgehoben wird" if len(au) == 1 else " aufgehoben werden")) if au else "") if p)
        if len(laws) == 1:
            out += [f"Bundesgesetz, mit dem {verb}", "", "Der Nationalrat hat beschlossen:", ""]
            lines, _ = body_for(laws[0][0], laws[0][1], mats_all); out += lines + [""]
        else:
            out += [f"Bundesgesetz, mit dem {verb}", "", "Der Nationalrat hat beschlossen:", "", "Inhaltsverzeichnis", ""]
            heads = []
            for i, (gnr, lst) in enumerate(laws, 1):
                _, name, _ = law_citation(c, gnr); art = law_article(name)
                aufh = any(whole_key(r["key"]) == "gesetz" for r in lst)
                heads.append(f"{'Aufhebung' if aufh else 'Änderung'} {_gen(art)} {name}")
                out.append(f"Artikel {i}\t{heads[-1]}")
            out.append("")
            for i, (gnr, lst) in enumerate(laws, 1):
                mats = []
                out += [f"Artikel {i}", heads[i - 1], ""]
                lines, _ = body_for(gnr, lst, mats); out += lines + [""]
                mats_all += [(f"Artikel {i}, {k}", w) for k, w in mats]
    for gnr, lst in other:
        _, name, _ = law_citation(c, gnr); art = law_article(name)
        out += ["", "––––", "", f"Verordnung, mit der {_acc(art)} {name} geändert wird",
                "", "[Verordnungsgeber und Rechtsgrundlage ergänzen]", ""]
        mats = []; lines, _ = body_for(gnr, lst, mats); out += lines + [""]
        mats_all += [(f"{name}, {k}", w) for k, w in mats]
    if mats_all:
        out += ["", "Erläuterungen", "", "Besonderer Teil", ""]
        for k, w in mats_all:
            out += [k + ":", (w or "").strip() or "(ohne Begründung)", ""]
    return "\n".join(out).rstrip() + "\n"

_amend_cache = {}
def amend_for(c, r):
    if r["type"] != "prop" or r["removed_at"] or is_draft(r["key"][:8]): return None
    k = (r["id"], r["text"], r["base_text"])
    if k not in _amend_cache:
        try: _amend_cache[k] = amendment_instruction(c, r)
        except Exception as e:
            sys.stderr.write("Novellierungstext %s: %r\n" % (r["id"], e)); _amend_cache[k] = None
    return _amend_cache[k]

# ---------- HTTP ----------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
KEY_RE = re.compile(r"^\d{8}/[a-z]+[0-9a-z_]*(/[a-z]+[0-9a-z_]*){1,4}$")
_attempts = {}

class ApiError(Exception):
    def __init__(self, code, msg): self.code, self.msg = code, msg

def need(cond, code, msg):
    if not cond: raise ApiError(code, msg)

class Handler(BaseHTTPRequestHandler):
    server_version = "OffenesRecht/1.11"

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
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        if not self._head: self.wfile.write(body)

    def send_raw(self, code, ctype, data):
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("Content-Length", str(len(data))); self.end_headers()
        if not self._head: self.wfile.write(data)

    def send_static(self, path):
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.realpath(os.path.join(STATIC, name))
        if not full.startswith(os.path.realpath(STATIC)) or not os.path.isfile(full):
            full = os.path.join(STATIC, "index.html")
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
                 ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}.get(os.path.splitext(full)[1], "application/octet-stream")
        data = open(full, "rb").read()
        if full.endswith(".html"): data = data.replace(b"__PUBLIC_URL__", PUBLIC_URL.encode())
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Length", str(len(data))); self.end_headers()
        if not self._head: self.wfile.write(data)

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

    _head = False
    def do_GET(self): self.dispatch("GET")
    def do_HEAD(self): self._head = True; self.dispatch("GET")   # wie GET, aber ohne Inhalt (Link-Prüfer, Monitoring)
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
                bump("mcp")
                return mcp_http(self)
            if path.startswith("/api/v1/"):
                bump("api")
                for m, rx, fn in API_PATTERNS:
                    mt = rx.match(path)
                    if mt and m == method:
                        return self.send_json(200, fn(self, *mt.groups()))
                raise ApiError(404, "Unbekannter Endpunkt. Übersicht: " + PUBLIC_URL + "/api/v1/openapi.json")
            if path.startswith("/.well-known/"):   # keine OAuth-/OpenID-Metadaten: ehrliches 404 statt der Startseite
                raise ApiError(404, "Nicht vorhanden.")
            if re.fullmatch(r"/(v|p|e)/\d{1,9}", path) and method == "GET":
                bump("share")
                return self.send_raw(200, "text/html; charset=utf-8", share_page(path))
            if path == "/og.png" and method == "GET":
                return self.send_raw(200, "image/png", og_png())
            if path.startswith("/feed/") or path == "/abmelden":
                need(method == "GET", 405, "Methode nicht erlaubt.")
                if path == "/abmelden": return self.send_raw(200, "text/html; charset=utf-8", unsubscribe_page(self.query))
                bump("rss")
                return self.send_raw(200, "application/rss+xml; charset=utf-8", feed(path))
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
    op = "repeal" if (typ == "prop" and d.get("op") == "repeal") else None
    whole = whole_key(key)
    need(not whole or op, 400, "Für eine ganze Norm oder Rechtsvorschrift ist nur ein Streichungsvorschlag möglich.")
    text = "" if op else (d.get("text") or "").strip(); why = (d.get("why") or "").strip()
    if not op: need(5 <= len(text) <= 8000, 400, "Bitte mindestens einen kurzen Satz schreiben.")
    if typ == "prop": need(len(why) >= 10, 400, "Bitte den Vorschlag in mindestens einem Satz begründen.")
    w = {} if op else (d.get("w") or {})
    wq = (w.get("quote") or "").strip()[:400] or None
    gnr, pid = key.split("/")[:2]
    if whole == "gesetz":
        need(db().execute("SELECT 1 FROM laws WHERE gnr=?", (gnr,)).fetchone(), 400, "Diese Rechtsvorschrift gibt es nicht.")
    else:
        norm = db().execute("SELECT nor, body FROM norms WHERE gnr=? AND pid=?", (gnr, pid)).fetchone()
        need(norm is not None, 400, "Diese Stelle gibt es im Normtext nicht.")
        if not norm["body"]: fetch_norm(norm["nor"])
    base = unit_text(key)
    need(base is not None, 400, "Diese Stelle gibt es im Normtext nicht.")
    if op:
        need(not db().execute("SELECT 1 FROM contributions WHERE key=? AND op='repeal' AND user_id=? AND removed_at IS NULL", (key, u["id"])).fetchone(),
             409, "Sie haben die Streichung dieser Stelle bereits vorgeschlagen.")
    gold = 1 if d.get("gold") else 0; celex = None
    if gold and d.get("celex"):
        celex = celex_norm(str(d["celex"])); need(celex, 400, "Ungültige CELEX-Nummer, z. B. 32006L0123.")
    meta = deb_meta(d)
    links = deb_links_in(d, gnr)
    cur = db().execute("""INSERT INTO contributions(key,type,text,why,w_quote,w_from,w_to,user_id,via,created,base_text,op,gold,celex)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (key, typ, text, why or None, wq, w.get("from") if wq else None, w.get("to") if wq else None, u["id"], via, now(), base, op, gold, celex))
    cid = cur.lastrowid
    if meta: db().execute("UPDATE contributions SET " + ",".join(k + "=?" for k in meta) + " WHERE id=?", (*meta.values(), cid))
    for kind, other, note in links: deb_link(cid, other, kind, note, u["id"])
    db().commit()
    return {"id": cid}

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
        if r["cid"] < 0:   # gemeldetes Reformpaket
            pk = c.execute("SELECT * FROM packages WHERE id=?", (-r["cid"],)).fetchone()
            cr = {"user_id": pk["user_id"], "removed_at": pk["removed_at"], "key": "", "type": "package",
                  "text": pk["title"] + " – " + pk["summary"]} if pk else None
        else:
            cr = c.execute("SELECT * FROM contributions WHERE id=?", (r["cid"],)).fetchone()
        if not cr or cr["removed_at"]:
            c.execute("UPDATE reports SET status='done' WHERE cid=? AND status='open'", (r["cid"],)); continue
        au = c.execute("SELECT name, role FROM users WHERE id=?", (cr["user_id"],)).fetchone()
        notes = [x["note"] for x in c.execute("SELECT note FROM reports WHERE cid=? AND status='open' AND note IS NOT NULL AND note!=''", (r["cid"],))]
        reps.append({"cid": r["cid"], "count": r["n"], "reasons": [REMOVE_REASONS.get(x, x) for x in sorted(set(r["reasons"].split(",")))],
                     "notes": notes[:5], "key": cr["key"], "where": ("Reformpaket " + str(-r["cid"])) if r["cid"] < 0 else key_label(c, cr["key"]),
                     "type": cr["type"], "text": cr["text"][:600],
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
    if cid < 0: r = c.execute("SELECT user_id, removed_at FROM packages WHERE id=?", (-cid,)).fetchone()
    else: r = c.execute("SELECT user_id, removed_at FROM contributions WHERE id=?", (cid,)).fetchone()
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
    note = (d.get("note") or "").strip()[:500]
    if cid < 0: return _moderate_package(g, c, -cid, dec, d.get("reason"), note)
    r = c.execute("SELECT * FROM contributions WHERE id=?", (cid,)).fetchone(); need(r, 404, "Beitrag nicht gefunden.")
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

def _moderate_package(g, c, pid, dec, reason, note):
    p = c.execute("SELECT * FROM packages WHERE id=?", (pid,)).fetchone(); need(p, 404, "Paket nicht gefunden.")
    lbl = f"Reformpaket {pid} („{p['title'][:60]}“)"
    if dec == "remove":
        need(reason in REMOVE_REASONS, 400, "Bitte einen Grund wählen.")
        need(len(note) >= 10, 400, "Bitte die Entfernung kurz begründen; die Begründung wird öffentlich vermerkt.")
        need(not p["removed_at"], 409, "Das Paket ist bereits entfernt.")
        c.execute("UPDATE packages SET removed_at=?, removed_by=?, removed_reason=?, removed_note=? WHERE id=?", (now(), g["id"], reason, note, pid))
        c.execute("UPDATE reports SET status='upheld', decided_by=?, decided_at=? WHERE cid=? AND status='open'", (g["id"], now(), -pid))
        log(g["id"], "moderation:remove", p["user_id"], f"{lbl}: {REMOVE_REASONS[reason]} · {note}")
    elif dec == "dismiss":
        n = c.execute("UPDATE reports SET status='dismissed', decided_by=?, decided_at=? WHERE cid=? AND status='open'", (g["id"], now(), -pid)).rowcount
        log(g["id"], "moderation:dismiss", p["user_id"], f"{lbl}: {n} Meldung(en) zurückgewiesen" + (f" · {note}" if note else ""))
    elif dec == "restore":
        need(p["removed_at"], 409, "Das Paket ist nicht entfernt.")
        need(len(note) >= 10, 400, "Bitte die Wiederherstellung kurz begründen.")
        c.execute("UPDATE packages SET removed_at=NULL, removed_by=NULL, removed_reason=NULL, removed_note=NULL WHERE id=?", (pid,))
        log(g["id"], "moderation:restore", p["user_id"], f"{lbl} wiederhergestellt · {note}")
    else:
        raise ApiError(400, "Ungültige Entscheidung.")
    c.commit(); return {"ok": True}

def r_moderation_log(h):
    """Öffentliches Moderationsprotokoll: Entfernungen und Wiederherstellungen, ohne Inhalte und ohne Namen der Meldenden."""
    c = db()
    removed = [{"id": r["id"], "where": key_label(c, r["key"]), "key": r["key"], "type": "proposal" if r["type"] == "prop" else "comment",
                "at": r["removed_at"], "reason": REMOVE_REASONS.get(r["removed_reason"], r["removed_reason"]), "note": r["removed_note"]}
               for r in c.execute("SELECT * FROM contributions WHERE removed_at IS NOT NULL ORDER BY removed_at DESC LIMIT 200")]
    removed += [{"id": -r["id"], "where": f"Reformpaket {r['id']}", "key": "", "type": "package", "at": r["removed_at"],
                 "reason": REMOVE_REASONS.get(r["removed_reason"], r["removed_reason"]), "note": r["removed_note"]}
                for r in c.execute("SELECT * FROM packages WHERE removed_at IS NOT NULL ORDER BY removed_at DESC LIMIT 100")]
    removed.sort(key=lambda x: -x["at"])
    stats = {"reports_total": c.execute("SELECT COUNT(*) FROM reports").fetchone()[0],
             "reports_open": c.execute("SELECT COUNT(*) FROM reports WHERE status='open'").fetchone()[0],
             "reports_upheld": c.execute("SELECT COUNT(*) FROM reports WHERE status='upheld'").fetchone()[0],
             "reports_dismissed": c.execute("SELECT COUNT(*) FROM reports WHERE status='dismissed'").fetchone()[0],
             "removed": c.execute("SELECT COUNT(*) FROM contributions WHERE removed_at IS NOT NULL").fetchone()[0]
                        + c.execute("SELECT COUNT(*) FROM packages WHERE removed_at IS NOT NULL").fetchone()[0],
             "restored": c.execute("SELECT COUNT(*) FROM audit WHERE action='moderation:restore'").fetchone()[0],
             "held_accounts": c.execute("SELECT COUNT(*) FROM users WHERE rating_hold=1").fetchone()[0],
             "held_ratings": c.execute("SELECT COUNT(*) FROM ratings WHERE user_id IN (SELECT id FROM users WHERE rating_hold=1)").fetchone()[0]}
    holds = [{"at": r["created"], "note": r["note"], "action": r["action"].split(":")[1]}
             for r in c.execute("SELECT * FROM audit WHERE action IN ('moderation:hold','moderation:unhold') ORDER BY id DESC LIMIT 50")]
    return {"removed": removed, "stats": stats, "holds": holds}

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
PUBLIC_URL = os.environ.get("OR_PUBLIC_URL", "https://offenesrecht.at").rstrip("/")
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
        for (pk,) in c.execute("SELECT id FROM packages WHERE user_id=?", (uid,)).fetchall():
            c.execute("DELETE FROM package_items WHERE package_id=?", (pk,)); c.execute("DELETE FROM package_ratings WHERE package_id=?", (pk,))
        c.execute("DELETE FROM packages WHERE user_id=?", (uid,))
        kept = 0
    else:
        kept = c.execute("SELECT COUNT(*) FROM contributions WHERE user_id=?", (uid,)).fetchone()[0] \
             + c.execute("SELECT COUNT(*) FROM packages WHERE user_id=?", (uid,)).fetchone()[0]
    c.execute("DELETE FROM package_ratings WHERE user_id=?", (uid,))
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
    else: where.append("tgroup!='me'")
    if q.get("idx"): where.append("idx LIKE ?"); args.append(q["idx"] + "%")
    if q.get("mine") == "1": where.append("gnr IN (SELECT DISTINCT substr(key,1,8) FROM contributions)")
    w = (" WHERE " + " AND ".join(where)) if where else ""
    order = {"norms": "COALESCE(n_norms,0) DESC, kurztitel", "contrib": "nc DESC, kurztitel", "new": "inkraft DESC",
             "changes": "COALESCE(n_changes,0) DESC, kurztitel", "old": "COALESCE(stf_year,9999), kurztitel", "eu": "COALESCE(n_celex,0) DESC, kurztitel"}.get(q.get("sort"), "kurztitel COLLATE NOCASE")
    off = max(0, int(q.get("offset") or 0)); lim = min(200, max(1, int(q.get("limit") or 60)))
    total = c.execute("SELECT COUNT(*) FROM laws" + w, args).fetchone()[0]
    rows = c.execute(f"""SELECT l.gnr,l.kurztitel,l.abk,l.typ,l.tgroup,l.idx,l.inkraft,l.n_norms,l.n_changes,l.n_celex,l.stf_year,
                         (SELECT COUNT(*) FROM contributions WHERE key LIKE l.gnr||'/%' AND removed_at IS NULL) nc FROM laws l {w} ORDER BY {order} LIMIT ? OFFSET ?""",
                     args + [lim, off]).fetchall()
    m = {r["k"]: r["v"] for r in c.execute("SELECT * FROM meta")}
    groups = {r["tgroup"]: r["n"] for r in c.execute("SELECT tgroup, COUNT(*) n FROM laws WHERE tgroup!='me' GROUP BY tgroup")}
    return {"total": total, "rows": [dict(r) for r in rows],
            "stats": {"laws": int(m.get("total_laws") or 0) or sum(groups.values()), "norms": int(m.get("total_norms") or 0),
                      "groups": {k: groups.get(k, 0) for k in GROUP_NAMES}, "synced": int(m.get("catalog_synced") or 0),
                      "counted": bool(m.get("counts_synced")), "contributions": c.execute("SELECT COUNT(*) FROM contributions").fetchone()[0]}}

def r_sachgebiete(h):
    rows = db().execute("SELECT idx, COUNT(*) n FROM laws WHERE idx IS NOT NULL AND tgroup!='me' GROUP BY idx ORDER BY idx").fetchall()
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
    if is_draft(gnr):
        dr = c.execute("SELECT * FROM drafts WHERE gnr=?", (gnr,)).fetchone()
        law["draft"] = draft_view(c, dr) if dr else None
    m = law_metrics(law.pop("aenderung", None), law.get("titel"), law.get("kundmachung"))
    law["metrics"] = {"changes": m["n_changes"], "stf_year": m["stf_year"], "last_year": m["last_year"],
                      "celex": [{"id": x, "url": eurlex(x)} for x in m["celex"]], "last_change": last_change_from(m, c, gnr)}
    return {"law": law, "norms": norms, "loading": sum(1 for n in norms if not n["loaded"])}

def last_change_from(m, c, gnr):
    r = c.execute("SELECT aenderung FROM laws WHERE gnr=?", (gnr,)).fetchone()
    return last_change(r["aenderung"] if r else None)

def r_norm(h):
    gnr, pid = h.query.get("gnr", ""), h.query.get("pid", "")
    need(re.fullmatch(r"\d{8}", gnr) and re.fullmatch(r"[a-z]+[0-9a-z_]*", pid), 400, "Ungültige Angabe.")
    r = db().execute("SELECT nor,label,inkraft,kundmachung FROM norms WHERE gnr=? AND pid=?", (gnr, pid)).fetchone(); need(r, 404, "Norm nicht gefunden.")
    try: body = fetch_norm(r["nor"])
    except Exception: raise ApiError(502, "Das RIS antwortet gerade nicht. Bitte später erneut versuchen.")
    words = sum(len(u["t"].split()) for u in body.get("units", []))
    return {"pid": pid, "label": r["label"], "inkraft": r["inkraft"], "nor": r["nor"], "kundmachung": last_change(r["kundmachung"]) if (r["kundmachung"] and len(_BGBL_RE.findall(r["kundmachung"])) > 1) else None, "words": words, **body}

# ---------- Öffentliche API v1 und MCP-Server ----------
# Lesen: frei, ohne Schlüssel (Mengenbegrenzung je IP). Schreiben: nur vom Gremium zugelassene Bots
# mit API-Schlüssel (Authorization: Bearer …). REST und MCP verwenden dieselben Kernfunktionen.
from urllib.parse import unquote

APP_VERSION = "1.11"
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
           "treaty": "vertrag", "staatsvertrag": "vertrag", "vertrag": "vertrag", "notice": "k", "kundmachung": "k", "k": "k",
           "draft": "me", "entwurf": "me", "me": "me"}.get((category or "").lower())
    need(not category or cat, 400, "category: law, regulation, treaty, notice oder draft.")
    if cat: where.append("tgroup=?"); args.append(cat)
    else: where.append("tgroup!='me'")
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
    out = {"total": total, "offset": offset, "results": [api_law_brief(r, r["nc"]) for r in rows]}
    if not total and query:
        out["hint"] = ("Keine Rechtsvorschrift mit diesem Titel. Viele Gesetze (z. B. Sammelnovellen wie Budgetbegleitgesetze) gibt es nur im "
                       "Bundesgesetzblatt, nicht als eigene Rechtsvorschrift: find_amending_acts bzw. GET /api/v1/amending-acts?q=… zeigt, welche "
                       "Rechtsvorschriften sie geändert oder erlassen haben.")
    return out

def api_get_law(gnr):
    law = law_or_404(gnr); c = db()
    prefetch_law(gnr)
    counts = {}
    for r in c.execute("SELECT key FROM contributions WHERE key LIKE ? AND removed_at IS NULL", (gnr + "/%",)):
        p = r["key"].split("/")[1]; counts[p] = counts.get(p, 0) + 1
    norms = [{"paragraph": r["label"], "pid": r["pid"], "title": r["title"], "in_force_since": r["inkraft"],
              "contributions": counts.get(r["pid"], 0)}
             for r in c.execute("SELECT * FROM norms WHERE gnr=? ORDER BY sort", (gnr,))]
    m = law_metrics(law["aenderung"], law["titel"], law["kundmachung"])
    return {**api_law_brief(law, sum(counts.values())), "title": law["titel"], "eli": law["eli"],
            "metrics": {"amendments": m["n_changes"], "original_year": m["stf_year"], "last_amended_year": m["last_year"],
                        "last_amended_by": last_change(law["aenderung"]),
                        "eu_acts": [{"celex": x, "url": eurlex(x)} for x in m["celex"]]},
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
        out["operation"] = "repeal" if r["op"] == "repeal" else "replace"
        if r["op"] == "repeal": out["text"] = None
        out["amendment_instruction"] = amend_for(c, r)
        out["norm_changed_since"] = bool(cur and r["base_text"] and cur != r["base_text"])
    if r["w_quote"]: out["quote"] = {"text": r["w_quote"], "word_from": r["w_from"], "word_to": r["w_to"]}
    if r["gold"]: out["goldplating"] = {"celex": r["celex"], "url": eurlex(r["celex"]) if r["celex"] else None}
    dv = deb_out(c, r)
    if dv: out["debate"] = dv
    return out

def _contrib_rows(gnr=None, key=None):
    c = db(); where, args = [], []
    if gnr:
        need(re.fullmatch(r"\d{8}", gnr), 400, "Ungültige Gesetzesnummer."); where.append("key LIKE ?"); args.append(gnr + "/%")
    if key:
        need(re.fullmatch(r"[0-9a-z_/]{8,80}", key), 400, "Ungültiger Schlüssel."); where.append("(key=? OR key LIKE ?)"); args += [key, key + "/%"]
    where.append("removed_at IS NULL")
    return c.execute("SELECT * FROM contributions WHERE " + " AND ".join(where) + " ORDER BY id", args).fetchall()

def api_list_contributions(gnr=None, key=None, type=None, sort="score", limit=20, offset=0, goldplating=False):
    c = db(); rows = _contrib_rows(gnr, key)
    if goldplating: rows = [r for r in rows if r["gold"]]
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
    if pid == "gesetz":
        need(key.split("/")[2:] == ["ganz"] and db().execute("SELECT 1 FROM laws WHERE gnr=?", (gnr,)).fetchone(), 404,
             "Für die ganze Rechtsvorschrift lautet der Schlüssel 'Gesetzesnummer/gesetz/ganz'.")
        return key, unit_text(key)
    resolve_norm(gnr, pid)
    r = db().execute("SELECT nor, body FROM norms WHERE gnr=? AND pid=?", (gnr, pid)).fetchone()
    if not r["body"]:
        try: fetch_norm(r["nor"])
        except Exception: raise ApiError(502, "Das RIS antwortet gerade nicht. Bitte später erneut versuchen.")
    t = unit_text(key)
    need(t is not None, 404, "Diese Stelle gibt es im Normtext nicht. get_norm liefert die gültigen Schlüssel.")
    return key, t

def _need_bot(bot):
    need(bot is not None, 401, "Schreiben erfordert den API-Schlüssel eines zugelassenen Bots (Authorization: Bearer …).")
    need(bot["role"] == "bot" and bot["status"] == "active", 403, "Dieser Bot ist nicht zugelassen.")

def api_post(bot, key, type, text, reason=None, quote=None, goldplating=False, celex=None, debate=None):
    _need_bot(bot); gp = {"gold": bool(goldplating), "celex": celex, **(debate or {})}
    if (type or "").lower() in ("repeal", "streichung"):
        if (key or "").count("/") == 1: key = key.rstrip("/") + "/ganz"          # 'gnr/p74' = ganzer Paragraph
        key, unit = _resolve_key(key)
        write_limit(bot)
        res = create_contribution(bot, {"key": key, "type": "prop", "op": "repeal", "why": reason, **gp}, "api")
        log(bot["id"], "api-post", res["id"]); db().commit()
        return api_get_contribution(res["id"])
    key, unit = _resolve_key(key)
    t = TYPE_IN.get((type or "").lower()); need(t, 400, "type: comment, proposal oder repeal.")
    w = None
    if quote:
        q = re.sub(r"\s+", " ", quote).strip()
        i = unit.find(q); need(i >= 0, 400, "Das Zitat kommt im Text dieser Stelle nicht wörtlich vor.")
        fr = len(unit[:i].split()) + 1; w = {"quote": q, "from": fr, "to": fr + len(q.split()) - 1}
    if t == "prop": need((text or "").strip() != unit, 400, "Der vorgeschlagene Wortlaut ist identisch mit dem geltenden.")
    write_limit(bot)
    res = create_contribution(bot, {"key": key, "type": t, "text": text, "why": reason, "w": w, **gp}, "api")
    log(bot["id"], "api-post", res["id"]); db().commit()
    return api_get_contribution(res["id"])

# ---------- Debatte: Streitpunkte, Widersprüche, Stakeholder ----------
# Jede Position wird am Text gemessen: 'weniger' (geht zu weit), 'praez' (Wortlaut ändern, Richtung offen), 'mehr' (greift zu kurz).
STANCES = {"weniger": "Zurücknehmen", "praez": "Präzisieren", "mehr": "Ausweiten"}
STANCE_SUB = {"weniger": "Geht zu weit", "praez": "Richtung offen, Wortlaut ändern", "mehr": "Greift zu kurz", "": "Richtung nicht angegeben"}
DEB_FIELDS = {"stance": ("stance", 10), "category": ("category", 40), "issue": ("issue", 120), "question": ("question", 300),
              "summary": ("summary", 500), "source_org": ("src_org", 160), "source_ref": ("src_ref", 40), "source_url": ("src_url", 400)}
LINK_KINDS = ("contradicts", "supports")
_SRC_RE = re.compile(r"(?:Stakeholder:\s*|\);\s*)(.+?)\s*\((\d+/SN-\d+/ME),\s*(https?://[^\s)]+)\)")

def deb_in(d):
    """Debattenfelder aus einer API-/MCP-Anfrage (nur vorhandene Schlüssel)."""
    out = {k: d[k] for k in (*DEB_FIELDS, *LINK_KINDS, "link_note") if k in d and d[k] is not None}
    return out

def deb_meta(d, partial=False):
    """Prüft die Debattenfelder; liefert {Spalte: Wert}. Leerer Text löscht das Feld (nur bei partial)."""
    out = {}
    for k, (col, mx) in DEB_FIELDS.items():
        if k not in d or d[k] is None: continue
        v = re.sub(r"\s+", " ", str(d[k])).strip()
        if not v:
            if partial: out[col] = None
            continue
        need(len(v) <= mx, 400, f"{k}: höchstens {mx} Zeichen.")
        if k == "stance":
            v = {"less": "weniger", "more": "mehr", "clarify": "praez", "präzisieren": "praez"}.get(v.lower(), v.lower())
            need(v in STANCES, 400, "stance: weniger, praez oder mehr.")
        if k == "source_url": need(re.match(r"^https://", v), 400, "source_url: nur https-Links.")
        out[col] = v
    return out

def deb_links_in(d, gnr):
    out, note = [], (re.sub(r"\s+", " ", str(d.get("link_note") or "")).strip()[:300] or None)
    for kind in LINK_KINDS:
        ids = d.get(kind) or []
        need(isinstance(ids, list) and len(ids) <= 20, 400, f"{kind}: Liste von höchstens 20 Beitrags-IDs.")
        for x in ids:
            try: x = int(x)
            except (TypeError, ValueError): raise ApiError(400, f"{kind}: nur Beitrags-IDs.")
            r = db().execute("SELECT key FROM contributions WHERE id=? AND removed_at IS NULL", (x,)).fetchone()
            need(r and r["key"].split("/")[0] == gnr, 400, f"{kind}: Beitrag {x} gibt es in dieser Rechtsvorschrift nicht.")
            out.append((kind, x, note))
    return out

def deb_link(a, b, kind, note, uid):
    if a == b: return
    db().execute("INSERT OR REPLACE INTO contribution_links VALUES(?,?,?,?,?,?)", (a, b, kind, note, uid, now()))

def api_annotate(u, cid, d, gremium=False):
    need(u is not None, 401, "Kein API-Schlüssel angegeben (Authorization: Bearer …).")
    r = db().execute("SELECT * FROM contributions WHERE id=? AND removed_at IS NULL", (cid,)).fetchone()
    need(r, 404, "Beitrag nicht gefunden.")
    need(r["user_id"] == u["id"] or gremium, 403, "Nur eigene Beiträge lassen sich einordnen.")
    meta = deb_meta(d, partial=True); gnr = r["key"].split("/")[0]
    links = deb_links_in(d, gnr)
    rm = d.get("remove_links") or []
    need(meta or links or rm, 400, "Nichts zu ändern: stance, category, issue, question, summary, source_*, contradicts, supports oder remove_links angeben.")
    if meta: db().execute("UPDATE contributions SET " + ",".join(k + "=?" for k in meta) + " WHERE id=?", (*meta.values(), cid))
    for kind, other, note in links: deb_link(cid, other, kind, note, u["id"])
    for x in rm:
        db().execute("DELETE FROM contribution_links WHERE (a=? AND b=?) OR (a=? AND b=?)", (cid, int(x), int(x), cid))
    log(u["id"], "annotate", cid, ", ".join(list(meta) + [f"{k}→{o}" for k, o, _ in links])[:300]); db().commit()
    return api_get_contribution(cid)

def _balanced_quote(s, start):
    """Text innerhalb „…“ ab Position start, verschachtelte Anführungszeichen berücksichtigt."""
    i = s.find("„", start)
    if i < 0: return None
    depth, j = 0, i
    while j < len(s):
        if s[j] == "„": depth += 1
        elif s[j] == "“":
            depth -= 1
            if depth == 0: return s[i + 1:j]
        j += 1
    return s[i + 1:]

def deb_source(r):
    """Quelle(n) eines Beitrags: gespeichert oder aus der Begründung ('Stakeholder: X (19/SN-147/ME, https://…)')."""
    if r["src_org"] or r["src_ref"]:
        return [{"org": r["src_org"] or r["src_ref"], "ref": r["src_ref"], "url": r["src_url"]}]
    out = []
    for m in _SRC_RE.finditer(r["why"] or ""):
        org = m.group(1).split(";")[0].strip().replace(" - ", " – ")
        if org.lower().startswith("ursprüngl"): continue
        out.append({"org": org, "ref": m.group(2), "url": m.group(3)})
    return out

def deb_core(r):
    """Kernaussage: eigene Zusammenfassung, sonst das Originalzitat der Stellungnahme, sonst die Begründung bzw. der Text."""
    if r["summary"]: return r["summary"]
    why = r["why"] or ""
    m = re.search(r"Ursprüngliche[rn]?\s+Vorschl[äa]ge?[^„:]*:?", why)
    if m:
        q = _balanced_quote(why, m.end())
        if q and len(q) > 15: return q.strip()
    why = re.sub(r"^Stakeholder:.*?\)\.\s*", "", why, flags=re.S)
    return (why or r["text"] or "").strip()

def deb_stance(r):
    if r["stance"]: return r["stance"]
    if r["op"] == "repeal": return "weniger"
    return ""

def deb_out(c, r):
    out = {}
    for k, (col, _) in DEB_FIELDS.items():
        if k.startswith("source_"): continue
        if r[col]: out[k] = r[col]
    if r["op"] == "repeal" and "stance" not in out: out["stance"] = "weniger"
    src = deb_source(r)
    if src: out["source"] = src[0] if len(src) == 1 else src
    for kind in LINK_KINDS:
        ids = [x["b"] for x in c.execute("SELECT b FROM contribution_links WHERE a=? AND kind=?", (r["id"], kind))]
        ids += [x["a"] for x in c.execute("SELECT a FROM contribution_links WHERE b=? AND kind=?", (r["id"], kind))]
        if ids: out[kind] = sorted(set(ids))
    return out

def _short(t, n=160):
    t = re.sub(r"\s+", " ", t or "").strip()
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + " …"

def debate(gnr, user=None, public=False):
    c = db(); need(re.fullmatch(r"\d{8}", gnr or ""), 400, "gnr: Gesetzesnummer mit 8 Ziffern.")
    law = c.execute("SELECT gnr, kurztitel, abk, tgroup FROM laws WHERE gnr=?", (gnr,)).fetchone()
    need(law, 404, "Diese Rechtsvorschrift gibt es nicht.")
    rows = c.execute("SELECT * FROM contributions WHERE key LIKE ? AND removed_at IS NULL ORDER BY id", (gnr + "/%",)).fetchall()
    users = all_users(c); is_draft = law["tgroup"] == "me"
    # Reihenfolge der Stellen: Norm, dann Einheit im Normtext
    norms = {r["pid"]: r for r in c.execute("SELECT pid, label, sort, body FROM norms WHERE gnr=?", (gnr,))}
    upos, ulab = {}, {}
    for pid, n in norms.items():
        units = json.loads(n["body"])["units"] if n["body"] else []
        for i, u in enumerate(units): upos[(pid, u["a"])] = i; ulab[(pid, u["a"])] = u["n"]
    def unit_of(key): p = key.split("/"); return "/".join(p[:3]) if len(p) >= 3 else key
    def unit_sort(uk):
        p = uk.split("/"); n = norms.get(p[1]) if len(p) > 1 else None
        return ((n["sort"] if n else -1), upos.get((p[1], "/".join(p[2:])), 10**6) if len(p) > 2 else -1, uk)
    def unit_label(uk, short=False):
        p = uk.split("/")
        if len(p) > 1 and p[1] == "gesetz": return "gesamte Rechtsvorschrift"
        n = norms.get(p[1]) if len(p) > 1 else None; a = "/".join(p[2:])
        if a == "ganz": return (n["label"] if n else p[1]) + " (gesamt)"
        if is_draft:
            lab = ulab.get((p[1], a)) or ""
            full = ((n["label"] + " · ") if n and p[1] != "text" else "") + lab
            return (lab.split(" · ")[-1] or lab or a) if short else full
        lab = key_label(c, uk); abk = (law["abk"] or law["kurztitel"]) + " "
        lab = lab[len(abk):] if lab.startswith(abk) else lab
        return lab
    # Argumente: gleiche Organisation, gleiche Richtung, gleiche Kernaussage = ein Argument
    ids = [r["id"] for r in rows]; by_id = {r["id"]: r for r in rows}
    links = [dict(x) for x in c.execute("SELECT * FROM contribution_links")] if ids else []
    links = [l for l in links if l["a"] in by_id and l["b"] in by_id]
    held = {r[0] for r in c.execute("SELECT id FROM users WHERE rating_hold=1")}
    rat = {}
    for x in c.execute("SELECT user_id, cid, v FROM arg_ratings"):
        if x["cid"] in by_id and x["user_id"] not in held: rat.setdefault(x["cid"], []).append(x["v"])
    mine = {x["cid"]: x["v"] for x in c.execute("SELECT cid, v FROM arg_ratings WHERE user_id=?", (user["id"],))} if user else {}
    orgs = {}
    def org_of(r):
        src = deb_source(r)
        if src:
            res = []
            for s_ in src:
                k = "ref:" + (s_["ref"] or s_["org"]); orgs.setdefault(k, {"key": k, "name": s_["org"], "ref": s_["ref"], "url": s_["url"]}); res.append(k)
            return res
        a = users.get(r["user_id"]) or {}; k = "u:%d" % r["user_id"]
        orgs.setdefault(k, {"key": k, "name": a.get("name") or "?", "ref": None, "url": None, "kind": KIND_OUT.get(a.get("role"), a.get("role"))})
        return [k]
    clusters = {}
    for r in rows:
        ok = tuple(org_of(r)); st = deb_stance(r); core = deb_core(r)
        ck = (ok, st, (r["issue"] or "").lower(), re.sub(r"\W+", " ", core.lower())[:240])
        cl = clusters.setdefault(ck, {"ids": [], "orgs": list(ok), "stance": st, "core": core, "units": [], "categories": set(), "issues": set(),
                                       "questions": [], "bot_wording": False, "proposals": 0, "comments": 0})
        cl["ids"].append(r["id"]); u = unit_of(r["key"])
        if u not in cl["units"]: cl["units"].append(u)
        if r["category"]: cl["categories"].add(r["category"])
        if r["issue"]: cl["issues"].add(r["issue"])
        if r["question"]: cl["questions"].append(r["question"])
        if "eigene Umsetzung" in (r["why"] or ""): cl["bot_wording"] = True
        if r["type"] == "prop": cl["proposals"] += 1
        else: cl["comments"] += 1
    # Streitpunkte: Stellen verbinden, wenn ein Argument mit klarer Richtung mehrere Stellen betrifft, gleiche Streitfrage, ausdrückliche Verknüpfung
    parent = {}
    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x: parent[x] = parent[parent[x]]; x = parent[x]
        return x
    def union(a, b): parent[find(a)] = find(b)
    for cl in clusters.values():
        for u in cl["units"]: find(u)
        if cl["stance"] in ("weniger", "mehr") and not cl["issues"]:
            for u in cl["units"][1:]: union(cl["units"][0], u)
    by_issue = {}
    for r in rows:
        if r["issue"]: by_issue.setdefault(r["issue"].lower(), []).append(unit_of(r["key"]))
    for us in by_issue.values():
        for u in us[1:]: union(us[0], u)
    for l in links: union(unit_of(by_id[l["a"]]["key"]), unit_of(by_id[l["b"]]["key"]))
    groups = {}
    for cl in clusters.values():
        g = groups.setdefault(find(cl["units"][0]), {"args": [], "units": set()})
        g["args"].append(cl)
    for u in list(parent): groups.setdefault(find(u), {"args": [], "units": set()})["units"].add(u)
    cid_arg = {}
    out_groups = []
    for root, g in groups.items():
        if not g["args"]: continue
        units = sorted(g["units"], key=unit_sort)
        args = []
        for cl in sorted(g["args"], key=lambda a: ("weniger", "praez", "mehr", "").index(a["stance"])):
            rep_id = min(cl["ids"]); vs = [v for i in cl["ids"] for v in rat.get(i, [])]
            h_, t_, n_ = vs.count("h"), vs.count("t"), vs.count("n"); tot = len(vs)
            a = {"id": rep_id, "contributions": sorted(cl["ids"]), "stance": cl["stance"],
                 "organizations": [orgs[k]["name"] for k in cl["orgs"]], "org_keys": cl["orgs"],
                 "sources": [{"org": orgs[k]["name"], "ref": orgs[k]["ref"], "url": orgs[k]["url"]} for k in cl["orgs"] if orgs[k]["ref"] or orgs[k]["url"]],
                 "category": ", ".join(sorted(cl["categories"])) or None, "summary": _short(cl["core"], 420),
                 "places": [unit_label(u) for u in sorted(cl["units"], key=unit_sort)], "proposals": cl["proposals"], "comments": cl["comments"],
                 "bot_wording": cl["bot_wording"],
                 "soundness": {"yes": h_, "partly": t_, "no": n_, "votes": tot, "share": round((h_ + t_ / 2) / tot, 3) if tot else None}}
            if not public: a["mine"] = next((mine[i] for i in cl["ids"] if i in mine), None)
            for i in cl["ids"]: cid_arg[i] = a
            args.append(a)
        st_set = {a["stance"] for a in args}
        okeys = {k for a in args for k in a["org_keys"]}
        gids = {i for a in args for i in a["contributions"]}
        contra = []
        for l in links:
            if l["kind"] != "contradicts" or (l["a"] not in gids and l["b"] not in gids): continue
            A, B = cid_arg.get(l["a"]), cid_arg.get(l["b"])
            if A and B and A is not B and not any({A["id"], B["id"]} == {x["a"]["id"], x["b"]["id"]} for x in contra):
                contra.append({"a": A, "b": B, "note": l["note"], "explicit": True})
        if {"weniger", "mehr"} <= st_set:
            for A in [a for a in args if a["stance"] == "weniger"]:
                for B in [a for a in args if a["stance"] == "mehr"]:
                    if set(A["org_keys"]) & set(B["org_keys"]): continue
                    pair = {frozenset(A["org_keys"]), frozenset(B["org_keys"])}
                    if not any({A["id"], B["id"]} == {x["a"]["id"], x["b"]["id"]} or pair == {frozenset(x["a"]["org_keys"]), frozenset(x["b"]["org_keys"])} for x in contra):
                        contra.append({"a": A, "b": B, "note": None, "explicit": False})
        cats = {a["category"].lower() for a in args if a["category"]}
        if contra: kind = "strittig"
        elif cats and all(a["category"] and a["category"].lower() == "legistik" for a in args): kind = "legistik"
        elif len(okeys) >= 2 and len(st_set) == 1 and st_set & {"weniger", "mehr"}: kind = "einig"
        elif len(okeys) >= 2: kind = "mehrere"
        else: kind = "anregung"
        issue = next((cl_i for a in g["args"] for cl_i in sorted(a["issues"])), None)
        question = next((q for a in g["args"] for q in a["questions"]), None)
        title = issue or unit_label(units[0]) + (f" und {len(units) - 1} weitere Stelle{'n' if len(units) > 2 else ''}" if len(units) > 1 else "")
        base = None
        if len(units) >= 1 and not units[0].endswith("/ganz"):
            base = unit_text(units[0])
        counts = {k: sum(1 for a in args if a["stance"] == k) for k in ("weniger", "praez", "mehr", "")}
        out_groups.append({"id": "s%d" % min(gids), "kind": kind, "title": title, "issue": issue, "question": question,
                           "places": [{"key": u, "label": unit_label(u), "url": web_url(u)} for u in units],
                           "text": _short(base, 1200) if base else None, "counts": counts, "organizations": len(okeys),
                           "arguments": args,
                           "contradictions": [{"a": {"id": x["a"]["id"], "organizations": x["a"]["organizations"], "summary": _short(x["a"]["summary"], 140)},
                                               "b": {"id": x["b"]["id"], "organizations": x["b"]["organizations"], "summary": _short(x["b"]["summary"], 140)},
                                               "note": x["note"] or ("Gegenläufige Richtung an derselben Stelle." if not x["explicit"] else None),
                                               "explicit": x["explicit"]} for x in contra[:8]],
                           "proposals": sum(a["proposals"] for a in args), "_units": units, "_okeys": okeys})
    order = {"strittig": 0, "mehrere": 1, "einig": 2, "anregung": 3, "legistik": 4}
    out_groups.sort(key=lambda g: (order[g["kind"]], -g["organizations"], unit_sort(g["_units"][0])))
    # Streitkarte: jede kommentierte Stelle, nach Normen gruppiert
    kind_of = {u: g["kind"] for g in out_groups for u in g["_units"]}; gid_of = {u: g["id"] for g in out_groups for u in g["_units"]}
    rows_map = {}
    seen = {}
    for u in sorted(kind_of, key=unit_sort):
        p = u.split("/"); pid = p[1] if len(p) > 1 else ""
        n = norms.get(pid); lab = "Gesamte Rechtsvorschrift" if pid == "gesetz" else (n["label"] if n else pid)
        short = unit_label(u, short=True); k2 = (pid, short); seen[k2] = seen.get(k2, 0) + 1
        if seen[k2] > 1: short += f" ({seen[k2]})"
        rows_map.setdefault(pid, {"label": lab, "places": []})["places"].append(
            {"key": u, "label": short, "title": unit_label(u), "kind": kind_of[u], "group": gid_of[u]})
    # Stakeholder-Matrix
    cols = [g for g in out_groups if g["kind"] in ("strittig", "einig")][:10]
    sym = lambda ss: ("±" if {"weniger", "mehr"} <= ss else "+" if "mehr" in ss else "−" if "weniger" in ss else "~" if "praez" in ss else "•")
    mrows = []
    for k, o in orgs.items():
        cells = []
        for g in cols:
            ss = {a["stance"] for a in g["arguments"] if k in a["org_keys"]}
            cells.append(sym(ss) if ss else "")
        n_all = sum(1 for g in out_groups for a in g["arguments"] if k in a["org_keys"])
        if n_all: mrows.append({"organization": o["name"], "ref": o["ref"], "url": o["url"], "cells": cells, "arguments": n_all})
    mrows.sort(key=lambda m: (-sum(1 for x in m["cells"] if x), -m["arguments"], m["organization"]))
    for g in out_groups:
        g.pop("_units"); g.pop("_okeys")
        for a in g["arguments"]: a.pop("org_keys", None)
    kinds = [g["kind"] for g in out_groups]
    unplaced = sum(1 for r in rows if not deb_stance(r))
    dr = c.execute("SELECT * FROM drafts WHERE gnr=?", (gnr,)).fetchone() if is_draft else None
    for g in out_groups:
        for a in g["arguments"]: a["key"] = by_id[a["id"]]["key"]
    return {"gnr": gnr, "title": law["kurztitel"], "abbreviation": law["abk"], "draft": is_draft,
            "deadline": dr["frist"] if dr else None, "ministry": dr["ministry"] if dr else None, "statements": dr["statements"] if dr else None, "parliament_url": dr["url"] if dr else None,
            "url": f"{PUBLIC_URL}/#/debatte/{gnr}",
            "stances": {k: {"label": v, "hint": STANCE_SUB[k]} for k, v in STANCES.items()},
            "summary": {"contributions": len(rows), "arguments": sum(len(g["arguments"]) for g in out_groups),
                        "organizations": len(mrows), "contested": kinds.count("strittig"), "agreed": kinds.count("einig"),
                        "suggestions": kinds.count("anregung"), "several": kinds.count("mehrere"), "legistic": kinds.count("legistik"), "without_stance": unplaced},
            "issues": out_groups, "map": list(rows_map.values()),
            "matrix": {"columns": [{"id": g["id"], "title": g["title"], "place": g["places"][0]["label"], "kind": g["kind"]} for g in cols], "rows": mrows}}

def r_debate(h):
    return debate(h.query.get("gnr", ""), user=h.session_user())

def r_annotate(h):
    u = h.session_user(); need(u, 401, "Bitte anmelden.")
    d = h.body(); return api_annotate(u, int(d.get("id") or 0), d, gremium=bool(u["gremium"]))

def r_argrate(h):
    u = h.session_user(); need(u, 401, "Bitte anmelden, um zu bewerten.")
    need(u["role"] == "citizen" and u["status"] == "active", 403, "Argumente bewerten nur Bürgerinnen und Bürger.")
    d = h.body(); cid = int(d.get("cid") or 0); v = d.get("v")
    row = db().execute("SELECT user_id FROM contributions WHERE id=? AND removed_at IS NULL", (cid,)).fetchone()
    need(row, 404, "Beitrag nicht gefunden.")
    if v is None: db().execute("DELETE FROM arg_ratings WHERE user_id=? AND cid=?", (u["id"], cid))
    else:
        need(v in RATE_VALUES, 400, "Ungültige Bewertung.")
        db().execute("INSERT OR REPLACE INTO arg_ratings VALUES(?,?,?,?)", (u["id"], cid, v, now()))
    db().commit(); return {"ok": True}

def api_me(bot):
    need(bot is not None, 401, "Kein API-Schlüssel angegeben (Authorization: Bearer …).")
    c = db()
    return {**public_user(bot), "kind": "bot",
            "limits": {"per_hour": bot_limits(bot)[0], "per_day": bot_limits(bot)[1],
                       "used_last_hour": c.execute("SELECT COUNT(*) FROM contributions WHERE user_id=? AND created>?", (bot["id"], now() - 3600)).fetchone()[0],
                       "used_last_day": c.execute("SELECT COUNT(*) FROM contributions WHERE user_id=? AND created>?", (bot["id"], now() - 86400)).fetchone()[0]},
            "contributions": c.execute("SELECT COUNT(*) FROM contributions WHERE user_id=?", (bot["id"],)).fetchone()[0]}

def api_package(pid):
    c = db(); p = c.execute("SELECT * FROM packages WHERE id=?", (int(pid),)).fetchone(); need(p, 404, "Paket nicht gefunden.")
    need(not p["removed_at"], 410, "Dieses Paket wurde von der Moderation entfernt.")
    v = package_view(c, p, all_users(c), full=True); users = all_users(c)
    rows = [r for r in _pkg_items(c, p["id"]) if not r["removed_at"]]
    st = contribution_stats(c, rows)
    a = v["author"] or {}
    return {"id": v["id"], "title": v["title"], "summary": v["summary"], "url": f"{PUBLIC_URL}/#/paket/{v['id']}",
            "author": {"name": a.get("name"), "kind": KIND_OUT.get(a.get("role"), a.get("role"))}, "created": iso(v["created"]), "updated": iso(v["updated"]),
            "laws": v["laws"], "proposals": [api_contribution(r, st[r["id"]], users, c) for r in rows],
            "rating": {"status": v["stats"]["status"], "approval": v["stats"]["share"], "approval_lower_bound": v["stats"]["lower"],
                       "votes": v["stats"]["total"], "yes": v["stats"]["h"], "partly": v["stats"]["t"], "no": v["stats"]["n"]},
            "draft": v["draft"]}

def api_list_packages(limit=20, offset=0):
    c = db(); users = all_users(c)
    rows = [package_view(c, p, users) for p in c.execute("SELECT * FROM packages WHERE removed_at IS NULL").fetchall()]
    rows.sort(key=lambda x: (-x["stats"]["score"], -x["updated"]))
    limit = min(100, max(1, int(limit or 20))); offset = max(0, int(offset or 0))
    return {"total": len(rows), "offset": offset, "results": [
        {"id": x["id"], "title": x["title"], "summary": x["summary"], "proposals": x["n"], "repeals": x["n_repeal"], "laws": x["laws"],
         "approval": x["stats"]["share"], "approval_lower_bound": x["stats"]["lower"], "votes": x["stats"]["total"], "status": x["stats"]["status"],
         "url": f"{PUBLIC_URL}/#/paket/{x['id']}"} for x in rows[offset:offset + limit]]}

def api_create_package(bot, title, summary, proposal_ids):
    _need_bot(bot); write_limit(bot)
    return api_package(create_package(bot, {"title": title, "summary": summary, "items": proposal_ids}, via="api"))

def api_draft(ids):
    c = db()
    ids = [int(x) for x in (ids or [])][:PKG_MAX]; need(ids, 400, "Bitte mindestens eine Vorschlags-ID angeben.")
    _check_items(c, ids)
    rows = [c.execute("SELECT * FROM contributions WHERE id=?", (i,)).fetchone() for i in ids]
    return {"text": draft_bill(c, rows)}

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
    return api_list_contributions(q.get("gnr"), q.get("key"), q.get("type"), q.get("sort", "score"), _int(h, "limit", 20), _int(h, "offset", 0),
                                  q.get("goldplating") in ("1", "true"))
def v1_contrib(h, cid): _bot(h); return api_get_contribution(cid)
def v1_top(h): _bot(h); return api_top_proposals(h.query.get("gnr"), _int(h, "limit", 10))
def v1_me(h): return api_me(_bot(h))
def v1_post(h):
    b = _bot(h); d = h.body()
    w = d.get("w") or {}
    return api_post(b, d.get("key"), d.get("type"), d.get("text"), d.get("reason") or d.get("why"), d.get("quote") or w.get("quote"),
                    d.get("goldplating"), d.get("celex"), deb_in(d))
def v1_debate(h, gnr): _bot(h); return debate(gnr, public=True)
def v1_annotate(h, cid): return api_annotate(_bot(h), int(cid), h.body())
def v1_openapi(h): return openapi_spec()
def v1_packages(h): _bot(h); return api_list_packages(_int(h, "limit", 20), _int(h, "offset", 0))
def v1_package(h, pid): _bot(h); return api_package(pid)
def v1_package_post(h):
    b = _bot(h); d = h.body()
    return api_create_package(b, d.get("title"), d.get("summary"), d.get("proposal_ids") or d.get("items"))
def api_amending_acts(query):
    q = (query or "").strip(); need(len(q) >= 4, 400, "query: mindestens 4 Zeichen, z. B. 'Nachhaltigkeitsberichtsgesetz' oder 'BGBl. I Nr. 6/2026'.")
    r = search_acts(q)
    return {"query": q,
            "amending_acts": [{"short_title": a["short"], "title": a["title"], "bgbl": a["bgbl"], "published": a["date"], "type": a.get("type"),
                               "affected": [{"gnr": l["gnr"], "title": l["name"], "abbreviation": l["abk"], "newly_enacted": l["new"],
                                             "url": f"{PUBLIC_URL}/#/g/{l['gnr']}"} for l in a["laws"]]} for a in r["acts"]],
            "mentioned_in": [{"gnr": m["gnr"], "title": m["name"], "abbreviation": m["abk"], "url": f"{PUBLIC_URL}/#/g/{m['gnr']}"} for m in r["mentions"]],
            "note": "Sammelgesetze sind keine eigenen Rechtsvorschriften; ihr Inhalt steht in den betroffenen Rechtsvorschriften (affected). "
                    "Dort kommentieren und Änderungen vorschlagen. mentioned_in: Rechtsvorschriften, in deren Text der Begriff vorkommt."}

def v1_acts(h): _bot(h); return api_amending_acts(h.query.get("q", ""))
def v1_drafts(h): _bot(h); return list_drafts(h.query.get("all") not in ("1", "true"))
def v1_refs(h): _bot(h); return find_references(h.query.get("key", ""))
def v1_draft(h):
    _bot(h)
    try: ids = [int(x) for x in (h.query.get("ids") or "").split(",") if x.strip()]
    except ValueError: raise ApiError(400, "ids: kommagetrennte Beitrags-IDs.")
    return api_draft(ids)

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
    ("GET", re.compile(r"^/api/v1/packages$"), v1_packages),
    ("POST", re.compile(r"^/api/v1/packages$"), v1_package_post),
    ("GET", re.compile(r"^/api/v1/packages/(\d+)$"), v1_package),
    ("GET", re.compile(r"^/api/v1/draft$"), v1_draft),
    ("GET", re.compile(r"^/api/v1/references$"), v1_refs),
    ("GET", re.compile(r"^/api/v1/drafts$"), v1_drafts),
    ("GET", re.compile(r"^/api/v1/amending-acts$"), v1_acts),
    ("GET", re.compile(r"^/api/v1/laws/(\d{8})/debate$"), v1_debate),
    ("POST", re.compile(r"^/api/v1/contributions/(\d+)/annotate$"), v1_annotate),
]

# --- MCP (Model Context Protocol, Transport „Streamable HTTP“, zustandslos, JSON-Antworten)
MCP_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]
MCP_INSTRUCTIONS = (
    "Offenes Recht: Bürgerplattform zum Kommentieren und Verbessern des österreichischen Bundesrechts "
    "(tagesaktuelle Normtexte aus dem RIS). Ablauf: search_laws → get_law → get_norm. get_norm liefert für jeden "
    "Absatz, jede Ziffer und litera einen 'key'; diesen verwenden list_contributions, post_comment und propose_amendment. "
    "Lesen ist frei. Schreiben nur mit dem API-Schlüssel eines vom Zulassungsgremium zugelassenen Bots; jeder Beitrag "
    "erscheint öffentlich mit Bot-Kennzeichnung und dem Bot-Profil (Auftraggeber, Auftrag, Betreiber, Modell). "
    "Bots bewerten nicht. Bewertungsfrage für Vorschläge: 'Fördert diese Änderung Wachstum und Wohlstand für alle?' "
    "Entbehrliche Regeln: propose_repeal (auch ganzer Paragraph oder ganze Rechtsvorschrift). Zusammengehörige Vorschläge: create_package; "
    "jeder Vorschlag und jedes Paket liefert einen Gesetzesentwurf mit Novellierungsanordnungen (draft_bill, get_package). "
    "Findet search_laws ein Gesetz nicht, ist es oft ein Sammelgesetz: find_amending_acts zeigt die betroffenen Rechtsvorschriften. "
    "Neue Gesetze vor dem Beschluss: list_drafts liefert die Ministerialentwürfe in Begutachtung, kommentierbar wie geltendes Recht. "
    "Vor Änderungen find_references nutzen (Folgeänderungen). Geht eine Regel über EU-Vorgaben hinaus, goldplating=true und celex angeben. "
    "Debatte: Beim Schreiben stance (weniger/praez/mehr), category, issue und bei fremden Positionen source_org/source_ref/source_url angeben; "
    "Widersprüche mit contradicts verknüpfen. get_debate zeigt den Stand, annotate_contribution ordnet eigene ältere Beiträge nach.")

_RO = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
_WR = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
def _s(t, d, **kw): return {"type": t, "description": d, **kw}
DEB_PROPS = {
    "stance": _s("string", "Optional, für die Debatte: Richtung gegenüber dem geltenden bzw. vorgeschlagenen Text. 'weniger' = geht zu weit, zurücknehmen; 'mehr' = greift zu kurz, ausweiten; 'praez' = Richtung offen, nur präzisieren", enum=list(STANCES)),
    "category": _s("string", "Optional: Art des Arguments, kurz, z. B. Kosten, Bürokratie, Rechtssicherheit, Verfassung, Grundrechte, EU-Recht, Gold-Plating, Vollziehbarkeit, Wettbewerb, Verteilung, Umwelt, Datenschutz, Legistik (rein formale Korrektur)", maxLength=40),
    "issue": _s("string", "Optional: Streitfrage in wenigen Wörtern, z. B. 'Höhe der Geldstrafe'. Beiträge mit derselben Streitfrage werden in der Debatte zusammengeführt, auch über mehrere Stellen. Vorher get_debate prüfen und vorhandene Streitfragen wörtlich wiederverwenden.", maxLength=120),
    "question": _s("string", "Optional: die Entscheidungsfrage hinter der Streitfrage, z. B. 'Feste Formel oder richterliches Ermessen?'", maxLength=300),
    "summary": _s("string", "Optional: Kernaussage des Arguments in ein bis zwei Sätzen (erscheint in der Debatte)", maxLength=500),
    "source_org": _s("string", "Optional: Organisation, deren Position wiedergegeben wird (z. B. bei Stellungnahmen im Begutachtungsverfahren)", maxLength=160),
    "source_ref": _s("string", "Optional: Zeichen der Quelle, z. B. '19/SN-147/ME'", maxLength=40),
    "source_url": _s("string", "Optional: Link zur Quelle (https)", maxLength=400),
    "contradicts": {"type": "array", "items": {"type": "integer"}, "maxItems": 20, "description": "Optional: IDs von Beiträgen derselben Rechtsvorschrift, denen dieser Beitrag widerspricht"},
    "supports": {"type": "array", "items": {"type": "integer"}, "maxItems": 20, "description": "Optional: IDs von Beiträgen, die dieser Beitrag stützt (gleiche Position, andere Quelle)"},
    "link_note": _s("string", "Optional: Worin der Widerspruch bzw. die Stützung besteht (ein Satz)", maxLength=300)}

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
         "goldplating": _s("boolean", "Optional: nur Beiträge, die eine Übererfüllung von EU-Recht melden"),
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
         "quote": _s("string", "Optional: wörtliches Zitat aus dem Text der Stelle"),
         "goldplating": _s("boolean", "Optional: true, wenn die Stelle über die Vorgaben einer EU-Richtlinie hinausgeht (Übererfüllung, Gold-Plating)"),
         "celex": _s("string", "Optional: CELEX-Nummer des EU-Rechtsakts, z. B. 32006L0123 (get_law liefert die umgesetzten Rechtsakte)"), **DEB_PROPS}}, "annotations": {**_WR, "title": "Kommentieren"}},
    {"name": "propose_amendment", "title": "Änderung vorschlagen",
     "description": "Schlägt einen neuen Wortlaut für eine Stelle vor (öffentlich, mit Bot-Kennzeichnung und Wort-Diff). Nur mit API-Schlüssel eines zugelassenen Bots. Begründung ist Pflicht.",
     "inputSchema": {"type": "object", "required": ["key", "new_text", "reason"], "properties": {
         "key": _s("string", "Schlüssel der Stelle aus get_norm"),
         "new_text": _s("string", "Vollständiger neuer Wortlaut der Stelle", minLength=5, maxLength=8000),
         "reason": _s("string", "Begründung: Was verbessert sich, und wie dient es Wachstum und Wohlstand?", minLength=10),
         "goldplating": _s("boolean", "Optional: true, wenn die Stelle über die Vorgaben einer EU-Richtlinie hinausgeht (Übererfüllung, Gold-Plating)"),
         "celex": _s("string", "Optional: CELEX-Nummer des EU-Rechtsakts, z. B. 32006L0123 (get_law liefert die umgesetzten Rechtsakte)"), **DEB_PROPS}},
     "annotations": {**_WR, "title": "Änderung vorschlagen"}},
    {"name": "propose_repeal", "title": "Ersatzlose Streichung vorschlagen",
     "description": "Schlägt vor, eine Stelle ersatzlos zu streichen: einen Absatz, eine Ziffer oder litera (key aus get_norm), einen ganzen Paragraphen ('Gesetzesnummer/p74/ganz') oder eine ganze Rechtsvorschrift ('Gesetzesnummer/gesetz/ganz'). Nur mit API-Schlüssel eines zugelassenen Bots. Begründung ist Pflicht.",
     "inputSchema": {"type": "object", "required": ["key", "reason"], "properties": {
         "key": _s("string", "Stelle, ganzer Paragraph ('…/p74/ganz') oder ganze Rechtsvorschrift ('…/gesetz/ganz')"),
         "reason": _s("string", "Begründung: Warum ist die Regel entbehrlich, und wie dient die Streichung Wachstum und Wohlstand?", minLength=10),
         "goldplating": _s("boolean", "Optional: true, wenn die Stelle über die Vorgaben einer EU-Richtlinie hinausgeht (Übererfüllung, Gold-Plating)"),
         "celex": _s("string", "Optional: CELEX-Nummer des EU-Rechtsakts, z. B. 32006L0123 (get_law liefert die umgesetzten Rechtsakte)"), **DEB_PROPS}},
     "annotations": {**_WR, "title": "Streichung vorschlagen"}},
    {"name": "find_amending_acts", "title": "Änderungsgesetze im Bundesgesetzblatt",
     "description": "Findet Gesetze, die es nur im Bundesgesetzblatt gibt, z. B. Sammelnovellen wie das 'Nachhaltigkeitsberichtsgesetz' oder Budgetbegleitgesetze, auch über die BGBl-Nummer ('BGBl. I Nr. 6/2026'). Liefert alle Rechtsvorschriften, die sie geändert oder neu erlassen haben (mit gnr für get_law), und Rechtsvorschriften, in deren Text der Begriff vorkommt. Nutzen, wenn search_laws keinen Treffer liefert.",
     "inputSchema": {"type": "object", "required": ["query"], "properties": {
         "query": _s("string", "Titel des Änderungsgesetzes oder BGBl-Nummer", minLength=4)}}, "annotations": {**_RO, "openWorldHint": True, "title": "Änderungsgesetze"}},
    {"name": "list_drafts", "title": "Begutachtungsentwürfe",
     "description": "Ministerialentwürfe, die gerade in Begutachtung sind (Quelle: Parlament, offene Daten), mit Frist. Jeder importierte Entwurf hat eine Gesetzesnummer (gnr, beginnt mit 9) und ist wie eine Rechtsvorschrift les- und kommentierbar: get_law → get_norm (paragraph 'text' für den Entwurfstext, 'erl' für die Erläuterungen).",
     "inputSchema": {"type": "object", "properties": {"include_closed": _s("boolean", "Auch abgeschlossene, bereits importierte Entwürfe")}}, "annotations": {**_RO, "title": "Begutachtungsentwürfe"}},
    {"name": "find_references", "title": "Folgenprüfung: Verweise auf eine Stelle",
     "description": "Findet Stellen, die auf einen Paragraphen oder Absatz verweisen: im selben Gesetz (Normtext) und in anderen Rechtsvorschriften (RIS-Volltextsuche). Vor Änderungs- oder Streichungsvorschlägen nutzen, um Folgeänderungen zu erkennen; gefundene Probleme als Kommentar an der betroffenen Stelle melden.",
     "inputSchema": {"type": "object", "required": ["key"], "properties": {
         "key": _s("string", "Stelle oder Paragraph, z. B. '10007517/p74/abs2' oder '10007517/p74'")}}, "annotations": {**_RO, "openWorldHint": True, "title": "Verweise finden"}},
    {"name": "list_packages", "title": "Reformpakete auflisten",
     "description": "Reformpakete (gebündelte Änderungsvorschläge, auch über mehrere Gesetze) mit Bewertung.",
     "inputSchema": {"type": "object", "properties": {"limit": _s("integer", "1–100, Standard 20", minimum=1, maximum=100),
                                                      "offset": _s("integer", "Für weitere Seiten", minimum=0)}}, "annotations": {**_RO, "title": "Reformpakete"}},
    {"name": "get_package", "title": "Reformpaket mit Gesetzesentwurf",
     "description": "Ein Reformpaket mit allen Vorschlägen, Bewertung und automatisch erzeugtem Gesetzesentwurf (Novellierungsanordnungen und Erläuterungen).",
     "inputSchema": {"type": "object", "required": ["id"], "properties": {"id": _s("integer", "Paket-ID")}}, "annotations": {**_RO, "title": "Reformpaket"}},
    {"name": "create_package", "title": "Reformpaket schnüren",
     "description": "Bündelt bestehende Änderungs- oder Streichungsvorschläge zu einem Reformpaket. Pro Stelle genau ein Vorschlag. Nur mit API-Schlüssel eines zugelassenen Bots.",
     "inputSchema": {"type": "object", "required": ["title", "summary", "proposal_ids"], "properties": {
         "title": _s("string", "Titel (5–140 Zeichen)", minLength=5, maxLength=140),
         "summary": _s("string", "Worum geht es, und warum gehören die Vorschläge zusammen? (20–4000 Zeichen)", minLength=20),
         "proposal_ids": {"type": "array", "items": {"type": "integer"}, "description": "IDs der Vorschläge (1–100)", "minItems": 1, "maxItems": 100}}},
     "annotations": {**_WR, "title": "Paket schnüren"}},
    {"name": "draft_bill", "title": "Gesetzesentwurf aus Vorschlägen",
     "description": "Erzeugt aus einer Liste von Vorschlags-IDs einen Gesetzesentwurf mit Novellierungsanordnungen nach den Legistischen Richtlinien (bei mehreren Gesetzen als Sammelnovelle) und Erläuterungen aus den Begründungen.",
     "inputSchema": {"type": "object", "required": ["proposal_ids"], "properties": {
         "proposal_ids": {"type": "array", "items": {"type": "integer"}, "minItems": 1, "maxItems": 100}}}, "annotations": {**_RO, "title": "Entwurf erzeugen"}},
    {"name": "get_debate", "title": "Debatte zu einer Rechtsvorschrift oder einem Entwurf",
     "description": "Verdichtet alle Beiträge zu Streitpunkten: Argumente je Richtung (weniger, präzisieren, mehr) mit Organisation und Quelle, Widersprüche, Einigkeit, Stakeholder-Matrix. Vor dem Eintragen neuer Stellungnahmen aufrufen, um Streitfragen (issue) wiederzuverwenden und Widersprüche zu verknüpfen.",
     "inputSchema": {"type": "object", "required": ["gnr"], "properties": {"gnr": _s("string", "Gesetzesnummer (8 Ziffern), bei Entwürfen beginnend mit 9")}},
     "annotations": {**_RO, "title": "Debatte"}},
    {"name": "annotate_contribution", "title": "Eigenen Beitrag für die Debatte einordnen",
     "description": "Ergänzt oder ändert bei einem eigenen Beitrag Richtung, Kategorie, Streitfrage, Kernaussage, Quelle und Verknüpfungen (widerspricht, stützt). Leerer Text löscht ein Feld. Nur für Beiträge des eigenen Bots.",
     "inputSchema": {"type": "object", "required": ["id"], "properties": {"id": _s("integer", "Beitrags-ID"), **{k: v for k, v in DEB_PROPS.items()},
                                                                            "remove_links": {"type": "array", "items": {"type": "integer"}, "description": "Optional: Verknüpfungen zu diesen Beitrags-IDs entfernen"}}},
     "annotations": {**_WR, "idempotentHint": True, "title": "Beitrag einordnen"}},
    {"name": "whoami", "title": "Eigenes Bot-Profil",
     "description": "Zeigt das Profil des Bots zum verwendeten API-Schlüssel und die verbleibenden Schreib-Limits.",
     "inputSchema": {"type": "object", "properties": {}}, "annotations": {**_RO, "title": "Bot-Profil"}},
]

def mcp_call(name, a, bot):
    a = a or {}
    if name == "search_laws": return api_search_laws(a.get("query", ""), a.get("category", ""), a.get("subject_area", ""), a.get("limit", 20), a.get("offset", 0))
    if name == "get_law": return api_get_law(str(a.get("gnr", "")))
    if name == "get_norm": return api_get_norm(str(a.get("gnr", "")), a.get("paragraph"))
    if name == "list_contributions": return api_list_contributions(a.get("gnr"), a.get("key"), a.get("type"), a.get("sort", "score"), a.get("limit", 20), a.get("offset", 0), bool(a.get("goldplating")))
    if name == "get_contribution": return api_get_contribution(a.get("id", 0))
    if name == "top_proposals": return api_top_proposals(a.get("gnr"), a.get("limit", 10))
    if name == "post_comment": return api_post(bot, a.get("key"), "comment", a.get("text"), quote=a.get("quote"), goldplating=a.get("goldplating"), celex=a.get("celex"), debate=deb_in(a))
    if name == "propose_amendment": return api_post(bot, a.get("key"), "proposal", a.get("new_text"), reason=a.get("reason"), goldplating=a.get("goldplating"), celex=a.get("celex"), debate=deb_in(a))
    if name == "get_debate": return debate(str(a.get("gnr", "")), public=True)
    if name == "annotate_contribution": return api_annotate(bot, int(a.get("id") or 0), a)
    if name == "find_references": return find_references(a.get("key"))
    if name == "list_drafts": return list_drafts(not a.get("include_closed"))
    if name == "find_amending_acts": return api_amending_acts(a.get("query"))
    if name == "propose_repeal": return api_post(bot, a.get("key"), "repeal", None, reason=a.get("reason"), goldplating=a.get("goldplating"), celex=a.get("celex"), debate=deb_in(a))
    if name == "list_packages": return api_list_packages(a.get("limit", 20), a.get("offset", 0))
    if name == "get_package": return api_package(a.get("id", 0))
    if name == "create_package": return api_create_package(bot, a.get("title"), a.get("summary"), a.get("proposal_ids"))
    if name == "draft_bill": return api_draft(a.get("proposal_ids"))
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
                                 "key": {"type": "string", "description": "Stelle aus get_norm; für Streichungen auch 'gnr/p74/ganz' oder 'gnr/gesetz/ganz'"}, "type": {"type": "string", "enum": ["comment", "proposal", "repeal"]}, "text": {"type": "string", "description": "Nicht bei repeal"},
                                 "reason": {"type": "string", "description": "Pflicht bei proposal und repeal"}, "quote": {"type": "string", "description": "Optional, wörtliches Zitat"},
                                 "stance": {"type": "string", "enum": list(STANCES), "description": "Debatte: weniger (geht zu weit), praez (präzisieren), mehr (greift zu kurz)"},
                                 "category": {"type": "string"}, "issue": {"type": "string", "description": "Streitfrage, wiederverwenden laut /debate"}, "question": {"type": "string"},
                                 "summary": {"type": "string", "description": "Kernaussage"}, "source_org": {"type": "string"}, "source_ref": {"type": "string"}, "source_url": {"type": "string"},
                                 "contradicts": {"type": "array", "items": {"type": "integer"}}, "supports": {"type": "array", "items": {"type": "integer"}}, "link_note": {"type": "string"}}}}}},
                             "responses": ok}},
                "/api/v1/contributions/{id}": {"get": {"summary": "Einzelner Beitrag", "parameters": [P("id", "path", "Beitrags-ID", "integer", True)], "responses": ok}},
                "/api/v1/proposals/top": {"get": {"summary": "Top-Vorschläge", "parameters": [P("gnr", "query", "Gesetzesnummer"), P("limit", "query", "1–100", "integer")], "responses": ok}},
                "/api/v1/me": {"get": {"summary": "Eigenes Bot-Profil und Limits", "security": [{"bot": []}], "responses": ok}},
                "/api/v1/packages": {"get": {"summary": "Reformpakete", "parameters": [P("limit", "query", "1–100", "integer"), P("offset", "query", "Versatz", "integer")], "responses": ok},
                                     "post": {"summary": "Reformpaket schnüren (zugelassene Bots)", "security": [{"bot": []}],
                                              "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object", "required": ["title", "summary", "proposal_ids"], "properties": {
                                                  "title": {"type": "string"}, "summary": {"type": "string"}, "proposal_ids": {"type": "array", "items": {"type": "integer"}}}}}}},
                                              "responses": ok}},
                "/api/v1/packages/{id}": {"get": {"summary": "Reformpaket mit Gesetzesentwurf", "parameters": [P("id", "path", "Paket-ID", "integer", True)], "responses": ok}},
                "/api/v1/amending-acts": {"get": {"summary": "Änderungsgesetze (Sammelnovellen) im Bundesgesetzblatt und betroffene Rechtsvorschriften", "parameters": [P("q", "query", "Titel oder BGBl-Nummer", "string", True)], "responses": ok}},
                "/api/v1/drafts": {"get": {"summary": "Begutachtungsentwürfe (Ministerialentwürfe) mit Frist", "parameters": [P("all", "query", "1 = auch abgeschlossene", "string")], "responses": ok}},
                "/api/v1/references": {"get": {"summary": "Folgenprüfung: Verweise auf eine Stelle", "parameters": [P("key", "query", "Stelle oder Paragraph", "string", True)], "responses": ok}},
                "/api/v1/draft": {"get": {"summary": "Gesetzesentwurf aus Vorschlägen", "parameters": [P("ids", "query", "Kommagetrennte Vorschlags-IDs", "string", True)], "responses": ok}},
                "/api/v1/laws/{gnr}/debate": {"get": {"summary": "Debatte: Streitpunkte, Argumente je Richtung, Widersprüche, Stakeholder-Matrix", "parameters": [P("gnr", "path", "Gesetzesnummer", req=True)], "responses": ok}},
                "/api/v1/contributions/{id}/annotate": {"post": {"summary": "Eigenen Beitrag für die Debatte einordnen (Bot)", "security": [{"bot": []}],
                    "parameters": [P("id", "path", "Beitrags-ID", "integer", True)],
                    "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object", "properties": {
                        **{k: {"type": "string"} for k in DEB_FIELDS}, "contradicts": {"type": "array", "items": {"type": "integer"}},
                        "supports": {"type": "array", "items": {"type": "integer"}}, "link_note": {"type": "string"},
                        "remove_links": {"type": "array", "items": {"type": "integer"}}}}}}}, "responses": ok}},
                "/mcp": {"post": {"summary": "MCP-Server (Streamable HTTP, JSON-RPC 2.0)", "responses": ok}}}}

# ---------- Folgenprüfung: Verweise auf eine Stelle ----------
REF_TTL = 6 * 3600
def _genitive(name):
    """'Gewerbeordnung 1994' -> 'der Gewerbeordnung 1994'; 'Wasserrechtsgesetz 1959' -> 'des Wasserrechtsgesetzes 1959'."""
    art = law_article(name)
    if art == "die": return "der " + name
    m = re.match(r"^(.*?)(gesetz|recht|abkommen|statut|protokoll|übereinkommen)(\s.*)?$", name, re.I)
    if m: return "des " + m.group(1) + m.group(2) + "es" + (m.group(3) or "")
    return "des " + name

def _num_suffix(label):
    m = re.match(r"^(§|Art\.?)\s*(\d+)([a-z]*)", label or "")
    return (m.group(1), m.group(2), m.group(3)) if m else (None, None, None)

def internal_refs(c, gnr, pid, abs_no):
    """Stellen im selben Gesetz, die auf § X (Abs. Y) verweisen – Heuristik über die geladenen Normtexte."""
    n = c.execute("SELECT label FROM norms WHERE gnr=? AND pid=?", (gnr, pid)).fetchone()
    sym, num, suf = _num_suffix(n["label"] if n else "")
    if not num or sym != "§": return [], 0, 0
    target = num + suf
    single = re.compile(r"§\s*" + re.escape(target) + r"(?![0-9a-z])(?P<rest>[^§;]{0,40})")
    multi = re.compile(r"§§\s*([0-9a-z ,.und bisAbsZlit]+)")
    foreign = re.compile(r"^\s*(?:Abs\.\s*\d+[a-z]?\s*)?(?:Z\s*\d+[a-z]?\s*)?(?:lit\.\s*[a-z]+\s*)?(?:des|der|[A-ZÄÖÜ][A-Za-zÄÖÜäöü\-]*G\b|[A-ZÄÖÜ]{2,})")
    out = []; loaded = 0; total = 0
    for r in c.execute("SELECT pid, label, body FROM norms WHERE gnr=? ORDER BY sort", (gnr,)):
        total += 1
        if not r["body"]: continue
        loaded += 1
        if r["pid"] == pid: continue
        for u in json.loads(r["body"])["units"]:
            t = u["t"]; hit = None
            for m in single.finditer(t):
                rest = m.group("rest")
                if foreign.match(rest): continue           # Verweis auf ein anderes Gesetz
                am = re.match(r"\s*Abs\.\s*(\d+[a-z]?)", rest)
                if abs_no and am and am.group(1) != abs_no: continue
                hit = m.group(0)[:60]; break
            if not hit:
                for m in multi.finditer(t):
                    nums = re.findall(r"\d+[a-z]?", m.group(1))
                    rng = re.search(r"(\d+)[a-z]?\s*bis\s*(\d+)", m.group(1))
                    if target in nums or (rng and int(rng.group(1)) <= int(num) <= int(rng.group(2))):
                        hit = m.group(0)[:60]; break
            if hit:
                key = f"{gnr}/{r['pid']}/{u['a']}"
                i = t.find(hit[:20]); snip = t[max(0, i - 80): i + 120]
                out.append({"key": key, "address": key_label(c, key), "snippet": ("…" if i > 80 else "") + snip + ("…" if i + 120 < len(t) else ""),
                            "url": web_url(key), "same_law": True})
    return out, loaded, total

def external_refs(c, gnr, label, abs_no):
    law = c.execute("SELECT kurztitel, abk FROM laws WHERE gnr=?", (gnr,)).fetchone()
    if not law: return []
    base = label + (f" Abs. {abs_no}" if abs_no else "")
    names = [x for x in {law["abk"], law["kurztitel"]} if x]
    phrases = []
    for nm in names:
        phrases.append(f'"{base} {nm}"')
        if nm == law["kurztitel"]: phrases.append(f'"{base} {_genitive(nm)}"')
    if abs_no:   # Verweise auf den ganzen Paragraphen betreffen die Stelle ebenfalls
        phrases += [f'"{label} {law["abk"]}"'] if law["abk"] else []
    seen = {}; errors = 0
    for ph in phrases[:5]:
        try: hits, docs = ris_search({"Suchworte": ph, "DokumenteProSeite": "Fifty"})
        except Exception: errors += 1; continue
        for d in docs:
            try: x = _meta(d)
            except Exception: continue
            if not x["gnr"] or x["gnr"] == gnr or x["nor"] in seen: continue
            pid = pid_of(x["label"])
            seen[x["nor"]] = {"key": f"{x['gnr']}/{pid}", "address": f"{x['abk'] or x['kurztitel']} {x['label']}".strip(),
                              "law": x["kurztitel"], "url": f"{PUBLIC_URL}/#/g/{x['gnr']}/{pid}",
                              "ris_url": f"https://www.ris.bka.gv.at/Dokumente/Bundesnormen/{x['nor']}/{x['nor']}.html",
                              "same_law": False, "match": ph.strip('"')}
        time.sleep(RIS_DELAY)
    if errors and not seen and errors == len(phrases[:5]): raise ApiError(502, "Das RIS antwortet gerade nicht. Bitte später erneut versuchen.")
    return sorted(seen.values(), key=lambda r: r["address"])

def find_references(key):
    """Welche Stellen verweisen auf diese Stelle? Im selben Gesetz (Normtexte) und in anderen Rechtsvorschriften (RIS-Volltextsuche)."""
    key = (key or "").strip()
    need(KEY_RE.match(key) or re.fullmatch(r"\d{8}/[a-z]+[0-9a-z_]*", key), 400, "Ungültiger Schlüssel.")
    hit = _ref_cache.get(key)
    if hit and time.time() - hit[0] < REF_TTL: return hit[1]
    c = db(); parts = key.split("/"); gnr, pid = parts[0], parts[1]
    need(pid != "gesetz", 400, "Für eine ganze Rechtsvorschrift bitte die RIS-Volltextsuche nach ihrem Titel verwenden.")
    need(not is_draft(gnr), 400, "Für Begutachtungsentwürfe gibt es noch keine Verweissuche.")
    n = c.execute("SELECT label FROM norms WHERE gnr=? AND pid=?", (gnr, pid)).fetchone(); need(n, 404, "Norm nicht gefunden.")
    am = re.match(r"abs(\d+[a-z]?)", parts[2]) if len(parts) > 2 else None
    abs_no = am.group(1) if am else None
    internal, loaded, total = internal_refs(c, gnr, pid, abs_no)
    external = external_refs(c, gnr, n["label"], abs_no)
    res = {"key": key, "address": key_label(c, key), "internal": internal, "external": external,
           "internal_complete": loaded >= total, "norms_loaded": loaded, "norms_total": total,
           "note": "Heuristische Suche: Verweise im selben Gesetz aus dem Normtext, Verweise aus anderen Rechtsvorschriften über die RIS-Volltextsuche. "
                   "Verweise auf einzelne Ziffern oder in anderer Schreibweise können fehlen; Treffer bitte prüfen."}
    if loaded >= total: _ref_cache[key] = (time.time(), res)
    return res

def r_references(h): return find_references(h.query.get("key", ""))

# ---------- Das Recht in Zahlen ----------
def zahlen():
    hit = _zahlen_cache.get("z")
    if hit and time.time() - hit[0] < 3600: return hit[1]
    c = db(); by_year = {}; by_decade = {}; celex = set(); total_changes = 0
    for r in c.execute("SELECT aenderung, titel, kundmachung, tgroup FROM laws WHERE tgroup!='me'"):
        m = law_metrics(r["aenderung"], r["titel"], r["kundmachung"])
        total_changes += m["n_changes"]; celex.update(m["celex"])
        for y in m["years"]: by_year[y] = by_year.get(y, 0) + 1
        if m["stf_year"]: d = m["stf_year"] // 10 * 10; by_decade[d] = by_decade.get(d, 0) + 1
    cols = "gnr, kurztitel, abk, typ, tgroup, n_norms, n_changes, n_celex, stf_year, last_year"
    top = lambda order, where="1": [dict(r) for r in c.execute(f"SELECT {cols} FROM laws WHERE tgroup!='me' AND {where} ORDER BY {order} LIMIT 15")]
    m = {r["k"]: r["v"] for r in c.execute("SELECT * FROM meta")}
    res = {"laws": c.execute("SELECT COUNT(*) FROM laws WHERE tgroup!='me'").fetchone()[0], "norms": int(m.get("total_norms") or 0),
           "changes": total_changes, "celex": len(celex),
           "laws_with_eu": c.execute("SELECT COUNT(*) FROM laws WHERE n_celex>0").fetchone()[0],
           "by_year": sorted([[y, n] for y, n in by_year.items() if 1945 <= y <= datetime.date.today().year]),
           "by_decade": sorted([[d, n] for d, n in by_decade.items() if d >= 1800]),
           "top_changes": top("COALESCE(n_changes,0) DESC"), "top_old": top("stf_year", "stf_year IS NOT NULL"),
           "top_norms": top("COALESCE(n_norms,0) DESC"), "top_eu": top("COALESCE(n_celex,0) DESC"),
           }
    _zahlen_cache["z"] = (time.time(), res)
    return res

def r_zahlen(h):
    c = db(); res = dict(zahlen())
    gold = c.execute("SELECT * FROM contributions WHERE gold=1 AND removed_at IS NULL ORDER BY id DESC LIMIT 100").fetchall()
    st = contribution_stats(c, gold)
    res.update({"proposals": c.execute("SELECT COUNT(*) FROM contributions WHERE type='prop' AND removed_at IS NULL").fetchone()[0],
                "repeals": c.execute("SELECT COUNT(*) FROM contributions WHERE op='repeal' AND removed_at IS NULL").fetchone()[0],
                "packages": c.execute("SELECT COUNT(*) FROM packages WHERE removed_at IS NULL").fetchone()[0],
                "gold": serialize(sorted(gold, key=lambda r: -st[r["id"]]["score"]), st, all_users(c))})
    return res

# ---------- Begutachtungsentwürfe (Parlament, offene Daten) ----------
# Entwürfe werden wie Rechtsvorschriften gespeichert: Pseudo-Gesetzesnummer 9GGNNNNN (GG = Gesetzgebungsperiode,
# NNNNN = Nummer des Ministerialentwurfs), tgroup 'me', Normen 'text' (Gesetzestext) und 'erl' (Erläuterungen).
from html.parser import HTMLParser
PARL = "https://www.parlament.gv.at"
DRAFT_GP = os.environ.get("OR_DRAFT_GP", "XXVIII")
def _roman(r):
    v = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}; t = 0
    for i, ch in enumerate(r):
        n = v[ch]; t += -n if i + 1 < len(r) and v[r[i + 1]] > n else n
    return t
def draft_gnr(gp, inr): return f"9{_roman(gp):02d}{int(inr):05d}"
def is_draft(gnr): return str(gnr).startswith("9")

class _P(HTMLParser):
    """Absätze (<p>) eines Word-HTML-Dokuments mit Formatvorlagen-Klasse."""
    def __init__(self):
        super().__init__(convert_charrefs=True); self.out = []; self.cur = None; self.depth = 0; self.skip = 0
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("style", "script", "head"): self.skip += 1
        if tag == "p" and self.cur is None: self.cur = [a.get("class") or "", []]
        elif tag == "br" and self.cur is not None: self.cur[1].append(" ")
    def handle_endtag(self, tag):
        if tag in ("style", "script", "head"): self.skip = max(0, self.skip - 1)
        if tag == "p" and self.cur is not None:
            t = re.sub(r"\s+", " ", "".join(self.cur[1])).strip()
            if t: self.out.append((self.cur[0], t))
            self.cur = None
    def handle_data(self, d):
        if self.cur is not None and not self.skip: self.cur[1].append(d)

def parse_draft_html(raw, erl=False):
    """Entwurfstext -> Einheiten wie bei RIS-Normen. Gliederung aus den Formatvorlagen des Bundes (Novellierungsanordnung, §, Absatz, Ziffer)."""
    for enc in ("utf-8", "windows-1252", "latin-1"):
        try: txt = raw.decode(enc); break
        except UnicodeDecodeError: continue
    p = _P(); p.feed(txt)
    units, heads = [], []; art = z = para = ab = zi = None; head = ""
    for cls, t in p.out:
        c = cls.lower()
        if c.startswith("10entwurf") or c.startswith("11titel") or c.startswith("12prom"):
            heads.append({"typ": "titel", "t": t}); continue
        if re.match(r"^4[1-4]ueberschr", c) or (erl and len(t) < 160 and (c.startswith("4") or re.match(r"^(Zu |Allgemeiner Teil|Besonderer Teil|Hauptgesichtspunkte|Kompetenz)", t))):
            ma = re.match(r"^Artikel\s+(\d+[a-z]?)", t)
            if ma: art = "Art. " + ma.group(1); z = para = ab = zi = None
            head = t
            units.append({"a": f"t{len(units)+1}", "n": "", "t": t, "lvl": 0}); continue
        if erl:
            units.append({"a": f"t{len(units)+1}", "n": head[:60], "t": t, "lvl": 1}); continue
        mz = re.match(r"^(\d+[a-z]?)\.\s", t)
        if c.startswith("2") and "novao" in c and mz: z = mz.group(1); para = ab = zi = None
        mp = re.match(r"^„?(§\s*\d+[a-z]*|Art(?:ikel|\.)\s*\d+[a-z]*)\.?\s*(?:\((\d+[a-z]?)\))?", t)
        if mp and not c.startswith("2"): para = re.sub(r"\s+", " ", mp.group(1)).replace("Artikel", "Art."); ab = mp.group(2); zi = None
        ma = re.match(r"^„?\((\d+[a-z]?)\)", t)
        if ma and not c.startswith("2"): ab = ma.group(1); zi = None
        lit = None
        if c.startswith("52aufzaehle1") and mz: zi = mz.group(1)
        ml = re.match(r"^([a-z]{1,2})\)\s", t)
        if ml and not c.startswith("2"): lit = ml.group(1)
        inner = " ".join(x for x in ((para or ""), (f"Abs. {ab}" if ab else ""), (f"Z {zi}" if zi and not c.startswith("2") else ""), (f"lit. {lit}" if lit else "")) if x)
        lbl = " · ".join(x for x in ((art or ""), (f"Pkt. {z}" if z else ""), inner) if x)
        lvl = 0 if c.startswith("2") else 1
        units.append({"a": f"t{len(units)+1}", "n": lbl, "t": t, "lvl": lvl})
    title = next((h["t"] for h in heads if h["typ"] == "titel" and not h["t"].lower().startswith("entwurf")), "")
    return {"heads": heads[:3], "units": units, "title": title}


def _parl_get(path, body=None):
    req = urllib.request.Request(PARL + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"User-Agent": RIS_UA, "Accept": "application/json", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r: return r.read()

def _iso(d):
    m = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", (d or "").strip())
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None

def import_draft(c, row):
    gnr = draft_gnr(row["gp"], row["inr"])
    d = json.loads(_parl_get(f"/gegenstand/{row['gp']}/ME/{row['inr']}?json=TRUE"))["content"]
    docs = {}
    for grp in d.get("documents") or []:
        t = (grp.get("title") or "").lower()
        html_ = next((x["link"] for x in grp.get("documents", []) if x.get("type") == "HTML"), None)
        if not html_: continue
        if "gegenüberstellung" in t: continue
        if ("text" in t or "entwurf" in t) and "text" not in docs: docs["text"] = (html_, "Entwurfstext")
        elif "erläuterung" in t and "erl" not in docs: docs["erl"] = (html_, "Erläuterungen")
    if "text" not in docs: return False
    c.execute("""INSERT INTO laws(gnr,kurztitel,titel,abk,typ,tgroup,idx,kundmachung,inkraft,eli,synced,norms_listed) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                 ON CONFLICT(gnr) DO UPDATE SET kurztitel=excluded.kurztitel,titel=excluded.titel,idx=excluded.idx,eli=excluded.eli,synced=excluded.synced""",
              (gnr, row["title"], d.get("description") or row["title"], f"{row['inr']}/ME", "Ministerialentwurf", "me", row["ministry"],
               None, row["einlangen"], PARL + f"/gegenstand/{row['gp']}/ME/{row['inr']}", now(), now()))
    n = 0
    for i, (pid, (link, label)) in enumerate(sorted(docs.items(), key=lambda x: x[0] != "text")):
        body = parse_draft_html(_parl_get(link), erl=(pid == "erl"))
        if not body["title"]: body["title"] = label
        nor = f"ME{gnr}{pid}"
        c.execute("""INSERT INTO norms(nor,gnr,pid,label,sort,inkraft,xml_url,title,body,fetched,kundmachung) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(nor) DO UPDATE SET body=excluded.body,title=excluded.title,fetched=excluded.fetched""",
                  (nor, gnr, pid, label, i, row["einlangen"], PARL + link, body["title"], json.dumps(body, ensure_ascii=False), now(), ""))
        n += 1
    c.execute("UPDATE laws SET n_norms=? WHERE gnr=?", (n, gnr)); c.commit()
    return True

def sync_drafts():
    """Liste der Ministerialentwürfe der laufenden Gesetzgebungsperiode; laufende Begutachtungen importieren."""
    c = sqlite3.connect(DB_PATH, timeout=30); c.row_factory = sqlite3.Row
    d = json.loads(_parl_get("/Filter/api/filter/data/81?js=eval&showAll=true", {"GP_CODE": [DRAFT_GP]}))
    h = [x["label"] for x in d["header"]]; new = 0
    for r in (dict(zip(h, x)) for x in d["rows"]):
        if r.get("ITYP") != "ME": continue
        gnr = draft_gnr(r["Gesetzgebungsperiode"], r["INR"])
        row = {"gp": r["Gesetzgebungsperiode"], "inr": r["INR"], "title": r["Betreff"], "ressort": r.get("Ressort"),
               "ministry": r.get("Ministerium"), "frist": _iso(r.get("Frist")), "einlangen": _iso(r.get("Einlangen")),
               "active": 1 if r.get("Begutachtung aktiv?") == "J" else 0, "statements": r.get("Stellungnahmen") or 0}
        c.execute("""INSERT INTO drafts(gnr,gp,inr,title,ressort,ministry,frist,einlangen,url,active,statements,synced) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(gnr) DO UPDATE SET title=excluded.title,frist=excluded.frist,active=excluded.active,statements=excluded.statements,synced=excluded.synced""",
                  (gnr, row["gp"], row["inr"], row["title"], row["ressort"], row["ministry"], row["frist"], row["einlangen"],
                   PARL + r["uri"], row["active"], row["statements"], now()))
        has = c.execute("SELECT 1 FROM norms WHERE gnr=? LIMIT 1", (gnr,)).fetchone()
        if row["active"] and not has:
            try:
                if import_draft(c, row): new += 1
            except Exception as e: sys.stderr.write("Entwurf %s: %r\n" % (gnr, e))
            time.sleep(1)
    c.execute("INSERT OR REPLACE INTO meta VALUES('drafts_synced',?)", (str(now()),)); c.commit(); c.close()
    sys.stderr.write(f"Begutachtungsentwürfe: {new} neu importiert\n")

def draft_view(c, r):
    nc = c.execute("SELECT COUNT(*) FROM contributions WHERE key LIKE ? AND removed_at IS NULL", (r["gnr"] + "/%",)).fetchone()[0]
    imported = bool(c.execute("SELECT 1 FROM norms WHERE gnr=? LIMIT 1", (r["gnr"],)).fetchone())
    return {"gnr": r["gnr"], "nr": f"{r['inr']}/ME", "gp": r["gp"], "title": r["title"], "ressort": r["ressort"], "ministry": r["ministry"],
            "deadline": r["frist"], "received": r["einlangen"], "active": bool(r["active"]), "statements_parliament": r["statements"],
            "parliament_url": r["url"], "contributions": nc, "commentable": imported, "url": f"{PUBLIC_URL}/#/g/{r['gnr']}" if imported else None}

def list_drafts(active_only=True):
    c = db()
    rows = c.execute("SELECT * FROM drafts" + (" WHERE active=1" if active_only else "") + " ORDER BY frist" + ("" if active_only else " DESC")).fetchall()
    return {"drafts": [draft_view(c, r) for r in rows if r["active"] or c.execute("SELECT 1 FROM norms WHERE gnr=? LIMIT 1", (r["gnr"],)).fetchone()],
            "synced": int((c.execute("SELECT v FROM meta WHERE k='drafts_synced'").fetchone() or [0])[0])}

def r_drafts(h): return list_drafts(h.query.get("all") != "1")

# ---------- Folgen und Benachrichtigungen ----------
FOLLOW_KINDS = {"law": "Rechtsvorschrift", "c": "Beitrag", "pkg": "Reformpaket"}
def _follow_snapshot(c, kind, ref):
    if kind == "law":
        r = c.execute("SELECT n_changes FROM laws WHERE gnr=?", (ref,)).fetchone(); need(r, 404, "Rechtsvorschrift nicht gefunden.")
        return r["n_changes"] or 0, None
    if kind == "c":
        r = c.execute("SELECT * FROM contributions WHERE id=? AND removed_at IS NULL", (int(ref),)).fetchone(); need(r, 404, "Beitrag nicht gefunden.")
        return None, contribution_stats(c, [r])[r["id"]]["status"]
    r = c.execute("SELECT id FROM packages WHERE id=? AND removed_at IS NULL", (int(ref),)).fetchone(); need(r, 404, "Paket nicht gefunden.")
    return None, package_stats(c, r["id"])["status"]

def r_follow(h):
    u = h.session_user(); need(u, 401, "Bitte anmelden, um zu folgen.")
    d = h.body(); kind = d.get("kind"); ref = str(d.get("ref") or "")
    need(kind in FOLLOW_KINDS and re.fullmatch(r"\d{1,9}", ref), 400, "Ungültige Angabe.")
    c = db()
    if d.get("on", True):
        need(c.execute("SELECT COUNT(*) FROM follows WHERE user_id=?", (u["id"],)).fetchone()[0] < 500, 429, "Höchstens 500 gefolgte Inhalte.")
        cnt, st = _follow_snapshot(c, kind, ref)
        c.execute("INSERT OR IGNORE INTO follows VALUES(?,?,?,?,?,?)", (u["id"], kind, ref, now(), cnt, st))
    else:
        c.execute("DELETE FROM follows WHERE user_id=? AND kind=? AND ref=?", (u["id"], kind, ref))
    c.commit(); return {"ok": True}

def r_follows(h):
    u = h.session_user(); need(u, 401, "Bitte anmelden.")
    c = db(); out = []
    for f in c.execute("SELECT * FROM follows WHERE user_id=? ORDER BY created DESC", (u["id"],)):
        if f["kind"] == "law":
            l = c.execute("SELECT kurztitel, abk FROM laws WHERE gnr=?", (f["ref"],)).fetchone()
            label, url = (l["kurztitel"] if l else f["ref"]), f"#/g/{f['ref']}"
        elif f["kind"] == "c":
            r = c.execute("SELECT key FROM contributions WHERE id=?", (int(f["ref"]),)).fetchone()
            label, url = (f"Beitrag zu {key_label(c, r['key'])}" if r else "Gelöschter Beitrag"), (f"#/g/{r['key']}" if r else "#/")
        else:
            r = c.execute("SELECT title FROM packages WHERE id=?", (int(f["ref"]),)).fetchone()
            label, url = (f"Paket „{r['title']}“" if r else "Gelöschtes Paket"), f"#/paket/{f['ref']}"
        out.append({"kind": f["kind"], "ref": f["ref"], "label": label, "url": url, "since": f["created"]})
    return {"follows": out, "notify": u["notify"] or "daily"}

def r_notify(h):
    u = h.session_user(); need(u, 401, "Bitte anmelden.")
    v = h.body().get("notify"); need(v in ("daily", "weekly", "off"), 400, "daily, weekly oder off.")
    db().execute("UPDATE users SET notify=? WHERE id=?", (v, u["id"])); db().commit(); return {"ok": True}

def _secret():
    return db().execute("SELECT v FROM meta WHERE k='secret'").fetchone()[0]
def unsubscribe_token(uid):
    import hmac
    return hmac.new(_secret().encode(), f"unsub:{uid}".encode(), "sha256").hexdigest()[:32]

def digest_for(c, u, since):
    """Neuigkeiten zu den gefolgten Inhalten eines Kontos seit 'since'. Gibt (Abschnitte, Aktualisierungen) zurück."""
    parts, updates = [], []
    for f in c.execute("SELECT * FROM follows WHERE user_id=?", (u["id"],)).fetchall():
        since_f = max(since, f["created"])
        if f["kind"] == "law":
            l = c.execute("SELECT * FROM laws WHERE gnr=?", (f["ref"],)).fetchone()
            if not l: continue
            lines = []
            new = c.execute("""SELECT * FROM contributions WHERE key LIKE ? AND created>? AND removed_at IS NULL AND user_id!=? ORDER BY id""",
                            (f["ref"] + "/%", since_f, u["id"])).fetchall()
            if new:
                np = sum(1 for r in new if r["type"] == "prop")
                lines.append((f"{len(new)} neue Beiträge" if len(new) > 1 else "1 neuer Beitrag") + (f", davon {np} {'Vorschlag' if np == 1 else 'Vorschläge'}:" if np else ":"))
                for r in new[:5]:
                    lines.append(f"  · {key_label(c, r['key'])}: " + ("Streichung" if r["op"] == "repeal" else (r["text"] or "")[:110].replace("\n", " ")))
                if len(new) > 5: lines.append(f"  … und {len(new) - 5} weitere")
            if (l["n_changes"] or 0) > (f["seen_count"] or 0) and not is_draft(f["ref"]):
                lines.append(f"Neue Fassung im RIS: zuletzt geändert durch {last_change(l['aenderung']) or 'eine neue Kundmachung'}.")
                updates.append(("UPDATE follows SET seen_count=? WHERE user_id=? AND kind='law' AND ref=?", (l["n_changes"], u["id"], f["ref"])))
            if is_draft(f["ref"]):
                dr = c.execute("SELECT frist FROM drafts WHERE gnr=?", (f["ref"],)).fetchone()
                if dr and dr["frist"]:
                    left = (datetime.date.fromisoformat(dr["frist"]) - datetime.date.today()).days
                    if 0 <= left <= 3 and f["seen_status"] != "frist":
                        lines.append(f"Die Begutachtungsfrist endet am {fmt_de(dr['frist'])}.")
                        updates.append(("UPDATE follows SET seen_status='frist' WHERE user_id=? AND kind='law' AND ref=?", (u["id"], f["ref"])))
            if lines: parts.append((f"{l['kurztitel']}", f"{PUBLIC_URL}/#/g/{f['ref']}", lines))
        elif f["kind"] == "c":
            r = c.execute("SELECT * FROM contributions WHERE id=?", (int(f["ref"]),)).fetchone()
            if not r: continue
            lines = []
            if r["removed_at"] and f["seen_status"] != "Entfernt":
                lines.append(f"Von der Moderation entfernt: {REMOVE_REASONS.get(r['removed_reason'], '')}.")
                updates.append(("UPDATE follows SET seen_status='Entfernt' WHERE user_id=? AND kind='c' AND ref=?", (u["id"], f["ref"])))
            elif not r["removed_at"]:
                st = contribution_stats(c, [r])[r["id"]]
                if st["status"] != f["seen_status"] and st["status"] != "Neu":
                    lines.append(f"Neuer Status: {st['status']} ({round(st['share'] * 100)} % Zustimmung, {st['total']} Stimmen).")
                    updates.append(("UPDATE follows SET seen_status=? WHERE user_id=? AND kind='c' AND ref=?", (st["status"], u["id"], f["ref"])))
                rival = c.execute("SELECT COUNT(*) FROM contributions WHERE key=? AND type='prop' AND id!=? AND created>? AND removed_at IS NULL",
                                  (r["key"], r["id"], since_f)).fetchone()[0]
                if rival and r["type"] == "prop": lines.append(f"{rival} neue konkurrierende Formulierung(en) an derselben Stelle.")
            if lines: parts.append((f"Beitrag zu {key_label(c, r['key'])}", f"{PUBLIC_URL}/#/g/{r['key']}", lines))
        else:
            p = c.execute("SELECT * FROM packages WHERE id=?", (int(f["ref"]),)).fetchone()
            if not p: continue
            lines = []
            if p["updated"] > since_f and p["user_id"] != u["id"]: lines.append("Das Paket wurde geändert.")
            st = package_stats(c, p["id"])["status"]
            if st != f["seen_status"] and st != "Neu":
                lines.append(f"Neuer Status: {st}.")
                updates.append(("UPDATE follows SET seen_status=? WHERE user_id=? AND kind='pkg' AND ref=?", (st, u["id"], f["ref"])))
            if lines: parts.append((f"Reformpaket „{p['title']}“", f"{PUBLIC_URL}/#/paket/{p['id']}", lines))
    return parts, updates

def fmt_de(iso_d):
    try: return datetime.date.fromisoformat(iso_d).strftime("%d.%m.%Y")
    except Exception: return iso_d or ""

def send_digests(force_user=None):
    """Tägliche (oder wöchentliche) Zusammenfassung an alle, die etwas folgen. Nur wenn es Neues gibt."""
    if not os.environ.get("OR_SMTP_HOST"): return 0
    c = sqlite3.connect(DB_PATH, timeout=30); c.row_factory = sqlite3.Row
    _local.db = c
    sent = 0
    try:
        q = "SELECT * FROM users WHERE status='active' AND role!='bot' AND notify IN ('daily','weekly') AND id IN (SELECT user_id FROM follows)"
        for u in c.execute(q).fetchall():
            if force_user and u["id"] != force_user: continue
            gap = 86400 if u["notify"] == "daily" else 7 * 86400
            last = u["last_digest"] or 0
            if not force_user and now() - last < gap - 3600: continue
            parts, updates = digest_for(c, u, last or (now() - gap))
            for sql, args in updates: c.execute(sql, args)
            c.execute("UPDATE users SET last_digest=? WHERE id=?", (now(), u["id"])); c.commit()
            if not parts: continue
            body = [f"Guten Morgen, {u['name']}!", "", "Neues zu den Inhalten, denen Sie auf Offenes Recht folgen:", ""]
            for title, url, lines in parts:
                body += [title, url] + lines + [""]
            body += ["Einstellungen und gefolgte Inhalte: " + PUBLIC_URL + "/#/  → Mein Konto",
                     "Keine solchen E-Mails mehr: " + f"{PUBLIC_URL}/abmelden?u={u['id']}&t={unsubscribe_token(u['id'])}"]
            try:
                send_mail(u["email"], f"Offenes Recht: Neues zu {len(parts)} gefolgten Inhalt{'en' if len(parts) > 1 else ''}", "\n".join(body)); sent += 1
            except Exception as e: sys.stderr.write("Zusammenfassung an Konto %s: %r\n" % (u["id"], e))
    finally:
        _local.db = None; c.close()
    if sent: sys.stderr.write(f"Zusammenfassungen versandt: {sent}\n")
    return sent

def scheduler():
    """Hintergrund: Begutachtungsentwürfe alle 6 Stunden, Zusammenfassungen täglich ab 7 Uhr."""
    time.sleep(20)
    while True:
        try:
            c = db()
            last = c.execute("SELECT v FROM meta WHERE k='drafts_synced'").fetchone()
            if not last or now() - int(last[0]) > 6 * 3600: sync_drafts()
        except Exception as e: sys.stderr.write("Entwürfe: %r\n" % e)
        try:
            if 7 <= time.localtime().tm_hour < 22: send_digests()
        except Exception as e: sys.stderr.write("Zusammenfassungen: %r\n" % e)
        time.sleep(1800)

# ---------- RSS ----------
def rss(title, link, items):
    from xml.sax.saxutils import escape as x
    out = ['<?xml version="1.0" encoding="utf-8"?>', '<rss version="2.0"><channel>', f"<title>{x(title)}</title>", f"<link>{x(link)}</link>",
           "<description>Beiträge auf Offenes Recht</description>", "<language>de-at</language>"]
    for it in items:
        out.append(f"<item><title>{x(it['title'])}</title><link>{x(it['link'])}</link><guid isPermaLink=\"false\">{x(it['guid'])}</guid>"
                   f"<pubDate>{email.utils.formatdate(it['ts'], usegmt=True)}</pubDate><description>{x(it['text'])}</description></item>")
    out.append("</channel></rss>")
    return "\n".join(out).encode()

def feed(path):
    c = db(); m = re.fullmatch(r"/feed/(?:gesetz/(\d{8})|neu)\.xml", path)
    need(m, 404, "Unbekannter Feed.")
    gnr = m.group(1)
    if gnr:
        l = c.execute("SELECT kurztitel FROM laws WHERE gnr=?", (gnr,)).fetchone(); need(l, 404, "Rechtsvorschrift nicht gefunden.")
        rows = c.execute("SELECT * FROM contributions WHERE key LIKE ? AND removed_at IS NULL ORDER BY id DESC LIMIT 50", (gnr + "/%",)).fetchall()
        title, link = f"Offenes Recht: {l['kurztitel']}", f"{PUBLIC_URL}/#/g/{gnr}"
    else:
        rows = c.execute("SELECT * FROM contributions WHERE removed_at IS NULL ORDER BY id DESC LIMIT 50").fetchall()
        title, link = "Offenes Recht: neue Beiträge", PUBLIC_URL
    users = all_users(c)
    items = [{"title": f"{'Streichung' if r['op'] == 'repeal' else 'Vorschlag' if r['type'] == 'prop' else 'Kommentar'}: {key_label(c, r['key'])}",
              "link": web_url(r["key"]), "guid": f"or-{r['id']}", "ts": r["created"],
              "text": ((users.get(r["user_id"]) or {}).get("name") or "") + ": " + ((r["text"] or "") if r["op"] != "repeal" else "Ersatzlos streichen. " + (r["why"] or ""))[:1000]}
             for r in rows]
    return rss(title, link, items)

def unsubscribe_page(q):
    import hmac
    uid = q.get("u", ""); t = q.get("t", "")
    ok = uid.isdigit() and hmac.compare_digest(t, unsubscribe_token(int(uid)))
    if ok: db().execute("UPDATE users SET notify='off' WHERE id=?", (int(uid),)); db().commit()
    msg = "Sie erhalten keine Benachrichtigungen mehr. Unter „Mein Konto“ können Sie sie jederzeit wieder einschalten." if ok else "Der Link ist ungültig."
    return (f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Offenes Recht</title>'
            f'<body style="font-family:system-ui,sans-serif;max-width:560px;margin:60px auto;padding:0 16px;color:#1A1F2B;background:#F5F6F8">'
            f'<h1 style="font-size:22px">Benachrichtigungen</h1><p>{msg}</p><p><a href="/">Zur Startseite</a></p></body>').encode()

# ---------- Teilen: Vorschauseiten für soziale Medien ----------
# Die App nutzt Hash-Adressen (#/…), die Vorschau-Crawler nicht sehen. /v/<id>, /p/<id> und /e/<gnr> liefern
# Open-Graph-Angaben und leiten Menschen sofort zur App weiter.
_og_png = None
def og_png():
    global _og_png
    if _og_png is None:
        import base64
        try: _og_png = base64.b64decode(json.load(open(os.path.join(HERE, "seed.json"), encoding="utf-8")).get("og_png", ""))
        except Exception: _og_png = b""
    return _og_png

def _short(t, n):
    t = re.sub(r"\s+", " ", t or "").strip()
    return t if len(t) <= n else t[:n - 1].rsplit(" ", 1)[0] + " …"

def share_page(path):
    from html import escape as x
    c = db(); m = re.fullmatch(r"/(v|p|e)/(\d{1,9})", path); need(m, 404, "Nicht gefunden.")
    kind, ref = m.group(1), m.group(2)
    if kind == "v":
        r = c.execute("SELECT * FROM contributions WHERE id=? AND removed_at IS NULL", (int(ref),)).fetchone(); need(r, 404, "Beitrag nicht gefunden.")
        st = contribution_stats(c, [r])[r["id"]]; where = key_label(c, r["key"])
        if r["type"] == "com": title, desc = f"Kommentar zu {where}", r["text"]
        elif r["op"] == "repeal": title, desc = f"Streichen: {where}", "Vorschlag: ersatzlos streichen. " + (r["why"] or "")
        else:
            title = f"Besserer Wortlaut für {where}"
            desc = (amend_for(c, r) or ("Neuer Wortlaut: " + r["text"])) + " Begründung: " + (r["why"] or "")
        if st["total"]: desc = f"{round(st['share'] * 100)} % Zustimmung bei {st['total']} Stimmen. " + desc
        target = f"/#/g/{r['key']}"
    elif kind == "p":
        p = c.execute("SELECT * FROM packages WHERE id=? AND removed_at IS NULL", (int(ref),)).fetchone(); need(p, 404, "Paket nicht gefunden.")
        n = c.execute("SELECT COUNT(*) FROM package_items WHERE package_id=?", (p["id"],)).fetchone()[0]
        st = package_stats(c, p["id"])
        title = f"Reformpaket: {p['title']}"
        desc = f"{n} Vorschläge mit fertigem Gesetzesentwurf. " + (f"{round(st['share'] * 100)} % Zustimmung bei {st['total']} Stimmen. " if st["total"] else "") + p["summary"]
        target = f"/#/paket/{p['id']}"
    else:
        need(re.fullmatch(r"\d{8}", ref), 404, "Nicht gefunden.")
        l = c.execute("SELECT * FROM laws WHERE gnr=?", (ref,)).fetchone(); need(l, 404, "Nicht gefunden.")
        nc = c.execute("SELECT COUNT(*) FROM contributions WHERE key LIKE ? AND removed_at IS NULL", (ref + "/%",)).fetchone()[0]
        if is_draft(ref):
            d = c.execute("SELECT * FROM drafts WHERE gnr=?", (ref,)).fetchone()
            title = f"Entwurf {l['abk']}: {l['kurztitel']}"
            desc = (f"In Begutachtung bis {fmt_de(d['frist'])}. " if d and d["frist"] else "") + f"Absatz für Absatz kommentieren und Stellung nehmen. {nc} {'Beitrag' if nc == 1 else 'Beiträge'} bisher."
        else:
            title = l["kurztitel"]
            desc = f"{l['n_norms'] or ''} Normen, {l['n_changes'] or 0} Änderungen seit der Stammfassung. Absatz für Absatz kommentieren und verbessern. {nc} {'Beitrag' if nc == 1 else 'Beiträge'} bisher."
        target = f"/#/g/{ref}"
    title, desc = _short(title, 110), _short(desc, 280)
    url = PUBLIC_URL + path
    return (f'<!doctype html><html lang="de"><head><meta charset="utf-8"><title>{x(title)} · Offenes Recht</title>'
            f'<meta name="description" content="{x(desc)}"><link rel="canonical" href="{x(PUBLIC_URL + target)}">'
            f'<meta property="og:type" content="article"><meta property="og:site_name" content="Offenes Recht">'
            f'<meta property="og:title" content="{x(title)}"><meta property="og:description" content="{x(desc)}"><meta property="og:url" content="{x(url)}">'
            f'<meta property="og:image" content="{PUBLIC_URL}/og.png"><meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">'
            f'<meta name="twitter:card" content="summary_large_image"><meta name="twitter:title" content="{x(title)}"><meta name="twitter:description" content="{x(desc)}">'
            f'<meta http-equiv="refresh" content="0;url={x(target)}"></head><body><p><a href="{x(target)}">{x(title)}</a></p>'
            f'<script>location.replace({json.dumps(target)})</script></body></html>').encode()

# ---------- Schutz der Bewertungen ----------
# Ohne ID Austria lassen sich Mehrfachkonten nicht ausschließen. Die App speichert keine IP-Adressen; erkannt
# werden daher nur Muster in Konten und Stimmen. Auffälligkeiten gehen an das Gremium; ausgesetzte Stimmen
# zählen nicht, bleiben aber gespeichert und können wieder gezählt werden. Jede Entscheidung wird protokolliert.
def anomalies(c):
    import hashlib
    out = []
    users = {r["id"]: r for r in c.execute("SELECT id, name, created, rating_hold FROM users WHERE role='citizen' AND status='active'")}
    def fid(kind, ids): return hashlib.sha1((kind + ":" + ",".join(map(str, sorted(ids)))).encode()).hexdigest()[:12]
    def acc(ids): return [{"id": i, "name": users[i]["name"], "created": users[i]["created"], "held": bool(users[i]["rating_hold"])} for i in sorted(ids) if i in users]
    # 1. Viele neue Bürgerkonten in kurzer Zeit
    recent = sorted((u["created"], i) for i, u in users.items() if u["created"] > now() - 30 * 86400)
    j = 0
    for k in range(len(recent)):
        while recent[k][0] - recent[j][0] > 3600: j += 1
        win = [i for _, i in recent[j:k + 1]]
        if len(win) >= 8 and (k + 1 == len(recent) or recent[k + 1][0] - recent[j][0] > 3600):
            out.append({"id": fid("reg", win), "kind": "Registrierungswelle", "accounts": acc(win), "items": [],
                        "detail": f"{len(win)} Bürgerkonten innerhalb einer Stunde angelegt."})
    # 2. Stimmenwelle neuer Konten auf einen Beitrag (gleiche Konten über mehrere Beiträge werden zusammengefasst)
    bursts = {}
    for (cid,) in c.execute("SELECT cid FROM ratings GROUP BY cid HAVING COUNT(*) >= 10").fetchall():
        rs = c.execute("SELECT user_id, v, created FROM ratings WHERE cid=? ORDER BY created", (cid,)).fetchall()
        j = 0
        for k in range(len(rs)):
            while rs[k]["created"] - rs[j]["created"] > 3600: j += 1
            win = rs[j:k + 1]
            young = [r for r in win if r["user_id"] in users and r["created"] - users[r["user_id"]]["created"] < 3 * 86400]
            if len(win) >= 10 and len(young) >= 0.7 * len(win):
                b = bursts.setdefault(frozenset(r["user_id"] for r in young), {"items": [], "same": True, "n": 0})
                b["items"].append(cid); b["n"] = max(b["n"], len(win)); b["same"] = b["same"] and len({r["v"] for r in young}) == 1
                break
    for accs, b in bursts.items():
        out.append({"id": fid("burst", accs), "kind": "Stimmenwelle", "accounts": acc(accs), "items": b["items"],
                    "detail": f"Bis zu {b['n']} Stimmen innerhalb einer Stunde auf {len(b['items'])} {'Beitrag' if len(b['items']) == 1 else 'Beiträge'}, "
                              f"überwiegend von {len(accs)} Konten, die jünger als drei Tage sind" + (", alle gleich." if b["same"] else ".")})
    # 3. Konten, die fast nur eine Person unterstützen
    authors = {r["id"]: r["user_id"] for r in c.execute("SELECT id, user_id FROM contributions")}
    fans = {}
    for uid in users:
        rs = c.execute("SELECT cid, v FROM ratings WHERE user_id=?", (uid,)).fetchall()
        if len(rs) < 5: continue
        by = {}
        for r in rs: by.setdefault(authors.get(r["cid"]), []).append(r["v"])
        top, vs = max(by.items(), key=lambda kv: len(kv[1]))
        if top and len(vs) >= 0.9 * len(rs) and len(set(vs)) == 1:
            fans.setdefault((top, vs[0]), []).append(uid)
    for (author, v), ids in fans.items():
        if len(ids) >= 3:
            name = (c.execute("SELECT name FROM users WHERE id=?", (author,)).fetchone() or ["?"])[0]
            out.append({"id": fid(f"fan{author}{v}", ids), "kind": "Einseitige Unterstützung", "accounts": acc(ids), "items": [],
                        "detail": f"{len(ids)} Konten stimmen fast ausschließlich über Beiträge von „{name}“ ab, und zwar immer gleich ({ {'h': 'Ja', 't': 'Teilweise', 'n': 'Nein'}[v] })."})
    # 4. Gleichförmig abstimmende Gruppen
    votes = {}
    for r in c.execute("SELECT user_id, cid, v FROM ratings"): votes.setdefault(r["user_id"], {})[r["cid"]] = r["v"]
    ids = [u for u in votes if u in users and len(votes[u]) >= 5]
    parent = {u: u for u in ids}
    def find(a):
        while parent[a] != a: parent[a] = parent[parent[a]]; a = parent[a]
        return a
    for a in range(len(ids)):
        for b in range(a + 1, len(ids)):
            va, vb = votes[ids[a]], votes[ids[b]]
            common = set(va) & set(vb)
            if len(common) >= 5 and all(va[x] == vb[x] for x in common) and len(common) >= 0.8 * min(len(va), len(vb)):
                parent[find(ids[a])] = find(ids[b])
    groups = {}
    for u in ids: groups.setdefault(find(u), []).append(u)
    for g in groups.values():
        if len(g) >= 3:
            out.append({"id": fid("bloc", g), "kind": "Gleichförmige Gruppe", "accounts": acc(g), "items": [],
                        "detail": f"{len(g)} Konten haben bei mindestens fünf gemeinsamen Beiträgen immer gleich abgestimmt."})
    reviewed = {r["fid"]: r for r in c.execute("SELECT * FROM anomaly_reviews")}
    for a in out:
        r = reviewed.get(a["id"])
        a["review"] = {"decision": r["decision"], "at": r["at"], "note": r["note"]} if r else None
    return out

def r_g_anomalies(h):
    require_gremium(h); c = db()
    held = c.execute("SELECT COUNT(*) FROM users WHERE rating_hold=1").fetchone()[0]
    return {"findings": anomalies(c), "held_accounts": held}

def r_g_hold(h):
    """Stimmen von Konten aussetzen oder wieder zählen; mit Begründung, protokolliert."""
    g = require_gremium(h); d = h.body(); c = db()
    ids = [int(x) for x in (d.get("user_ids") or [])][:500]; on = bool(d.get("hold")); note = (d.get("note") or "").strip()[:500]
    fid_ = (d.get("finding") or "")[:20]
    need(ids or fid_, 400, "Keine Konten angegeben.")
    need(len(note) >= 10, 400, "Bitte die Entscheidung kurz begründen; sie wird protokolliert.")
    if ids:
        q = ",".join("?" * len(ids))
        need(g["id"] not in ids, 403, "Über das eigene Konto entscheidet ein anderes Gremiumsmitglied.")
        c.execute(f"UPDATE users SET rating_hold=? WHERE id IN ({q}) AND role='citizen'", [1 if on else 0] + ids)
        n = c.execute(f"SELECT COUNT(*) FROM ratings WHERE user_id IN ({q})", ids).fetchone()[0]
        log(g["id"], "moderation:" + ("hold" if on else "unhold"), None,
            f"Stimmen von {len(ids)} Konten {'ausgesetzt' if on else 'wieder gezählt'} ({n} Bewertungen) · {note}")
    if fid_:
        c.execute("INSERT OR REPLACE INTO anomaly_reviews VALUES(?,?,?,?,?)", (fid_, "held" if (ids and on) else "ok", g["id"], now(), note))
        if not ids: log(g["id"], "moderation:anomaly-ok", None, f"Auffälligkeit {fid_} als unbedenklich eingestuft · {note}")
    c.commit(); return {"ok": True}

# ---------- Mein Konto: Aktivitäten, Profil, Datenexport ----------
def r_activity(h):
    """Alles, was ein Konto auf der Plattform getan hat, und was sich daran seit dem letzten Besuch geändert hat."""
    u = h.session_user(); need(u, 401, "Bitte anmelden.")
    c = db(); users = all_users(c); uid = u["id"]
    own = c.execute("SELECT * FROM contributions WHERE user_id=? ORDER BY id DESC", (uid,)).fetchall()
    # Statistik je Stelle braucht die Konkurrenz an derselben Stelle (Paarvergleich)
    keys = list({r["key"] for r in own})
    rows = []
    for i in range(0, len(keys), 400):
        q = ",".join("?" * len(keys[i:i + 400]))
        rows += c.execute(f"SELECT * FROM contributions WHERE key IN ({q})", keys[i:i + 400]).fetchall()
    st = contribution_stats(c, rows) if rows else {}
    mine = serialize(own, {r["id"]: st.get(r["id"]) or contribution_stats(c, [r])[r["id"]] for r in own}, users)
    for m in mine:   # konkurrierende Vorschläge anderer an derselben Stelle
        m["rivals"] = sum(1 for r in rows if r["key"] == m["key"] and r["type"] == "prop" and r["user_id"] != uid and not r["removed_at"])
    pk = c.execute("SELECT * FROM packages WHERE user_id=? ORDER BY updated DESC", (uid,)).fetchall()
    packages = [package_view(c, p, users) for p in pk]
    rated = c.execute("""SELECT r.v, r.created rated_at, c.* FROM ratings r JOIN contributions c ON c.id=r.cid
                         WHERE r.user_id=? ORDER BY r.created DESC LIMIT 300""", (uid,)).fetchall()
    rst = contribution_stats(c, rated) if rated else {}
    ratings = [{"v": r["v"], "at": r["rated_at"], "c": serialize([r], {r["id"]: rst[r["id"]]}, users)[0]} for r in rated]
    prat = [{"v": r["v"], "at": r["created"], "package": package_view(c, p, users)}
            for r in c.execute("SELECT * FROM package_ratings WHERE user_id=? ORDER BY created DESC", (uid,)).fetchall()
            for p in [c.execute("SELECT * FROM packages WHERE id=?", (r["package_id"],)).fetchone()] if p]
    since = u["last_seen"] or u["created"]
    news, _ = digest_for(c, u, since)
    own_news = []
    nr = c.execute("""SELECT COUNT(*) FROM ratings r JOIN contributions c ON c.id=r.cid WHERE c.user_id=? AND r.created>?
                      AND r.user_id NOT IN (SELECT id FROM users WHERE rating_hold=1)""", (uid, since)).fetchone()[0]
    if nr: own_news.append(f"{nr} {'neue Bewertung' if nr == 1 else 'neue Bewertungen'} Ihrer Beiträge.")
    npr = c.execute("""SELECT COUNT(*) FROM package_ratings r JOIN packages p ON p.id=r.package_id WHERE p.user_id=? AND r.created>?
                       AND r.user_id NOT IN (SELECT id FROM users WHERE rating_hold=1)""", (uid, since)).fetchone()[0]
    if npr: own_news.append(f"{npr} {'neue Bewertung' if npr == 1 else 'neue Bewertungen'} Ihrer Reformpakete.")
    riv = c.execute("""SELECT COUNT(*) FROM contributions o WHERE o.type='prop' AND o.user_id!=? AND o.created>? AND o.removed_at IS NULL
                       AND o.key IN (SELECT key FROM contributions WHERE user_id=? AND type='prop')""", (uid, since, uid)).fetchone()[0]
    if riv: own_news.append(f"{riv} {'neuer konkurrierender Vorschlag' if riv == 1 else 'neue konkurrierende Vorschläge'} an Stellen, zu denen Sie etwas vorgeschlagen haben.")
    rem = c.execute("SELECT COUNT(*) FROM contributions WHERE user_id=? AND removed_at>?", (uid, since)).fetchone()[0]
    if rem: own_news.append(f"{rem} {'Beitrag wurde' if rem == 1 else 'Beiträge wurden'} von der Moderation entfernt (siehe Beiträge, Filter „Entfernt“).")
    reports = c.execute("SELECT COUNT(*) FROM reports WHERE user_id=?", (uid,)).fetchone()[0]
    props = [m for m in mine if m["type"] == "prop" and not m.get("removed")]
    voted = [m for m in props if m["stats"]["total"]]
    stats = {"contributions": len(mine), "comments": sum(1 for m in mine if m["type"] == "com"), "proposals": len(props),
             "repeals": sum(1 for m in props if m["op"] == "repeal"), "removed": sum(1 for m in mine if m.get("removed")),
             "packages": len(packages), "ratings": len(ratings) + len(prat), "follows": c.execute("SELECT COUNT(*) FROM follows WHERE user_id=?", (uid,)).fetchone()[0],
             "votes_received": sum(m["stats"]["total"] for m in mine if not m.get("removed")),
             "approval_avg": round(sum(m["stats"]["share"] for m in voted) / len(voted), 4) if voted else None,
             "consensus": sum(1 for m in props if m["stats"]["status"] == "Breite Zustimmung"),
             "laws": len({m["key"].split("/")[0] for m in mine}), "reports": reports,
             "rating_values": {v: sum(1 for r in ratings if r["v"] == v) for v in "htn"}}
    laws = {}
    for m in mine:
        g = m["key"].split("/")[0]; laws.setdefault(g, 0); laws[g] += 1
    top_laws = [{"gnr": g, "name": (c.execute("SELECT abk, kurztitel FROM laws WHERE gnr=?", (g,)).fetchone() or {"abk": g, "kurztitel": g})["kurztitel"], "n": n}
                for g, n in sorted(laws.items(), key=lambda x: -x[1])[:8]]
    first = min([u["created"]] + [m["created"] for m in mine])
    return {"stats": stats, "contributions": mine, "packages": packages, "ratings": ratings, "package_ratings": prat,
            "news": [{"title": t, "url": url.replace(PUBLIC_URL, ""), "lines": l} for t, url, l in news], "own_news": own_news,
            "since": since, "member_since": u["created"], "first_activity": first, "top_laws": top_laws}

def r_seen(h):
    u = h.session_user(); need(u, 401, "Bitte anmelden.")
    db().execute("UPDATE users SET last_seen=? WHERE id=?", (now(), u["id"])); db().commit(); return {"ok": True}

def r_rename(h):
    u = h.session_user(); need(u, 401, "Bitte anmelden.")
    need(u["role"] != "bot", 403, "Den Namen eines Bots ändert das Zulassungsgremium, weil er Teil der Zulassung ist.")
    name = re.sub(r"\s+", " ", (h.body().get("name") or "")).strip()
    need(2 <= len(name) <= 80, 400, "Name: 2 bis 80 Zeichen.")
    if name == u["name"]: return {"ok": True}
    db().execute("UPDATE users SET name=? WHERE id=?", (name, u["id"]))
    log(u["id"], "account:rename", u["id"], f"{u['name']} → {name}"); db().commit()
    return {"ok": True}

def r_export(h):
    """Auskunft nach Art. 15 DSGVO: alle zum Konto gespeicherten Daten als JSON (ohne Passwort-Hash und Schlüssel)."""
    u = h.session_user(); need(u, 401, "Bitte anmelden.")
    c = db(); uid = u["id"]
    rows = lambda sql: [dict(r) for r in c.execute(sql, (uid,)).fetchall()]
    acc = {k: u[k] for k in u.keys() if k not in ("pw_hash", "api_key_hash", "api_key_once")}
    data = {"exportiert": iso(now()), "hinweis": "Alle Daten, die Offenes Recht zu Ihrem Konto speichert. Zeiten in Sekunden seit 1.1.1970 (UTC).",
            "konto": acc, "beitraege": rows("SELECT * FROM contributions WHERE user_id=?"),
            "bewertungen": rows("SELECT cid, v, created FROM ratings WHERE user_id=?"),
            "paarvergleiche": rows("SELECT a, b, winner, created FROM pairwise WHERE user_id=?"),
            "pakete": rows("SELECT * FROM packages WHERE user_id=?"),
            "paket_inhalte": rows("SELECT i.* FROM package_items i JOIN packages p ON p.id=i.package_id WHERE p.user_id=?"),
            "paket_bewertungen": rows("SELECT package_id, v, created FROM package_ratings WHERE user_id=?"),
            "gefolgt": rows("SELECT kind, ref, created FROM follows WHERE user_id=?"),
            "meldungen": rows("SELECT cid, reason, note, created, status FROM reports WHERE user_id=?"),
            "protokoll": rows("SELECT action, target, note, created FROM audit WHERE actor=? OR (target=? AND action LIKE 'gremium:%')".replace("target=?", "target=" + str(uid)))}
    return data

# ---------- Suche nach Änderungsgesetzen (Sammelgesetze) und Erwähnungen ----------
# Viele Gesetze heißen nur im Bundesgesetzblatt so (z. B. „Nachhaltigkeitsberichtsgesetz“, BGBl. I Nr. 6/2026):
# Es sind Sammelnovellen, die bestehende Gesetze ändern und eventuell neue erlassen. Im konsolidierten Recht gibt es
# sie nicht als eigene Rechtsvorschrift. Diese Suche findet sie im BGBl und zeigt, welche Rechtsvorschriften sie betreffen.
_acts_cache = {}
_BGBL_Q = re.compile(r"(?:BGBl\.?\s*)?(I{1,3})?\s*(?:Nr\.?\s*)?(\d{1,4})\s*/\s*(\d{4})", re.I)

def _laws_by_bgbl(c, bgbl):
    m = re.search(r"Nr\.\s*(\d+)/(\d{4})", bgbl); teil = re.search(r"BGBl\.\s*(I{1,3})\s", bgbl)
    if not m: return []
    pat = re.compile(r"BGBl\.\s*" + (re.escape(teil.group(1)) + r"\s*" if teil else "") + r"Nr\.\s*" + m.group(1) + "/" + m.group(2) + r"(?!\d)")
    like = f"%Nr. {m.group(1)}/{m.group(2)}%"
    out = []
    for r in c.execute("""SELECT gnr, kurztitel, abk, typ, aenderung, titel FROM laws WHERE tgroup!='me' AND (aenderung LIKE ? OR titel LIKE ?)""", (like, like)):
        stf = re.search(r"StF:\s*([^·(]+)", r["titel"] or "")
        new = bool(stf and pat.search(stf.group(1)))
        if new or pat.search(r["aenderung"] or ""):
            out.append({"gnr": r["gnr"], "name": r["kurztitel"], "abk": r["abk"], "new": new})
    return sorted(out, key=lambda x: (not x["new"], x["name"]))

def search_acts(q):
    q = re.sub(r"\s+", " ", (q or "")).strip()
    if len(q) < 4: return {"acts": [], "mentions": []}
    hit = _acts_cache.get(q.lower())
    if hit and time.time() - hit[0] < 3600: return hit[1]
    c = db(); acts = []
    m = _BGBL_Q.fullmatch(q)
    if m and ("/" in q):
        bg = f"BGBl. {m.group(1).upper() if m.group(1) else 'I'} Nr. {int(m.group(2))}/{m.group(3)}"
        acts.append({"title": bg, "short": None, "bgbl": bg, "date": None, "laws": _laws_by_bgbl(c, bg)})
    else:
        try:
            _, docs = ris_search({"Applikation": "BgblAuth", "Titel": q, "DokumenteProSeite": "Ten"})
        except Exception: docs = []
        for d in docs[:8]:
            try:
                b = d["Data"]["Metadaten"]["Bundesrecht"]; a = b.get("BgblAuth") or {}
                bg = a.get("Bgblnummer")
                if not bg: continue
                acts.append({"title": b.get("Titel") or "", "short": b.get("Kurztitel"), "bgbl": bg, "date": a.get("Ausgabedatum"),
                             "type": a.get("Typ"), "laws": _laws_by_bgbl(c, bg)})
            except Exception: continue
    mentions = []
    if not m:
        try:
            _, docs = ris_search({"Suchworte": f'"{q}"', "DokumenteProSeite": "Twenty"})
            seen = set()
            for d in docs:
                try: x = _meta(d)
                except Exception: continue
                if x["gnr"] in seen: continue
                seen.add(x["gnr"]); mentions.append({"gnr": x["gnr"], "name": x["kurztitel"], "abk": x["abk"]})
        except Exception: pass
    known = {l["gnr"] for a in acts for l in a["laws"]}
    res = {"acts": acts, "mentions": [x for x in mentions if x["gnr"] not in known][:12]}
    _acts_cache[q.lower()] = (time.time(), res)
    return res

def r_acts(h): return search_acts(h.query.get("q", ""))

# ---------- Statistik (nur Gremium) ----------
# Aufrufe werden anonym gezählt: nur Tageszahlen je Kennzahl, keine IP-Adressen, keine Cookies, keine Kennungen.
# Ein „Besuch“ ist die erste Seite in einem Browser-Tab (sessionStorage im Browser, nichts davon wird übertragen
# außer dem Hinweis „neu“). Alle übrigen Zahlen kommen rückwirkend aus der Datenbank.
try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("Europe/Vienna")
except Exception:
    _TZ = None
def _today(ts=None):
    d = datetime.datetime.fromtimestamp(ts if ts is not None else time.time(), _TZ) if _TZ else datetime.datetime.fromtimestamp(ts or time.time())
    return d.date()

HIT_PAGES = {"debatte", "home", "law", "draft", "paket", "zahlen", "projekt", "api", "moderation", "konto", "impressum", "datenschutz", "statistik", "other"}
def bump(metric, n=1):
    try:
        c = db()
        c.execute("INSERT INTO stats_daily(day,metric,n) VALUES(?,?,?) ON CONFLICT(day,metric) DO UPDATE SET n=n+excluded.n",
                  (_today().isoformat(), metric[:60], n))
        c.commit()
    except Exception as e:
        sys.stderr.write("Statistik: %r\n" % e)

def r_hit(h):
    d = h.body(); p = d.get("p") if d.get("p") in HIT_PAGES else "other"
    bump("views"); bump("views:" + p)
    if d.get("new"): bump("visits")
    g = str(d.get("g") or "")
    if re.fullmatch(r"\d{8}", g): bump("law:" + g)
    return {"ok": True}

def _ts_start(day):
    dt = datetime.datetime.combine(day, datetime.time.min)
    return int(dt.replace(tzinfo=_TZ).timestamp()) if _TZ else int(time.mktime(dt.timetuple()))

def r_g_stats(h):
    require_gremium(h); c = db()
    today = _today()
    periods = {"heute": today, "7": today - datetime.timedelta(days=6), "30": today - datetime.timedelta(days=29),
               "365": today - datetime.timedelta(days=364), "gesamt": datetime.date(2000, 1, 1)}
    def counts(since_day):
        t = _ts_start(since_day); d = since_day.isoformat()
        one = lambda q, *a: c.execute(q, a).fetchone()[0]
        daily = {r["metric"]: r["n"] for r in c.execute("SELECT metric, SUM(n) n FROM stats_daily WHERE day>=? GROUP BY metric", (d,))}
        roles = {r["role"]: r["n"] for r in c.execute("SELECT role, COUNT(*) n FROM users WHERE created>=? AND role!='deleted' GROUP BY role", (t,))}
        contrib = c.execute("""SELECT SUM(type='com') com, SUM(type='prop' AND COALESCE(op,'')!='repeal') prop, SUM(op='repeal') rep,
                               SUM(via='api') api, COUNT(*) n FROM contributions WHERE created>=?""", (t,)).fetchone()
        active = one("""SELECT COUNT(DISTINCT u) FROM (SELECT user_id u FROM contributions WHERE created>=? UNION SELECT user_id FROM ratings WHERE created>=?
                        UNION SELECT user_id FROM pairwise WHERE created>=? UNION SELECT user_id FROM package_ratings WHERE created>=?)""", t, t, t, t)
        return {"visits": daily.get("visits", 0), "views": daily.get("views", 0), "api": daily.get("api", 0), "mcp": daily.get("mcp", 0),
                "share": daily.get("share", 0), "rss": daily.get("rss", 0),
                "accounts": sum(roles.values()), "citizens": roles.get("citizen", 0), "orgs": roles.get("org", 0), "bots": roles.get("bot", 0),
                "active": active, "contributions": contrib["n"] or 0, "comments": contrib["com"] or 0, "proposals": contrib["prop"] or 0,
                "repeals": contrib["rep"] or 0, "via_api": contrib["api"] or 0,
                "ratings": one("SELECT COUNT(*) FROM ratings WHERE created>=?", t) + one("SELECT COUNT(*) FROM package_ratings WHERE created>=?", t),
                "pairs": one("SELECT COUNT(*) FROM pairwise WHERE created>=?", t),
                "packages": one("SELECT COUNT(*) FROM packages WHERE created>=?", t),
                "reports": one("SELECT COUNT(*) FROM reports WHERE created>=?", t),
                "follows": one("SELECT COUNT(*) FROM follows WHERE created>=?", t),
                "pages": {k.split(":", 1)[1]: v for k, v in daily.items() if k.startswith("views:")}}
    def series(days):
        out = []
        for i in range(days - 1, -1, -1):
            day = today - datetime.timedelta(days=i); t0, t1 = _ts_start(day), _ts_start(day + datetime.timedelta(days=1))
            m = {r["metric"]: r["n"] for r in c.execute("SELECT metric, n FROM stats_daily WHERE day=? AND metric IN ('visits','views')", (day.isoformat(),))}
            out.append({"d": day.isoformat(), "visits": m.get("visits", 0), "views": m.get("views", 0),
                        "contributions": c.execute("SELECT COUNT(*) FROM contributions WHERE created>=? AND created<?", (t0, t1)).fetchone()[0],
                        "ratings": c.execute("SELECT COUNT(*) FROM ratings WHERE created>=? AND created<?", (t0, t1)).fetchone()[0],
                        "accounts": c.execute("SELECT COUNT(*) FROM users WHERE created>=? AND created<? AND role!='deleted'", (t0, t1)).fetchone()[0]})
        return out
    def months(n):
        out = []; y, m = today.year, today.month
        keys = []
        for _ in range(n):
            keys.append((y, m)); m -= 1
            if m == 0: y, m = y - 1, 12
        for y, m in reversed(keys):
            a = datetime.date(y, m, 1); b = datetime.date(y + (m == 12), m % 12 + 1, 1)
            t0, t1 = _ts_start(a), _ts_start(b)
            v = {r["metric"]: r["n"] for r in c.execute("SELECT metric, SUM(n) n FROM stats_daily WHERE day>=? AND day<? AND metric IN ('visits','views') GROUP BY metric", (a.isoformat(), b.isoformat()))}
            out.append({"d": f"{y}-{m:02d}", "visits": v.get("visits", 0), "views": v.get("views", 0),
                        "contributions": c.execute("SELECT COUNT(*) FROM contributions WHERE created>=? AND created<?", (t0, t1)).fetchone()[0],
                        "ratings": c.execute("SELECT COUNT(*) FROM ratings WHERE created>=? AND created<?", (t0, t1)).fetchone()[0],
                        "accounts": c.execute("SELECT COUNT(*) FROM users WHERE created>=? AND created<? AND role!='deleted'", (t0, t1)).fetchone()[0]})
        return out
    d30 = periods["30"].isoformat(); t30 = _ts_start(periods["30"])
    viewed = [{"gnr": r["metric"][4:], "n": r["n"]} for r in c.execute(
        "SELECT metric, SUM(n) n FROM stats_daily WHERE day>=? AND metric LIKE 'law:%' GROUP BY metric ORDER BY n DESC LIMIT 15", (d30,))]
    active_laws = [{"gnr": r["g"], "n": r["n"]} for r in c.execute(
        "SELECT substr(key,1,8) g, COUNT(*) n FROM contributions WHERE created>=? GROUP BY g ORDER BY n DESC LIMIT 15", (t30,))]
    for lst in (viewed, active_laws):
        for x in lst:
            l = c.execute("SELECT kurztitel, abk, tgroup FROM laws WHERE gnr=?", (x["gnr"],)).fetchone()
            x["name"] = (l["abk"] or l["kurztitel"]) if l else x["gnr"]; x["draft"] = bool(l and l["tgroup"] == "me")
    totals = {"accounts": {r["role"]: r["n"] for r in c.execute("SELECT role, COUNT(*) n FROM users WHERE role!='deleted' GROUP BY role")},
              "contributions": c.execute("SELECT COUNT(*) FROM contributions WHERE removed_at IS NULL").fetchone()[0],
              "removed": c.execute("SELECT COUNT(*) FROM contributions WHERE removed_at IS NOT NULL").fetchone()[0],
              "ratings": c.execute("SELECT COUNT(*) FROM ratings").fetchone()[0],
              "packages": c.execute("SELECT COUNT(*) FROM packages WHERE removed_at IS NULL").fetchone()[0],
              "follows": c.execute("SELECT COUNT(*) FROM follows").fetchone()[0],
              "first_count": (c.execute("SELECT MIN(day) FROM stats_daily").fetchone() or [None])[0]}
    return {"periods": {k: counts(v) for k, v in periods.items()}, "daily": series(30), "monthly": months(12),
            "viewed_laws": viewed, "active_laws": active_laws, "totals": totals, "today": today.isoformat()}

# ---------- Reformpakete ----------
PKG_MAX = 100
def _pkg_items(c, pid):
    return c.execute("""SELECT c.* FROM package_items i JOIN contributions c ON c.id=i.cid WHERE i.package_id=? ORDER BY i.pos""", (pid,)).fetchall()

def _check_items(c, ids):
    need(isinstance(ids, list) and 1 <= len(ids) <= PKG_MAX, 400, f"Ein Paket enthält 1 bis {PKG_MAX} Vorschläge.")
    ids = list(dict.fromkeys(int(x) for x in ids))
    q = ",".join("?" * len(ids))
    rows = {r["id"]: r for r in c.execute(f"SELECT * FROM contributions WHERE id IN ({q})", ids)}
    for i in ids:
        r = rows.get(i)
        need(r and r["type"] == "prop" and not r["removed_at"], 400, f"Beitrag {i} ist kein verfügbarer Änderungsvorschlag.")
        need(not is_draft(r["key"][:8]), 400, "Vorschläge zu Begutachtungsentwürfen gehören in die Stellungnahme, nicht in ein Reformpaket.")
    keys = [rows[i]["key"] for i in ids]
    for a in range(len(keys)):
        for b in range(a + 1, len(keys)):
            ka, kb = keys[a], keys[b]; pa, pb = ka.split("/"), kb.split("/")
            same_law = pa[0] == pb[0]
            clash = ka == kb or (same_law and "gesetz" in (pa[1], pb[1])) or \
                    (same_law and pa[1] == pb[1] and "ganz" in (pa[2], pb[2]))
            need(not clash, 400, f"Zwei Vorschläge betreffen dieselbe Stelle ({key_label(c, ka)}" + ("" if ka == kb else f" und {key_label(c, kb)}")
                 + "). Ein Paket braucht pro Stelle genau einen Vorschlag.")
    return ids

def package_stats(c, pid):
    agg = {"h": 0, "t": 0, "n": 0}
    for r in c.execute("SELECT v, COUNT(*) n FROM package_ratings WHERE package_id=? AND user_id NOT IN (SELECT id FROM users WHERE rating_hold=1) GROUP BY v", (pid,)): agg[r["v"]] = r["n"]
    tot = sum(agg.values()); pos = agg["h"] + .5 * agg["t"]; lo, hi = wilson(pos, tot)
    status = "Neu" if tot < 10 else "Breite Zustimmung" if lo >= .55 else "Überwiegend abgelehnt" if hi < .45 else "Offen"
    return {**agg, "total": tot, "share": round(pos / tot, 4) if tot else 0, "lower": round(lo, 4), "upper": round(hi, 4), "status": status, "score": round(lo, 4)}

def package_view(c, p, users, full=False):
    items = _pkg_items(c, p["id"])
    live = [r for r in items if not r["removed_at"]]
    gnrs = {r["key"].split("/")[0] for r in live}
    laws = sorted(x["abk"] or x["kurztitel"] for x in (c.execute("SELECT abk, kurztitel FROM laws WHERE gnr=?", (g,)).fetchone() for g in gnrs) if x)
    out = {"id": p["id"], "title": p["title"], "summary": p["summary"], "author": users.get(p["user_id"]), "created": p["created"],
           "updated": p["updated"], "n": len(live), "n_repeal": sum(1 for r in live if r["op"] == "repeal"),
           "laws": [x for x in laws if x], "stats": package_stats(c, p["id"])}
    if p["removed_at"]:
        out.update({"title": "", "summary": "", "author": None, "n": 0, "laws": [],
                    "removed": {"at": p["removed_at"], "reason": REMOVE_REASONS.get(p["removed_reason"], p["removed_reason"]), "note": p["removed_note"]}})
        return out
    if full:
        st = contribution_stats(c, items)
        out["items"] = serialize(items, st, users)
        out["draft"] = draft_bill(c, live, title=p["title"])
        out["avg_item_share"] = round(sum(st[r["id"]]["share"] for r in live) / len(live), 4) if live else 0
    return out

def r_packages(h):
    c = db(); users = all_users(c)
    rows = c.execute("SELECT * FROM packages WHERE removed_at IS NULL ORDER BY updated DESC").fetchall()
    u = h.session_user()
    return {"packages": [package_view(c, p, users) for p in rows],
            "mine": {str(r["package_id"]): r["v"] for r in c.execute("SELECT package_id, v FROM package_ratings WHERE user_id=?", (u["id"],))} if u else {}}

def r_package(h):
    c = db(); pid = int(h.query.get("id") or 0)
    p = c.execute("SELECT * FROM packages WHERE id=?", (pid,)).fetchone(); need(p, 404, "Paket nicht gefunden.")
    return package_view(c, p, all_users(c), full=True)

def _pkg_text(d):
    title = (d.get("title") or "").strip(); summary = (d.get("summary") or "").strip()
    need(5 <= len(title) <= 140, 400, "Titel: 5 bis 140 Zeichen.")
    need(20 <= len(summary) <= 4000, 400, "Bitte das Paket in mindestens einem Satz beschreiben (20 bis 4.000 Zeichen).")
    return title, summary

def create_package(u, d, via="web"):
    need(u["status"] == "active", 403, "Ihr Konto ist nicht aktiv.")
    c = db(); title, summary = _pkg_text(d); ids = _check_items(c, d.get("items") or [])
    need(c.execute("SELECT COUNT(*) FROM packages WHERE user_id=? AND created>?", (u["id"], now() - 86400)).fetchone()[0] < 20, 429,
         "Höchstens 20 neue Pakete pro Tag.")
    cur = c.execute("INSERT INTO packages(title,summary,user_id,created,updated) VALUES(?,?,?,?,?)", (title, summary, u["id"], now(), now()))
    pid = cur.lastrowid
    c.executemany("INSERT INTO package_items VALUES(?,?,?)", [(pid, i, n) for n, i in enumerate(ids)])
    log(u["id"], "package-create" + (":api" if via == "api" else ""), pid, title[:120]); c.commit()
    return pid

def r_package_create(h):
    u = h.session_user(); need(u, 401, "Bitte anmelden, um ein Paket zu schnüren.")
    return {"id": create_package(u, h.body())}

def r_package_update(h):
    u = h.session_user(); need(u, 401, "Bitte anmelden.")
    d = h.body(); c = db(); pid = int(d.get("id") or 0)
    p = c.execute("SELECT * FROM packages WHERE id=? AND removed_at IS NULL", (pid,)).fetchone(); need(p, 404, "Paket nicht gefunden.")
    need(p["user_id"] == u["id"], 403, "Nur wer das Paket geschnürt hat, kann es ändern.")
    if d.get("delete"):
        c.execute("DELETE FROM package_items WHERE package_id=?", (pid,)); c.execute("DELETE FROM package_ratings WHERE package_id=?", (pid,))
        c.execute("DELETE FROM packages WHERE id=?", (pid,)); log(u["id"], "package-delete", pid); c.commit(); return {"ok": True}
    title, summary = _pkg_text({"title": d.get("title", p["title"]), "summary": d.get("summary", p["summary"])})
    if "items" in d:
        ids = _check_items(c, d["items"])
        old = [r["cid"] for r in c.execute("SELECT cid FROM package_items WHERE package_id=? ORDER BY pos", (pid,))]
        if ids != old:
            c.execute("DELETE FROM package_items WHERE package_id=?", (pid,))
            c.executemany("INSERT INTO package_items VALUES(?,?,?)", [(pid, i, n) for n, i in enumerate(ids)])
            if set(ids) != set(old):   # Inhalt geändert: bisherige Bewertungen gelten nicht mehr
                c.execute("DELETE FROM package_ratings WHERE package_id=?", (pid,))
    c.execute("UPDATE packages SET title=?, summary=?, updated=? WHERE id=?", (title, summary, now(), pid)); c.commit()
    return {"ok": True}

def r_package_add(h):
    """Einen Vorschlag zu einem eigenen Paket hinzufügen (Bewertungen des Pakets werden dabei zurückgesetzt)."""
    u = h.session_user(); need(u, 401, "Bitte anmelden.")
    d = h.body(); c = db(); pid = int(d.get("id") or 0); cid = int(d.get("cid") or 0)
    p = c.execute("SELECT * FROM packages WHERE id=? AND removed_at IS NULL", (pid,)).fetchone(); need(p, 404, "Paket nicht gefunden.")
    need(p["user_id"] == u["id"], 403, "Nur wer das Paket geschnürt hat, kann es ändern.")
    old = [r["cid"] for r in c.execute("SELECT cid FROM package_items WHERE package_id=? ORDER BY pos", (pid,))]
    need(cid not in old, 409, "Der Vorschlag ist bereits in diesem Paket.")
    ids = _check_items(c, old + [cid])
    c.execute("INSERT INTO package_items VALUES(?,?,?)", (pid, cid, len(ids) - 1))
    c.execute("DELETE FROM package_ratings WHERE package_id=?", (pid,))
    c.execute("UPDATE packages SET updated=? WHERE id=?", (now(), pid)); c.commit()
    return {"ok": True}

def r_package_rate(h):
    u = h.session_user(); need(u, 401, "Bitte anmelden, um zu bewerten.")
    need(u["role"] == "citizen" and u["status"] == "active", 403, "Bewerten dürfen nur Bürgerinnen und Bürger.")
    d = h.body(); c = db(); pid = int(d.get("id") or 0); v = d.get("v")
    p = c.execute("SELECT user_id FROM packages WHERE id=? AND removed_at IS NULL", (pid,)).fetchone(); need(p, 404, "Paket nicht gefunden.")
    need(p["user_id"] != u["id"], 403, "Eigene Pakete sind nicht bewertbar.")
    if v is None: c.execute("DELETE FROM package_ratings WHERE user_id=? AND package_id=?", (u["id"], pid))
    else:
        need(v in RATE_VALUES, 400, "Ungültige Bewertung.")
        c.execute("INSERT OR REPLACE INTO package_ratings VALUES(?,?,?,?)", (u["id"], pid, v, now()))
    c.commit(); return {"ok": True}

def r_draft(h):
    """Entwurf als Text für einen einzelnen Vorschlag (?cid=) oder ein Paket (?package=)."""
    c = db()
    if h.query.get("package"):
        p = c.execute("SELECT * FROM packages WHERE id=? AND removed_at IS NULL", (int(h.query["package"]),)).fetchone(); need(p, 404, "Paket nicht gefunden.")
        rows = [r for r in _pkg_items(c, p["id"]) if not r["removed_at"]]; title = p["title"]
    else:
        r = c.execute("SELECT * FROM contributions WHERE id=? AND type='prop' AND removed_at IS NULL", (int(h.query.get("cid") or 0),)).fetchone()
        need(r, 404, "Vorschlag nicht gefunden."); rows = [r]; title = None
    return {"text": draft_bill(c, rows, title=title)}

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
    ("GET", "/api/packages"): r_packages, ("GET", "/api/package"): r_package, ("POST", "/api/packages"): r_package_create,
    ("POST", "/api/package/update"): r_package_update, ("POST", "/api/package/add"): r_package_add, ("POST", "/api/package/rate"): r_package_rate,
    ("GET", "/api/draft"): r_draft, ("GET", "/api/drafts"): r_drafts,
    ("GET", "/api/gremium/anomalies"): r_g_anomalies, ("POST", "/api/gremium/hold"): r_g_hold,
    ("GET", "/api/acts"): r_acts, ("POST", "/api/hit"): r_hit, ("GET", "/api/gremium/stats"): r_g_stats,
    ("GET", "/api/account/activity"): r_activity, ("GET", "/api/debate"): r_debate, ("POST", "/api/argrate"): r_argrate, ("POST", "/api/annotate"): r_annotate, ("POST", "/api/account/seen"): r_seen,
    ("POST", "/api/account/name"): r_rename, ("GET", "/api/account/export"): r_export,
    ("POST", "/api/follow"): r_follow, ("GET", "/api/follows"): r_follows, ("POST", "/api/account/notify"): r_notify, ("GET", "/api/references"): r_references, ("GET", "/api/zahlen"): r_zahlen,
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
            threading.Thread(target=scheduler, daemon=True).start()
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
