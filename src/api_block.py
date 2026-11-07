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
    rows = c.execute(f"""SELECT l.*, (SELECT COUNT(*) FROM contributions WHERE key LIKE l.gnr||'/%') nc FROM laws l {w}
                         ORDER BY {order} LIMIT ? OFFSET ?""", args + oargs + [limit, offset]).fetchall()
    return {"total": total, "offset": offset, "results": [api_law_brief(r, r["nc"]) for r in rows]}

def api_get_law(gnr):
    law = law_or_404(gnr); c = db()
    prefetch_law(gnr)
    counts = {}
    for r in c.execute("SELECT key FROM contributions WHERE key LIKE ?", (gnr + "/%",)):
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
    for x in db().execute("SELECT key FROM contributions WHERE key LIKE ?", (f"{gnr}/{r['pid']}/%",)):
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
    return c.execute("SELECT * FROM contributions" + ((" WHERE " + " AND ".join(where)) if where else "") + " ORDER BY id", args).fetchall()

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
    rows = c.execute("SELECT * FROM contributions WHERE key=?", (r["key"],)).fetchall()
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

