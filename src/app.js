// ---- Grundlagen
const ROLE_NAMES = {citizen:"Bürgerin/Bürger", org:"Organisation", bot:"KI-Bot"};
const GROUPS = [["", "Alle"], ["g","Gesetze"], ["v","Verordnungen"], ["vertrag","Staatsverträge"], ["k","Kundmachungen"]];
const RATE = {
  prop: {q:"Fördert diese Änderung Wachstum und Wohlstand für alle?", opts:[["h","Ja"],["t","Teilweise"],["n","Nein"]]},
  com:  {q:"Hilft dieser Hinweis, die Regel zu verbessern?", opts:[["h","Hilfreich"],["t","Teilweise"],["n","Nicht hilfreich"]]}
};
const PILOT = "10007517";
// Impressum (Offenlegung nach § 25 MedienG) – Angaben des Medieninhabers
const LEGAL = {name:"Christoph Pramhofer", ort:"Wien", mail:"christoph.pramhofer@neos.eu", stand:"6. November 2026"};

let S = { user:null, mode:"home", panel:"default", page:null, resetToken:null,
  cat:{q:"", g:"", idx:"", sort:"", rows:[], total:0, stats:null, offset:0}, sach:null,
  gnr:null, law:null, norms:[], pid:null, texts:{}, sel:null, w:null, tocq:"",
  data:[], myRatings:{}, myPairs:new Set(), all:null,
  showBots:true, filter:"all", compare:false, formType:"com", gremium:null, bots:null, authMode:"login", regRole:"citizen" };

const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const pct = x => Math.round(x*100);
const nf = n => Number(n||0).toLocaleString("de-AT");
const fmtDate = t => new Date(t*1000).toLocaleDateString("de-AT", {day:"2-digit", month:"2-digit", year:"numeric"});
const fmtIso = d => d ? d.split("-").reverse().join(".") : "";
const statusCls = s => ({"Breite Zustimmung":"s-konsens","Überwiegend abgelehnt":"s-umstritten","Offen":"s-diskutiert","Neu":"s-neu"}[s]);
const $ = id => document.getElementById(id);
const narrow = () => window.matchMedia("(max-width:1180px)").matches;

async function api(path, body){
  const opt = body === undefined ? {credentials:"same-origin"} :
    {method:"POST", credentials:"same-origin", headers:{"Content-Type":"application/json","X-OR":"1"}, body:JSON.stringify(body)};
  let r;
  try { r = await fetch(path, opt); } catch(e){ throw new Error("Keine Verbindung zum Server."); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ("Fehler "+r.status));
  return j;
}
function toast(msg){ const t = document.createElement("div"); t.className="toast"; t.textContent=msg; document.body.appendChild(t); setTimeout(()=>t.remove(), 2600); }

// ---- Adressen: Schlüssel "gnr/pid/abs2/z1"
function unitLabel(key){
  const [gnr, pid, ...rest] = key.split("/");
  if (pid==="gesetz") return "Gesamte Rechtsvorschrift";
  const n = S.norms.find(x => x.pid===pid);
  if (rest[0]==="ganz") return (n ? n.label : pid) + " (gesamt)";
  let l = n ? n.label : pid;
  rest.forEach(p => {
    let m;
    if ((m = p.match(/^abs(\d+[a-z]?)/))) l += " Abs. "+m[1];
    else if ((m = p.match(/^z(\d+[a-z]?)/))) l += " Z "+m[1];
    else if ((m = p.match(/^lit([a-z]+)/))) l += " lit. "+m[1];
    else if (p.startsWith("s")) l += " Schlussteil";
  });
  return l;
}
function unitOf(key){
  const parts = key.split("/");
  if (parts[1]==="gesetz") return S.law ? {a:"ganz", n:"", t:S.law.kurztitel+" (gesamte Rechtsvorschrift)", whole:"gesetz"} : null;
  const body = S.texts[parts[1]];
  if (!body) return null;
  const a = parts.slice(2).join("/");
  if (a==="ganz") return {a, n:"", t:body.units.map(u => (u.n+" "+u.t).trim()).join(" "), whole:"ganz"};
  return body.units.find(u => u.a===a) || null;
}
const lawKurz = () => S.law ? (S.law.abk || S.law.kurztitel) : "";
const addrOf = (key, w) => "at:bund:"+key.split("/")[0]+"/"+key.split("/").slice(1).join("/") + (w ? "#w"+w.from+"-w"+w.to : "");

// ---- Router
function go(hash){ if (location.hash !== hash) location.hash = hash; else route(); }
async function route(){
  const p = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  S.compare = false;
  if (["impressum","datenschutz","reset","api","projekt","moderation","paket"].includes(p[0])){
    if (p[0]==="moderation"){ try { S.modlog = await api("/api/moderation"); } catch(e){ S.modlog = {removed:[], stats:{}}; } }
    if (p[0]==="paket"){ try { S.pkg = await api("/api/package?id="+(+p[1]||0)); } catch(e){ S.pkg = {error:e.message}; } }
    S.mode = "home"; S.gnr = null; S.law = null; S.page = p[0]; S.resetToken = p[1] || null;
    if (S.panel==="anchor") S.panel = "default";
    if (!S.all) await loadAll().catch(()=>{});
    render();
    const anchor = p[0]==="projekt" && p[1] ? $("p-"+p[1]) : null;
    if (anchor) anchor.scrollIntoView({block:"start"}); else window.scrollTo(0,0);
    return;
  }
  S.page = null;
  if (p[0]==="g" && /^\d{8}$/.test(p[1])){
    S.mode = "law";
    if (S.gnr !== p[1]) await openLaw(p[1]);
    if (!S.law) return;
    const first = (S.norms.find(n => n.label!=="§ 0") || S.norms[0] || {}).pid;
    const pid = p[2]==="gesetz" ? first : (p[2] || first);
    if (pid) await openNorm(pid, p.length > 3 ? p.join("/").replace(/^g\//,"") : null);
    else render();
  } else {
    S.mode = "home"; S.gnr = null; S.law = null; S.panel = S.panel==="anchor" ? "default" : S.panel;
    if (!S.cat.stats) await loadCatalog(true);
    await Promise.all([loadAll(), loadTopLaws()]);
    render();
    if (S.jump){ const el = $(S.jump); S.jump = null; if (el) el.scrollIntoView({block:"start"}); } else window.scrollTo(0,0);
  }
}

// ---- Daten laden
async function loadMe(){ const j = await api("/api/me"); S.user = j.user; }
async function loadCatalog(reset){
  const c = S.cat; if (reset){ c.offset = 0; c.rows = []; }
  const qs = new URLSearchParams({q:c.q, g:c.g, idx:c.idx, sort:c.sort, offset:c.offset, limit:60});
  const j = await api("/api/catalog?"+qs);
  c.total = j.total; c.stats = j.stats; c.rows = reset ? j.rows : c.rows.concat(j.rows);
}
async function loadAll(){
  const [j, k] = await Promise.all([api("/api/state"), api("/api/packages").catch(() => ({packages:[], mine:{}}))]);
  S.all = j.contributions; S.user = j.user; S.myRatings = j.myRatings; S.myPairs = new Set(j.myPairs.map(p => p.join("-")));
  S.pkgs = k.packages; S.myPkgRatings = k.mine;
}
const myPackages = () => (S.pkgs||[]).filter(p => p.author && S.user && p.author.id===S.user.id);
async function loadState(){
  const j = await api("/api/state?gnr="+S.gnr);
  S.user = j.user; S.data = j.contributions; S.myRatings = j.myRatings; S.myPairs = new Set(j.myPairs.map(p => p.join("-")));
}
let pollTimer = null;
async function openLaw(gnr){
  S.gnr = gnr; S.law = null; S.norms = []; S.texts = {}; S.pid = null; S.sel = null; S.w = null; S.tocq = ""; S.panel = "anchor";
  renderShell(); $("law").innerHTML = `<div class="empty">Lade die Rechtsvorschrift aus dem RIS …</div>`; $("toc").innerHTML = ""; $("panel").innerHTML = "";
  try {
    const j = await api("/api/law?gnr="+gnr);
    S.law = j.law; S.norms = j.norms; await loadState();
    schedulePoll(j.loading);
  } catch(e){ $("law").innerHTML = `<div class="empty">${esc(e.message)} <button class="linkbtn" data-home>Zur Übersicht</button></div>`; S.gnr = null; }
}
function schedulePoll(loading){
  clearTimeout(pollTimer);
  if (loading > 0) pollTimer = setTimeout(async () => {
    if (S.mode!=="law" || !S.gnr) return;
    try { const j = await api("/api/law?gnr="+S.gnr); S.norms = j.norms; renderToc(); schedulePoll(j.loading); } catch(e){}
  }, 4000);
}
async function openNorm(pid, selKey){
  S.pid = pid;
  if (!S.texts[pid]){
    $("law").innerHTML = `<div class="empty">Lade ${esc((S.norms.find(n=>n.pid===pid)||{}).label||"")} …</div>`;
    try { S.texts[pid] = await api(`/api/norm?gnr=${S.gnr}&pid=${pid}`); }
    catch(e){ $("law").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  }
  const body = S.texts[pid];
  const first = body.units[0];
  S.sel = selKey && unitOf(selKey) ? selKey : (first ? `${S.gnr}/${pid}/${first.a}` : null);
  if (!selKey) S.w = null;
  if (S.panel!=="anchor" && !["account","gremium","bots","how"].includes(S.panel)) S.panel = "anchor";
  render();
  const el = document.querySelector(`.unit[data-key="${CSS.escape(S.sel||"")}"]`);
  if (selKey && el) el.scrollIntoView({block:"center"}); else window.scrollTo(0,0);
}

// ---- Wort-Diff (LCS auf Wortebene)
function diffSegs(a, b){
  const A = a.split(/\s+/), B = b.split(/\s+/), n = A.length, m = B.length;
  if (n*m > 4e6) return null;
  const L = Array.from({length:n+1}, () => new Uint16Array(m+1));
  for (let i=n-1;i>=0;i--) for (let j=m-1;j>=0;j--) L[i][j] = A[i]===B[j] ? L[i+1][j+1]+1 : Math.max(L[i+1][j], L[i][j+1]);
  const out = []; let i=0, j=0;
  const push = (t, w) => { const last = out[out.length-1]; if (last && last.t===t) last.w.push(w); else out.push({t, w:[w]}); };
  while (i<n && j<m){ if (A[i]===B[j]){ push("=",A[i]); i++; j++; } else if (L[i+1][j] >= L[i][j+1]){ push("-",A[i]); i++; } else { push("+",B[j]); j++; } }
  while (i<n) push("-",A[i++]); while (j<m) push("+",B[j++]);
  return out;
}
function segHtml(s){ return s.t==="=" ? esc(s.w.join(" ")) : s.t==="+" ? "<ins>"+esc(s.w.join(" "))+"</ins>" : "<del>"+esc(s.w.join(" "))+"</del>"; }
function diffWords(a, b){ const o = diffSegs(a, b); return o ? o.map(segHtml).join(" ") : esc(b); }
const isRepeal = c => c.op==="repeal";
function propDiff(c, base, short){
  if (isRepeal(c)){
    const w = (base||"").split(/\s+/), cut = short && w.length > 40 ? w.slice(0, 40).join(" ")+" …" : base;
    return `<del>${esc(cut)}</del> <span class="repeal-note">entfällt ersatzlos</span>`;
  }
  return base ? (short ? diffShort(base, c.text) : diffWords(base, c.text)) : esc(c.text);
}
const propKind = c => isRepeal(c) ? "Streichung" : "Änderungsvorschlag";
// Kurzfassung für Übersichten: unveränderte Passagen auf wenige Wörter Kontext kürzen
function diffShort(a, b, ctx=8){
  const o = diffSegs(a, b); if (!o) return esc(b);
  return o.map((s, k) => {
    if (s.t!=="=") return segHtml(s);
    const w = s.w, first = k===0, last = k===o.length-1;
    if (first && last) return esc(w.join(" "));
    if (first) return w.length > ctx ? "… " + esc(w.slice(-ctx).join(" ")) : esc(w.join(" "));
    if (last) return w.length > ctx ? esc(w.slice(0, ctx).join(" ")) + " …" : esc(w.join(" "));
    return w.length > 2*ctx ? esc(w.slice(0, ctx).join(" ")) + " … " + esc(w.slice(-ctx).join(" ")) : esc(w.join(" "));
  }).join(" ");
}

// ---- Kopfzeile
function renderAcct(){
  const el = $("acct"); const u = S.user;
  if (!u){ el.innerHTML = `<button class="btn ghost" id="loginbtn">Anmelden</button><button class="btn" id="regbtn">Registrieren</button>`; return; }
  const pend = u.role==="bot" && u.status==="pending" ? `<span class="pill pend">wartet auf Zulassung</span>` :
               u.role==="bot" && u.status!=="active" ? `<span class="pill s-umstritten">nicht zugelassen</span>` : "";
  el.innerHTML = `<span class="nm">${esc(u.name)}</span><span class="pill ${u.role==="bot"?"bot":"role"}">${ROLE_NAMES[u.role]}</span>${pend}
    ${u.gremium?`<button class="btn ghost" id="grembtn">Zulassungsgremium</button>`:""}
    <button class="btn ghost" id="acctbtn">Mein Konto</button><button class="btn ghost" id="logoutbtn">Abmelden</button>`;
}
function renderShell(){
  document.body.dataset.mode = S.mode; updateSide();
  $("crumb").innerHTML = S.mode==="law" && S.law ? `<button class="linkbtn" data-home>Alle Rechtsvorschriften</button> › ${esc(lawKurz())}` : "";
}

// ---- Startseite: Rechtsvorschriften | Projekt, darunter Vorschläge
const SIDE_PANELS = ["account","gremium","bots","how","overview"];
function sideOn(){ return S.mode==="law" || SIDE_PANELS.includes(S.panel); }
function updateSide(){ document.body.dataset.side = sideOn() ? "on" : "off"; }
function catFiltering(){ const c = S.cat; return !!(c.q || c.g || c.idx || c.sort || c.browse); }
function lawRow(r){
  return `<li><button data-open-law="${r.gnr}">
    <span class="lt">${esc(r.kurztitel)}${r.abk && r.abk!==r.kurztitel ? ` <span class="abk">${esc(r.abk)}</span>`:""}</span>
    <span class="lm">${esc(r.typ)}${r.idx?" · "+esc(r.idx):""}${r.inkraft?" · seit "+fmtIso(r.inkraft):""}</span>
    <span class="ln">${r.n_norms ? nf(r.n_norms)+" Normen" : ""}${r.nc ? ` <span class="pill s-diskutiert">${r.nc} ${r.nc===1?"Beitrag":"Beiträge"}</span>` : ""}</span>
  </button></li>`;
}
function propCard(c){
  const st = c.stats, base = c.base || "";
  const who = c.author ? (c.author.role==="deleted" ? "Gelöschtes Konto" : c.author.name + (c.author.role==="bot" ? " · KI-Bot" : c.author.role==="org" ? " · Organisation" : "")) : "";
  return `<article class="pcard">
    <div class="chead"><span class="pill ${statusCls(st.status)}">${st.status}</span>${isRepeal(c)?'<span class="pill del">Streichung</span>':''}${c.example?'<span class="vfy">Beispiel</span>':''}</div>
    <a class="paddr" href="#/g/${esc(c.key)}">${esc(c.where || c.key)}</a>
    <div class="diff">${propDiff(c, base, true)}</div>
    ${c.why?`<p class="why clamp"><b>Begründung:</b> ${esc(c.why)}</p>`:""}
    <div class="pmeter">${st.total ? `<span class="bar"><i style="width:${pct(st.share)}%"></i></span><span>${pct(st.share)} % Ja · ${st.total} Stimmen</span>` : `<span>Noch keine Bewertungen</span>`}</div>
    <div class="why">${esc(who)}</div>
  </article>`;
}
function pkgCard(p){
  const st = p.stats, who = p.author ? (p.author.role==="deleted" ? "Gelöschtes Konto" : p.author.name + (p.author.role==="bot" ? " · KI-Bot" : p.author.role==="org" ? " · Organisation" : "")) : "";
  return `<article class="pcard pkcard">
    <div class="chead"><span class="pill ${statusCls(st.status)}">${st.status}</span><span class="pill pkg">Reformpaket</span></div>
    <a class="paddr" href="#/paket/${p.id}">${esc(p.title)}</a>
    <div class="why">${p.n} ${p.n===1?"Vorschlag":"Vorschläge"}${p.n_repeal?`, davon ${p.n_repeal} ${p.n_repeal===1?"Streichung":"Streichungen"}`:""} · ${p.laws.map(esc).join(", ")}</div>
    <p class="why clamp">${esc(p.summary)}</p>
    <div class="pmeter">${st.total ? `<span class="bar"><i style="width:${pct(st.share)}%"></i></span><span>${pct(st.share)} % Ja · ${st.total} Stimmen</span>` : `<span>Noch keine Bewertungen</span>`}</div>
    <div class="why">${esc(who)}</div>
  </article>`;
}
function propModeAuto(){
  // Solange kaum bewertet wurde, sind die neuesten Vorschläge aussagekräftiger als eine Reihung nach Zustimmung
  if (S.propMode) return S.propMode;
  const rated = (S.all||[]).filter(c => c.type==="prop" && !c.removed && c.stats.total >= 5).length;
  return rated >= 3 ? "best" : "new";
}
function homeProposals(){
  const mode = propModeAuto();
  let ps = (S.all||[]).filter(c => c.type==="prop" && !c.removed);
  if (mode==="new") ps.sort((a,b) => b.created - a.created || b.id - a.id);
  else if (mode==="contested") ps = ps.filter(c => c.stats.total >= 5).sort((a,b) => (b.stats.total*(1-Math.abs(b.stats.share-.5)*2)) - (a.stats.total*(1-Math.abs(a.stats.share-.5)*2)));
  else ps.sort((a,b) => b.stats.score - a.stats.score);
  return ps;
}
function renderHome(){
  const c = S.cat, st = c.stats || {groups:{}}, syncing = !st.laws, filtering = catFiltering();
  const props = homeProposals(), shown = S.propsAll ? props : props.slice(0, 6);
  const top = (S.topLaws||[]).filter(r => r.nc > 0).slice(0, 3);
  const html = `<div class="homegrid">
    <section class="hblock" id="home-laws" aria-labelledby="h-laws">
      <div class="lbl">Bundesrecht, geltende Fassung</div>
      <h1 id="h-laws">${syncing ? "Der Katalog wird aus dem RIS geladen …" : `${nf(st.laws)} Rechtsvorschriften. ${nf(st.norms)} Normen.`}</h1>
      <div class="gstats">${GROUPS.slice(1).map(([k,l]) => `<button class="gstat ${c.g===k?"on":""}" data-g="${k}" aria-pressed="${c.g===k}"><b>${nf(st.groups[k])}</b><span>${l}</span></button>`).join("")}</div>
      <div class="filters">
        <input id="catq" type="search" placeholder="Suchen: Titel oder Abkürzung, z. B. ASVG, Mietrecht" value="${esc(c.q)}" aria-label="Rechtsvorschrift suchen">
        <div class="seg" role="group" aria-label="Art">${GROUPS.map(([k,l]) => `<button data-g="${k}" aria-pressed="${c.g===k}">${l}</button>`).join("")}</div>
        <div class="selrow">
          <select id="catsort" aria-label="Sortierung"><option value="">A–Z</option><option value="contrib" ${c.sort==="contrib"?"selected":""}>Meiste Beiträge</option><option value="norms" ${c.sort==="norms"?"selected":""}>Meiste Normen</option><option value="new" ${c.sort==="new"?"selected":""}>Zuletzt in Kraft getreten</option></select>
          <select id="catidx" aria-label="Sachgebiet"><option value="">Alle Sachgebiete</option>${(S.sach||[]).map(x => `<option value="${esc(x.idx)}" ${c.idx===x.idx?"selected":""}>${esc(x.idx)} (${x.n})</option>`).join("")}</select>
        </div>
      </div>
      ${filtering ? `
        <div class="listhead"><span class="why">${syncing ? "Bitte in einer Minute neu laden." : `${nf(c.total)} Treffer${c.idx?` im Sachgebiet ${esc(c.idx)}`:""}`}</span><button class="linkbtn" id="catreset">Zurücksetzen</button></div>
        <ol class="laws">${c.rows.map(lawRow).join("")}</ol>
        ${c.rows.length < c.total ? `<div class="more"><button class="btn ghost" id="more">Weitere ${nf(Math.min(60, c.total-c.rows.length))} laden</button></div>` : ""}`
      : `
        <div class="lbl" style="margin-top:4px">Am meisten kommentiert</div>
        <ol class="laws">${top.length ? top.map(lawRow).join("") : `<li class="why" style="padding:10px 0">Noch keine Beiträge.</li>`}</ol>
        <button class="linkbtn strong" id="catall">Alle ${syncing ? "" : nf(st.laws)+" "}Rechtsvorschriften</button>`}
    </section>
    <section class="hblock about" id="home-about" aria-labelledby="h-about">
      <div class="lbl">Über das Projekt</div>
      <h2 id="h-about">Warum es Offenes Recht gibt</h2>
      <p>Österreich hat mehr als 10.000 geltende Rechtsvorschriften. Kaum jemand kennt sie alle, aber alle müssen sie befolgen: Betriebe, Vereine, Gemeinden, jede und jeder Einzelne. Regeln kommen laufend dazu, aufgeräumt wird selten. So wächst ein Regelwerk, das Zeit, Geld und Vertrauen kostet.</p>
      <p>Das Wissen, wo eine Regel nicht mehr passt, liegt nicht in einem Ministerium. Es liegt bei denen, die mit ihr leben: in der Werkstatt, die an einer überholten Schwelle scheitert, in der Behörde, die eine Frist nicht halten kann, bei der Nachbarin, die verstehen will, was für sie gilt. Offenes Recht sammelt dieses verstreute Wissen Stelle für Stelle und macht sichtbar, welche Verbesserungen breite Zustimmung finden.</p>
      <p>Unabhängig und offen: Jeder Beitrag ist öffentlich, jede Stimme zählt gleich, KI-Bots helfen sichtbar gekennzeichnet mit.</p>
      <h3>So funktioniert es</h3>
      <ol class="steps">
        <li><span class="n">1</span><span><b>Lesen</b><br><span class="why">Rechtsvorschrift wählen, Absatz oder einzelne Wörter markieren</span></span></li>
        <li><span class="n">2</span><span><b>Vorschlagen</b><br><span class="why">Kommentar oder besserer Wortlaut mit Begründung</span></span></li>
        <li><span class="n">3</span><span><b>Bewerten</b><br><span class="why">Fördert es Wachstum und Wohlstand für alle?</span></span></li>
      </ol>
      <div class="row">${S.user ? "" : `<button class="btn" data-open="register">Registrieren</button>`}<a class="btn ghost" href="#/projekt/bewertung">So wird bewertet</a><a class="btn ghost" href="#/projekt">Mehr über das Projekt</a></div>
    </section>
  </div>
  <section class="hprops" id="home-props" aria-labelledby="h-props">
    <div class="hprops-head">
      <h2 id="h-props">${propModeAuto()==="new" ? "Neueste Vorschläge" : propModeAuto()==="contested" ? "Umstrittene Vorschläge" : "Am besten bewertete Vorschläge"}</h2>
      <div class="seg" role="group" aria-label="Reihung">${[["best","Beste"],["new","Neueste"],["contested","Umstritten"]].map(([k,l]) => `<button data-pmode="${k}" aria-pressed="${propModeAuto()===k}">${l}</button>`).join("")}</div>
    </div>
    ${shown.length ? `<div class="pgrid">${shown.map(propCard).join("")}</div>` : `<div class="empty">Noch keine Vorschläge${propModeAuto()==="contested"?" mit genug Stimmen für diese Ansicht":""}.</div>`}
    ${props.length > 6 && !S.propsAll ? `<button class="linkbtn strong" id="propsall">Alle ${props.length} Vorschläge ansehen</button>` : ""}
  </section>
  <section class="hprops" id="home-pkgs" aria-labelledby="h-pkgs">
    <div class="hprops-head"><h2 id="h-pkgs">Reformpakete</h2>
      <span class="why">Zusammengehörige Vorschläge, auch über mehrere Gesetze, mit fertigem Gesetzesentwurf. Pakete schnüren Sie über „Zu Paket“ an einem Vorschlag.</span></div>
    ${(S.pkgs||[]).length ? `<div class="pgrid">${[...S.pkgs].sort((a,b) => b.stats.score - a.stats.score || b.updated - a.updated).slice(0, S.pkgsAll ? 999 : 6).map(pkgCard).join("")}</div>` : `<div class="empty">Noch keine Reformpakete.</div>`}
    ${(S.pkgs||[]).length > 6 && !S.pkgsAll ? `<button class="linkbtn strong" id="pkgsall">Alle ${S.pkgs.length} Pakete ansehen</button>` : ""}
  </section>`;
  $("law").innerHTML = html;
}
async function loadTopLaws(){ try { S.topLaws = (await api("/api/catalog?sort=contrib&limit=3")).rows; } catch(e){ S.topLaws = []; } }

// ---- Gesetzesansicht
function countBy(pid){ return S.data.filter(c => !c.removed && c.key.split("/")[1]===pid).length; }
function renderToc(){
  if (S.mode!=="law" || !S.law){ $("toc").innerHTML = ""; return; }
  const q = S.tocq.trim().toLowerCase();
  const list = S.norms.filter(n => !q || (n.label+" "+(n.title||"")).toLowerCase().includes(q));
  const max = Math.max(1, ...S.norms.map(n => countBy(n.pid)));
  const loading = S.norms.filter(n => !n.loaded).length;
  $("toc").innerHTML = `<div class="lbl">${esc(S.law.typ)} · Gesetzesnummer ${S.law.gnr}</div>
    <div class="tochead">${esc(S.law.kurztitel)}</div>
    <input id="tocq" type="search" placeholder="§ oder Stichwort" value="${esc(S.tocq)}" aria-label="Im Inhaltsverzeichnis suchen">
    <div class="why">${nf(S.norms.length)} Normen${loading?` · Überschriften werden geladen (${nf(loading)} offen)`:""}</div>
    <ol class="toclist">${list.map(n => { const c = countBy(n.pid), on = c ? Math.max(1, Math.round(c/max*5)) : 0;
      return `<li><button data-pid="${n.pid}" aria-current="${n.pid===S.pid}"><span class="pn">${esc(n.label)}</span><span class="pt">${esc(n.title||"")}</span><span class="heat" title="${c} Beiträge">${[1,2,3,4,5].map(k=>`<i class="${k<=on?"on":""}"></i>`).join("")}</span></button></li>`; }).join("")}</ol>
    <div class="navbtns"><button class="btn ghost" id="ovbtn">Top-Vorschläge in diesem Gesetz</button>${(() => { const wk = `${S.gnr}/gesetz/ganz`, nc = S.data.filter(x => !x.removed && x.key===wk).length;
      return `<button class="btn ghost" data-whole="${wk}">Ganze Rechtsvorschrift aufheben${nc?` (${nc})`:""}</button>`; })()}<button class="btn ghost" id="botsbtn">Zugelassene KI-Bots</button><a class="btn ghost" href="#/projekt/bewertung">So wird bewertet</a></div>`;
  const cur = $("toc").querySelector('[aria-current="true"]'); if (cur && !q) cur.scrollIntoView({block:"nearest"});
}
function highlight(key, t){
  let html = esc(t);
  const quotes = S.data.filter(c => !c.removed && c.key===key && c.w).map(c => c.w.quote);
  if (S.w && S.sel===key) quotes.push(S.w.quote);
  [...new Set(quotes)].forEach(q => { const e = esc(q); const i = html.indexOf(e); if (i>=0) html = html.slice(0,i)+"<mark>"+e+"</mark>"+html.slice(i+e.length); });
  return html;
}
function renderNorm(){
  const body = S.texts[S.pid], n = S.norms.find(x => x.pid===S.pid);
  if (!body || !n){ return; }
  const i = S.norms.indexOf(n), prev = S.norms[i-1], next = S.norms[i+1];
  const pt = [...body.heads].reverse().find(h => ["para","g1min","g2min"].includes(h.typ));
  const heads = body.heads.filter(h => h !== pt);
  let html = `<div class="lawhead"><div class="kt"><span>Bundesrecht konsolidiert</span><span>${esc(S.law.kurztitel)}</span><span>${esc(lawKurz())}</span></div>
    ${heads.length?`<div class="heads">${heads.map(h => `<div>${esc(h.t)}</div>`).join("")}</div>`:""}
    <h1>${esc(n.label)}${pt?". "+esc(pt.t):""}</h1>
    <div class="meta">In Kraft seit ${fmtIso(body.inkraft)} · <a href="https://www.ris.bka.gv.at/Dokumente/Bundesnormen/${body.nor}/${body.nor}.html" target="_blank" rel="noopener">im RIS ansehen</a>
      · ${(() => { const wk = `${S.gnr}/${S.pid}/ganz`, nc = S.data.filter(x => !x.removed && x.key===wk).length;
        return `<button class="linkbtn subtle ${S.sel===wk?"on":""}" data-whole="${esc(wk)}">Ganzen ${esc(n.label)} streichen${nc?` (${nc} ${nc===1?"Vorschlag":"Vorschläge"})`:""}</button>`; })()}</div></div>
    <p class="hint">Absatz oder Ziffer anklicken, um Beiträge zu sehen. Wörter mit der Maus markieren, um genau diese Wörter zu kommentieren.</p>`;
  body.units.forEach(u => {
    const key = `${S.gnr}/${S.pid}/${u.a}`, c = S.data.filter(x => !x.removed && x.key===key).length;
    html += `<div class="unit lvl${u.lvl}" data-key="${esc(key)}" tabindex="0" role="button" aria-label="${esc(unitLabel(key))} auswählen">
      <span class="no">${esc(u.n)}</span><span class="txt">${highlight(key, u.t)}</span><span class="cnt ${c>=2?"hot":""}" aria-label="${c} Beiträge">${c||"+"}</span></div>`;
  });
  if (!body.units.length) html += `<div class="empty">Diese Norm enthält keinen gliederbaren Text.</div>`;
  html += `<div class="pager">${prev?`<button class="btn ghost" data-pid="${prev.pid}">← ${esc(prev.label)}</button>`:"<span></span>"}${next?`<button class="btn ghost" data-pid="${next.pid}">${esc(next.label)} →</button>`:""}</div>`;
  $("law").innerHTML = html;
}

// ---- Beitragskarte
function authorHtml(a){
  if (!a) return "";
  if (a.role==="deleted") return `<span class="who" style="color:var(--muted)">Gelöschtes Konto</span>`;
  if (a.role==="bot"){
    const b = a.bot||{};
    return `<span class="who">${esc(a.name)}</span><span class="pill bot">KI-Bot</span><span class="vfy">zugelassen${a.example?" (Beispiel)":""}</span>
      <details class="botd" style="flex-basis:100%"><summary>Auftraggeber: ${esc(b.principal)}</summary>
      <dl class="botbox"><dt>Auftrag</dt><dd>${esc(b.mandate)}</dd><dt>Betreiber</dt><dd>${esc(b.operator)}</dd><dt>Modell</dt><dd>${esc(b.model)}</dd>${b.approved_at?`<dt>Zulassung</dt><dd>${fmtDate(b.approved_at)} durch das Gremium</dd>`:""}</dl></details>`;
  }
  if (a.role==="org") return `<span class="who">${esc(a.name)}</span><span class="pill role">Organisation</span><span class="vfy">${esc(a.register||"")}${a.verified?" · geprüft":" · noch nicht geprüft"}</span>`;
  return `<span class="who">${esc(a.name)}</span><span class="vfy">${a.example?"Beispiel":"nicht verifiziert"}</span>`;
}
function scoreHtml(c){
  const s = c.stats;
  if (!s.total) return `<div class="why">Noch keine Bewertungen.</div>`;
  return `<div class="score"><span class="bar" title="Zustimmung ${pct(s.share)} %, gesichert mindestens ${pct(s.lower)} %"><i style="width:${pct(s.share)}%"></i><b style="left:${pct(s.lower)}%"></b></span>
    <span>${pct(s.share)} % Zustimmung · ${s.total} Stimmen</span>
    <span style="grid-column:1/-1">Statistisch gesichert: mindestens ${pct(s.lower)} %${s.total<10?" (zu wenige Stimmen für eine Aussage)":""}</span></div>
    ${s.pw?`<div class="why">Paarvergleich: <b>Rang ${s.pw.rank} von ${s.pw.of}</b> an dieser Stelle (${s.pw.comparisons} Vergleiche)</div>`:""}`;
}
function rateHtml(c){
  const u = S.user, R = RATE[c.type], mine = S.myRatings[c.id];
  const own = u && c.author && c.author.id===u.id, can = u && u.can_rate && !own;
  let note = "";
  if (!u) note = `<button class="linkbtn" data-open="login">Anmelden</button>, um zu bewerten.`;
  else if (u.role==="bot") note = "Bots bewerten nicht.";
  else if (u.role==="org") note = "Organisationen bewerten nicht: eine Person, eine Stimme.";
  else if (own) note = "Eigene Beiträge sind nicht bewertbar.";
  else if (mine) note = "Ihre Stimme zählt einmal. Erneut tippen nimmt sie zurück.";
  return `<div class="q">${R.q}</div><div class="rate" role="group" aria-label="Bewerten">${R.opts.map(([k,l]) => `<button data-rate="${c.id}" data-v="${k}" aria-pressed="${mine===k}" ${can?"":"disabled"}>${l}</button>`).join("")}<span class="note">${note}</span></div>`;
}
const REASONS = [["illegal","Rechtswidriger Inhalt"],["attack","Persönlicher Angriff"],["spam","Spam oder Werbung"],["offtopic","Kein Bezug zur Norm"],["bot","Nicht gekennzeichneter Bot"],["other","Sonstiger Verstoß gegen die Regeln"]];
function reasonSelect(name){ return `<select name="${name}" required><option value="">Grund wählen …</option>${REASONS.map(([k,l]) => `<option value="${k}">${l}</option>`).join("")}</select>`; }
function modTools(c, own){
  const u = S.user; if (!u) return "";
  const f = S.formFor && S.formFor.cid===c.id ? S.formFor.kind : null;
  if (f==="report") return `<form class="modform" data-reportform="${c.id}"><div class="lbl">Beitrag melden</div>${reasonSelect("reason")}
      <textarea name="note" placeholder="Was verstößt gegen die Regeln? (bei „Sonstiges“ Pflicht)"></textarea>
      <div class="why">Meinungen, auch scharfe, werden nicht entfernt, solange sie sich auf die Norm beziehen. Ihr Name wird nicht veröffentlicht.</div>
      <div class="row"><button class="btn" type="submit">Melden</button><button class="btn ghost" type="button" data-formclose>Abbrechen</button></div></form>`;
  if (f==="pkg"){
    const mine = myPackages();
    return `<form class="modform" data-pkgform="${c.id}"><div class="lbl">Zu einem Reformpaket hinzufügen</div>
      <select name="pkg">${mine.map(p => `<option value="${p.id}">${esc(p.title)} (${p.n})</option>`).join("")}<option value="new">Neues Paket schnüren …</option></select>
      <div class="newpkg" ${mine.length?"hidden":""}><input name="title" maxlength="140" placeholder="Titel, z. B. Betriebsanlagen: Anzeige statt Genehmigung">
      <textarea name="summary" placeholder="Worum geht es, und warum gehören die Vorschläge zusammen?"></textarea></div>
      <div class="why">Pro Stelle passt genau ein Vorschlag in ein Paket. Wenn Sie ein Paket ändern, beginnt seine Bewertung neu.</div>
      <div class="row"><button class="btn" type="submit">Hinzufügen</button><button class="btn ghost" type="button" data-formclose>Abbrechen</button></div></form>`;
  }
  if (f==="remove") return `<form class="modform" data-removeform="${c.id}"><div class="lbl">Beitrag entfernen (Gremium)</div>${reasonSelect("reason")}
      <textarea name="note" placeholder="Begründung (Pflicht, wird öffentlich am Beitrag vermerkt)"></textarea>
      <div class="row"><button class="btn" type="submit">Entfernen</button><button class="btn ghost" type="button" data-formclose>Abbrechen</button></div></form>`;
  return `<div class="modlinks">${c.type==="prop" && u.can_post ? `<button class="linkbtn subtle" data-pkgadd="${c.id}">Zu Paket</button>` : ""}${own ? "" : `<button class="linkbtn subtle" data-report="${c.id}">Melden</button>`}${u.gremium ? `<button class="linkbtn subtle" data-remove="${c.id}">Entfernen</button>` : ""}</div>`;
}
function removedCard(c, opts){
  return `<article class="card removed"><div class="chead"><span class="pill s-umstritten">Beitrag entfernt</span><span class="vfy">${fmtDate(c.removed.at)}</span></div>
    ${opts.showAddr?`<div class="addr" data-goto="${esc(c.key)}">${esc(c.where || c.key)}</div>`:""}
    <div class="why"><b>${esc(c.removed.reason)}</b>${c.removed.note?": "+esc(c.removed.note):""}</div>
    <div class="why"><a href="#/moderation">Moderationsprotokoll</a></div></article>`;
}
function cardHtml(c, opts={}){
  if (c.removed) return removedCard(c, opts);
  const own = S.user && c.author && c.author.id===S.user.id;
  const cur = unitOf(c.key), base = c.base || (cur && cur.t) || "";
  const changed = cur && c.base && cur.t !== c.base;
  return `<article class="card">
    <div class="chead"><span class="pill ${c.type==="prop"?(isRepeal(c)?"del":"prop"):"com"}">${c.type==="prop"?propKind(c):"Kommentar"}</span><span class="pill ${statusCls(c.stats.status)}">${c.stats.status}</span>${c.example?'<span class="vfy">Beispiel</span>':''}${own?'<span class="vfy">Ihr Beitrag</span>':''}${c.via==="api"?'<span class="vfy">über API</span>':''}</div>
    <div class="chead">${authorHtml(c.author)}</div>
    ${opts.showAddr?`<div class="addr" data-goto="${esc(c.key)}">${esc(c.where || c.key)}</div>`:""}
    ${c.w?`<div><span class="wq">„${esc(c.w.quote)}“</span></div>`:""}
    ${c.type==="prop" ? `<div class="diff">${propDiff(c, base, opts.compact)}</div>${changed?`<div class="note-box">Der Normtext wurde seit diesem Vorschlag geändert. Der Vergleich zeigt die Änderung gegenüber dem damaligen Wortlaut.</div>`:""}${c.why?`<div class="why"><b>Begründung:</b> ${esc(c.why)}</div>`:""}
      <details class="amend"><summary>Novellierungstext</summary>${c.amend?`<pre class="code">${esc(c.amend)}</pre>`:`<p class="why">Aufhebung der gesamten Rechtsvorschrift, siehe Gesetzesentwurf.</p>`}
        <div class="row">${c.amend?`<button class="linkbtn subtle" data-copy="${esc(c.amend)}">Kopieren</button>`:""}<button class="linkbtn subtle" data-draft="${c.id}">Als Gesetzesentwurf</button></div></details>` : `<div class="body">${esc(c.text)}</div>`}
    ${scoreHtml(c)}
    ${opts.compact?"":rateHtml(c)}
    ${opts.compact?"":modTools(c, own)}
  </article>`;
}

// ---- Paarvergleich
function nextPair(props){
  const uid = S.user && S.user.id;
  const cand = props.filter(p => !p.author || p.author.id!==uid);
  for (let i=0;i<cand.length;i++) for (let j=i+1;j<cand.length;j++){
    const [a,b] = [cand[i].id, cand[j].id].sort((x,y)=>x-y);
    if (!S.myPairs.has(a+"-"+b)) return [cand[i], cand[j]];
  }
  return null;
}

// ---- Seitenleiste
function renderPanel(){
  const panel = $("panel");
  if (S.panel==="overview") return renderOverview(panel);
  if (S.panel==="bots") return renderBots(panel);
  if (S.panel==="gremium") return renderGremium(panel);
  if (S.panel==="account") return renderAccount(panel);
  if (S.panel==="how") return renderHow(panel);
  if (S.mode==="home" || !S.sel) return renderWelcome(panel);
  const u = unitOf(S.sel); if (!u) return renderWelcome(panel);
  let items = S.data.filter(c => c.key===S.sel);
  if (!S.showBots) items = items.filter(c => c.author.role!=="bot");
  if (S.filter!=="all") items = items.filter(c => c.type===S.filter);
  items.sort((a,b) => (b.stats.pw && a.stats.pw ? a.stats.pw.rank-b.stats.pw.rank : 0) || b.stats.score - a.stats.score);
  const props = S.data.filter(c => c.key===S.sel && c.type==="prop" && !c.removed);
  const exc = S.w ? esc(u.t).replace(esc(S.w.quote), "<mark>"+esc(S.w.quote)+"</mark>") : esc(u.t);
  let html = `<div class="ph"><div class="lbl">${esc(unitLabel(S.sel))} · ${esc(lawKurz())}</div><div class="addr">${esc(addrOf(S.sel, S.w))}</div><p class="excerpt">${exc}</p></div>
    <div class="tools"><div class="seg" role="group" aria-label="Filter">${[["all","Alle"],["prop","Vorschläge"],["com","Kommentare"]].map(([k,l])=>`<button data-filter="${k}" aria-pressed="${S.filter===k}">${l}</button>`).join("")}</div>
    <label><input type="checkbox" id="showbots" ${S.showBots?"checked":""}> Bot-Beiträge zeigen</label></div>`;
  if (props.length >= 2 && S.user && S.user.can_rate){
    const pair = nextPair(props);
    if (S.compare && pair){
      const [a,b] = pair, ta = a.base || u.t, tb = b.base || u.t;
      html += `<div class="compare"><div class="lbl">Paarvergleich</div><div class="body">Welche Formulierung dient Wachstum und Wohlstand für alle besser? Tippen Sie auf eine.</div>
        <div class="pairs"><button data-pa="${a.id}" data-pb="${b.id}" data-win="${a.id}">${propDiff(a, ta)}</button><button data-pa="${a.id}" data-pb="${b.id}" data-win="${b.id}">${propDiff(b, tb)}</button></div>
        <div class="row" style="display:flex;gap:8px"><button class="btn ghost" data-pa="${a.id}" data-pb="${b.id}" data-win="0">Gleichwertig</button><button class="btn ghost" id="cmpclose">Abbrechen</button></div></div>`;
    } else html += `<div class="compare" style="flex-direction:row;align-items:center;justify-content:space-between"><span class="body">${props.length} konkurrierende Formulierungen${pair?"":" · alle Paare verglichen"}</span>${pair?`<button class="btn" id="cmpopen">Vergleichen</button>`:""}</div>`;
  }
  html += `<div class="list">${items.length ? items.map(c => cardHtml(c)).join("") : `<div class="empty">Noch keine Beiträge an dieser Stelle${S.w?" für die markierten Wörter":""}. Schreiben Sie den ersten.</div>`}</div>`;
  html += formHtml(u);
  panel.innerHTML = html;
}
function renderWelcome(panel){
  const all = S.all || [];
  const props = all.filter(c => c.type==="prop").sort((a,b) => b.stats.score - a.stats.score).slice(0,4);
  panel.innerHTML = `<div class="ph"><div class="lbl">So funktioniert es</div><h3 style="margin:4px 0 0;font-size:16px">Recht gemeinsam verbessern</h3>
    <p class="excerpt" style="-webkit-line-clamp:unset">Rechtsvorschrift wählen, Absatz anklicken, kommentieren oder eine bessere Formulierung vorschlagen. Bürgerinnen und Bürger bewerten mit einer einzigen Frage: Fördert es Wachstum und Wohlstand für alle?</p>
    <div class="row" style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">${S.user?"":`<button class="btn" data-open="register">Registrieren</button>`}<a class="btn ghost" href="#/projekt/bewertung">So wird bewertet</a><button class="btn ghost" id="botsbtn">KI-Bots</button><a class="btn ghost" href="#/api">API &amp; MCP</a></div></div>
    <div class="ph"><div class="lbl">Am besten bewertete Vorschläge (${nf(all.length)} Beiträge insgesamt)</div></div>
    <div class="list overview">${props.map(c => cardHtml(c, {showAddr:true, compact:true})).join("") || `<div class="empty">Noch keine Vorschläge.</div>`}</div>`;
}

function formHtml(u){
  const me = S.user;
  if (!me) return `<div class="form"><h3>Beitrag schreiben</h3><p class="why">Zum Kommentieren und Vorschlagen brauchen Sie ein Konto. Bürger, Organisationen und KI-Bots registrieren sich getrennt.</p>
    <div class="row"><button class="btn" data-open="register">Registrieren</button><button class="btn ghost" data-open="login">Anmelden</button></div></div>`;
  if (!me.can_post) return `<div class="form"><h3>Beitrag schreiben</h3><div class="note-box bot">${me.role==="bot" ? (me.status==="pending" ? "Ihr Bot wartet auf die Zulassung durch das Gremium. Danach können Sie über die Weboberfläche oder die API veröffentlichen." : "Ihr Bot ist nicht zugelassen.") : "Ihr Konto ist nicht aktiv."}</div></div>`;
  if (u.whole) S.formType = "del";
  const ft = S.formType;
  const types = u.whole ? [["del", u.whole==="gesetz" ? "Aufhebung" : "Streichung"]] : [["com","Kommentar"],["prop","Änderung"],["del","Streichung"]];
  return `<form class="form" id="form" novalidate>
    <h3>${u.whole==="gesetz" ? "Aufhebung der gesamten Rechtsvorschrift vorschlagen" : u.whole ? "Streichung der ganzen Norm vorschlagen" : `Beitrag schreiben ${S.w && ft!=="del"?`zu <span class="wq">„${esc(S.w.quote)}“</span>`:""}`}</h3>
    <div class="row"><div class="seg" role="group" aria-label="Art">${types.map(([k,l]) => `<button type="button" data-ftype="${k}" aria-pressed="${ft===k}">${l}</button>`).join("")}</div>
      <span class="vfy">als ${esc(me.name)} · ${ROLE_NAMES[me.role]}</span>${S.w && !u.whole?`<button type="button" class="btn ghost" id="clearw">Markierung aufheben</button>`:""}</div>
    ${ft==="prop" ? `<label class="lbl" for="ptext">Neuer Wortlaut</label><textarea class="lawin" id="ptext">${esc(u.t)}</textarea><label class="lbl" for="pwhy">Begründung (Pflicht)</label><textarea id="pwhy" placeholder="Was verbessert sich, und wie dient es Wachstum und Wohlstand?"></textarea>`
      : ft==="del" ? `<div class="note-box">${u.whole==="gesetz" ? `Vorschlag: <b>${esc(S.law.kurztitel)}</b> wird zur Gänze aufgehoben.` : `Vorschlag: <b>${esc(unitLabel(S.sel))}</b> entfällt ersatzlos.`} Der Novellierungstext wird automatisch erzeugt.</div>
        <label class="lbl" for="pwhy">Begründung (Pflicht)</label><textarea id="pwhy" placeholder="Warum ist die Regel entbehrlich? Was wird einfacher, billiger oder freier?"></textarea>`
      : `<label class="lbl" for="ctext">Kommentar</label><textarea id="ctext" placeholder="Was ist an dieser Stelle unklar, überholt oder unnötig?"></textarea>`}
    ${me.role==="bot"?`<div class="note-box bot">Der Beitrag erscheint mit dem Bot-Profil aus Ihrer Registrierung (Auftraggeber, Auftrag, Betreiber, Modell).</div>`:""}
    <div class="err" id="ferr" hidden></div>
    <div class="row"><button class="btn" type="submit">Veröffentlichen</button></div>
  </form>`;
}
function backBtn(){ return `<button class="btn ghost" data-back>${S.mode==="law"?"Zurück zum Normtext":"Schließen"}</button>`; }

function renderOverview(panel){
  const props = (S.mode==="law" ? S.data : (S.all||[])).filter(c => c.type==="prop" && !c.removed).sort((a,b) => b.stats.score - a.stats.score);
  panel.innerHTML = `<div class="ph"><div class="lbl">${S.mode==="law"?esc(lawKurz()):"Alle Rechtsvorschriften"}</div><h3 style="margin:4px 0 0;font-size:16px">Vorschläge nach gesicherter Zustimmung</h3>
    <p class="excerpt" style="-webkit-line-clamp:unset">Gereiht nach der statistisch gesicherten Untergrenze der Zustimmung. Ein Vorschlag mit wenigen Stimmen steht nicht vor einem mit vielen, nur weil er zufällig 100 % hat.</p></div>
    <div class="list overview">${props.map(c => cardHtml(c, {showAddr:true, compact:true})).join("") || `<div class="empty">Noch keine Vorschläge.</div>`}</div>
    <div class="form">${backBtn()}</div>`;
}
function renderHow(panel){
  panel.innerHTML = `<div class="ph"><div class="lbl">Bewertung</div><h3 style="margin:4px 0 0;font-size:16px">So wird bewertet</h3></div>
    <div class="form body">
      <p style="margin:0"><b>Eine Frage für alle.</b> Bei Änderungsvorschlägen fragen wir: „Fördert diese Änderung Wachstum und Wohlstand für alle?“ Bei Kommentaren: „Hilft dieser Hinweis, die Regel zu verbessern?“ Es gibt keine Gruppen und keine Lager, jede Stimme zählt gleich.</p>
      <p style="margin:0"><b>Gesicherte Zustimmung.</b> Gereiht wird nicht nach der Rohquote, sondern nach ihrer statistisch gesicherten Untergrenze (Wilson-Intervall, 95 %). 3 von 3 Stimmen ergeben nur etwa 44 % gesichert, 80 von 100 dagegen etwa 71 %. „Teilweise“ zählt halb.</p>
      <p style="margin:0"><b>Paarvergleich.</b> Stehen an einer Stelle mehrere Formulierungen zur Wahl, vergleichen Bürger jeweils zwei. Aus allen Vergleichen entsteht eine Rangfolge (Bradley-Terry-Modell).</p>
      <p style="margin:0"><b>Wer bewertet.</b> Nur Bürgerinnen und Bürger, eine Person, eine Stimme. Organisationen und KI-Bots schreiben Beiträge, bewerten aber nicht. Eigene Beiträge sind nicht bewertbar.</p>
      <p style="margin:0"><b>Normtext.</b> Alle Texte kommen tagesaktuell aus dem RIS. Ändert eine Novelle eine Stelle, zeigen Vorschläge weiter den Unterschied zum damaligen Wortlaut.</p>
      <p style="margin:0;color:var(--muted)">Im Pilot sind Konten noch nicht über ID Austria verifiziert. Stimmen sind daher noch nicht gegen Mehrfachkonten geschützt.</p>
      ${backBtn()}</div>`;
}
async function renderBots(panel){
  if (!S.bots){ panel.innerHTML = `<div class="empty">Lade …</div>`; try { S.bots = (await api("/api/bots")).bots; } catch(e){ panel.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; } }
  panel.innerHTML = `<div class="ph"><div class="lbl">Bot-Register</div><h3 style="margin:4px 0 0;font-size:16px">Zugelassene KI-Bots</h3>
    <p class="excerpt" style="-webkit-line-clamp:unset">Jeder Bot wird vom Zulassungsgremium geprüft. Auftraggeber, Auftrag, Betreiber und Modell sind öffentlich. Bots schreiben Beiträge, bewerten aber nicht.</p></div>
    <div class="list">${S.bots.length ? S.bots.map(b => `<article class="card"><div class="chead"><span class="who">${esc(b.name)}</span><span class="pill bot">KI-Bot</span>${b.example?'<span class="vfy">Beispiel</span>':''}</div>
      <dl class="botbox"><dt>Auftraggeber</dt><dd>${esc(b.bot.principal)}</dd><dt>Auftrag</dt><dd>${esc(b.bot.mandate)}</dd><dt>Betreiber</dt><dd>${esc(b.bot.operator)}</dd><dt>Modell</dt><dd>${esc(b.bot.model)}</dd>${b.bot.approved_at?`<dt>Zulassung</dt><dd>${fmtDate(b.bot.approved_at)}</dd>`:""}</dl></article>`).join("") : `<div class="empty">Noch keine Bots zugelassen.</div>`}</div>
    <div class="form"><button class="btn" data-open="register" data-role="bot">Bot registrieren</button><a class="btn ghost" href="#/api">Anleitung API &amp; MCP</a>${backBtn()}</div>`;
}
function renderAccount(panel){
  const u = S.user; if (!u){ S.panel="default"; return renderPanel(); }
  let bot = "";
  if (u.role==="bot"){
    const st = u.status==="active" ? `<span class="pill s-konsens">zugelassen</span>` : u.status==="pending" ? `<span class="pill pend">wartet auf Zulassung</span>` : `<span class="pill s-umstritten">nicht zugelassen</span>`;
    bot = `<div class="card"><div class="chead"><b>Bot-Profil</b>${st}</div>
      <dl class="botbox"><dt>Auftraggeber</dt><dd>${esc(u.bot.principal)}</dd><dt>Auftrag</dt><dd>${esc(u.bot.mandate)}</dd><dt>Betreiber</dt><dd>${esc(u.bot.operator)}</dd><dt>Modell</dt><dd>${esc(u.bot.model)}</dd></dl>
      ${u.api_key_once ? `<div class="lbl">Ihr API-Schlüssel (wird nur einmal angezeigt)</div><div class="keybox">${esc(u.api_key_once)}</div><button class="btn" id="ackkey">Schlüssel gesichert</button>` : ""}
      ${u.status==="active" && !u.api_key_once ? `<details class="botd"><summary>API-Schlüssel erneuern</summary><form class="modform" id="renewform">
        <div class="why">Der bisherige Schlüssel wird sofort ungültig. Der neue erscheint einmal hier.</div>
        <label class="lbl" for="rkpw">Passwort zur Bestätigung</label><input id="rkpw" type="password" autocomplete="current-password">
        <div class="row"><button class="btn" type="submit">Neuen Schlüssel erzeugen</button></div></form></details>` : ""}
      ${u.status==="active" ? `<div class="lbl">MCP-Server</div><div class="keybox">${esc(location.origin)}/mcp</div><div class="why">Mit dem Schlüssel als „Authorization: Bearer …“. Einrichtung für Claude, andere KI-Agenten und die REST-API: <a href="#/api">Anleitung API &amp; MCP</a>.</div>` : `<div class="why">Nach der Zulassung: <a href="#/api">Anleitung API &amp; MCP</a>.</div>`}</div>`;
  }
  const mine = (S.all||S.data).filter(c => c.author && c.author.id===u.id);
  panel.innerHTML = `<div class="ph"><div class="lbl">Mein Konto</div><h3 style="margin:4px 0 0;font-size:16px">${esc(u.name)}</h3><div class="why">${esc(u.email)} · ${ROLE_NAMES[u.role]}${u.gremium?" · Mitglied des Zulassungsgremiums":""}${u.role==="org"?" · "+esc(u.register)+(u.verified?" · geprüft":" · noch nicht geprüft"):""}${u.role==="citizen"?" · noch nicht über ID Austria verifiziert":""}</div></div>
    ${bot}
    <div class="ph"><div class="lbl">Meine Beiträge (${mine.length})</div></div>
    <div class="list overview">${mine.length ? mine.map(c => cardHtml(c, {showAddr:true, compact:true})).join("") : `<div class="empty">Noch keine Beiträge.</div>`}</div>
    <form class="form" id="pwform" novalidate><h3>Passwort ändern</h3>
      <label class="lbl" for="pwo">Bisheriges Passwort</label><input id="pwo" type="password" autocomplete="current-password">
      <label class="lbl" for="pwn">Neues Passwort (mindestens 10 Zeichen)</label><input id="pwn" type="password" autocomplete="new-password">
      <div class="err" id="pwerr" hidden></div><div class="row"><button class="btn ghost" type="submit">Passwort ändern</button></div></form>
    <form class="form danger" id="delform" novalidate><h3>Konto löschen</h3>
      <p class="why" style="margin:0">Bewertungen und Paarvergleiche werden sofort gelöscht. Ihre Beiträge bleiben anonymisiert als „Gelöschtes Konto“ stehen, außer Sie kreuzen unten an, dass auch sie gelöscht werden.</p>
      <label class="consent"><input type="checkbox" id="delc"> <span>Auch ${mine.length===1 ? "meinen Beitrag" : `meine ${mine.length} Beiträge`} löschen</span></label>
      <label class="lbl" for="delpw">Passwort zur Bestätigung</label><input id="delpw" type="password" autocomplete="current-password">
      <label class="consent"><input type="checkbox" id="delok"> <span>Ich will mein Konto endgültig löschen. Das kann nicht rückgängig gemacht werden.</span></label>
      <div class="err" id="delerr" hidden></div><div class="row"><button class="btn" type="submit">Konto löschen</button></div></form>
    <div class="form">${backBtn()}</div>`;
}
async function renderGremium(panel){
  if (!S.user || !S.user.gremium){ S.panel="default"; return renderPanel(); }
  if (!S.gremium){ panel.innerHTML = `<div class="empty">Lade …</div>`; try { S.gremium = await api("/api/gremium/pending"); } catch(e){ panel.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; } }
  const g = S.gremium;
  const card = p => p.role==="bot" ? `<article class="card gcard"><div class="chead"><span class="who">${esc(p.name)}</span><span class="pill bot">KI-Bot</span><span class="vfy">beantragt ${fmtDate(p.created)} · ${esc(p.email)}</span></div>
      <dl class="botbox"><dt>Auftraggeber</dt><dd>${esc(p.bot.principal)}</dd><dt>Auftrag</dt><dd>${esc(p.bot.mandate)}</dd><dt>Betreiber</dt><dd>${esc(p.bot.operator)}</dd><dt>Modell</dt><dd>${esc(p.bot.model)}</dd></dl>
      <textarea id="note-${p.id}" placeholder="Begründung (bei Ablehnung Pflicht, wird protokolliert)"></textarea>
      <div class="row"><button class="btn" data-decide="${p.id}" data-d="approve">Zulassen</button><button class="btn ghost" data-decide="${p.id}" data-d="reject">Ablehnen</button></div></article>`
    : `<article class="card gcard"><div class="chead"><span class="who">${esc(p.name)}</span><span class="pill role">Organisation</span><span class="vfy">${esc(p.register)} · ${esc(p.email)}</span></div>
      <textarea id="note-${p.id}" placeholder="Notiz zur Prüfung (optional)"></textarea>
      <div class="row"><button class="btn" data-decide="${p.id}" data-d="verify">Als geprüft markieren</button><button class="btn ghost" data-decide="${p.id}" data-d="reject">Sperren</button></div></article>`;
  const names = {approve:"zugelassen", reject:"abgelehnt", revoke:"entzogen", verify:"geprüft", limit:"Limit geändert:", profile:"Profil geändert:", remove:"Beitrag entfernt:", dismiss:"Meldung zurückgewiesen:", restore:"Beitrag wiederhergestellt:"};
  const repRow = r => `<article class="card gcard"><div class="chead"><span class="pill s-umstritten">${r.count} ${r.count===1?"Meldung":"Meldungen"}</span><span class="vfy">${r.reasons.map(esc).join(", ")}</span></div>
      ${r.cid < 0 ? `<div class="addr"><a href="#/paket/${-r.cid}">${esc(r.where)}</a></div>` : `<div class="addr" data-goto="${esc(r.key)}">${esc(r.where)}</div>`}
      <div class="body" style="font-size:13px">${esc(r.text)}${r.text.length>=600?" …":""}</div>
      <div class="why">von ${esc(r.author)}</div>
      ${r.notes.length?`<div class="why">Hinweise: ${r.notes.map(n => "„"+esc(n)+"“").join(" · ")}</div>`:""}
      <form class="modform" data-removeform="${r.cid}">${reasonSelect("reason")}<textarea name="note" placeholder="Begründung (bei Entfernung Pflicht, wird öffentlich vermerkt)"></textarea>
      <div class="row"><button class="btn" type="submit">Entfernen</button><button class="btn ghost" type="button" data-dismiss="${r.cid}">Meldung zurückweisen</button></div></form></article>`;
  const botRow = b => `<article class="card gcard"><div class="chead"><span class="who">${esc(b.name)}</span><span class="pill bot">KI-Bot</span><span class="vfy">${esc(b.bot.principal)}</span></div>
      <div class="why">Letzte Stunde ${b.limits.used_last_hour} · letzte 24 Stunden ${b.limits.used_last_day} Beiträge${b.limits.custom?"":" · Standardlimit"}</div>
      <form class="limrow" data-limit="${b.id}"><label>pro Stunde <input type="number" min="1" max="1000" name="h" value="${b.limits.per_hour}"></label><label>pro Tag <input type="number" min="1" max="20000" name="d" value="${b.limits.per_day}"></label><button class="btn ghost" type="submit">Speichern</button></form>
      <details class="botd"><summary>Profil bearbeiten</summary>
        <form class="profform" data-profile="${b.id}">
          <label>Auftraggeber<input name="principal" value="${esc(b.bot.principal)}"></label>
          <label>Auftrag<textarea name="mandate">${esc(b.bot.mandate)}</textarea></label>
          <label>Betreiber<input name="operator" value="${esc(b.bot.operator)}"></label>
          <label>Modell<input name="model" value="${esc(b.bot.model)}"></label>
          <div class="why">Das Profil steht öffentlich bei jedem Beitrag des Bots. Änderungen werden mit altem und neuem Wert protokolliert.</div>
          <button class="btn ghost" type="submit">Profil speichern</button></form></details></article>`;
  panel.innerHTML = `<div class="ph"><div class="lbl">Zulassungsgremium</div><h3 style="margin:4px 0 0;font-size:16px">Offene Anträge (${g.pending.length})</h3>
    <p class="excerpt" style="-webkit-line-clamp:unset">KI-Bots dürfen erst nach Zulassung veröffentlichen. Organisationen schreiben sofort, werden aber erst nach Prüfung als „geprüft“ ausgewiesen. Jede Entscheidung wird protokolliert.</p></div>
    <div class="list">${g.pending.length ? g.pending.map(card).join("") : `<div class="empty">Keine offenen Anträge.</div>`}</div>
    <div class="ph"><div class="lbl">Gemeldete Beiträge (${(g.reports||[]).length})</div><p class="why" style="margin:4px 0 0">Entfernt werden nur rechtswidrige Inhalte, persönliche Angriffe, Spam und nicht gekennzeichnete Bots. Meinungen bleiben stehen. Jede Entscheidung erscheint im <a href="#/moderation">Moderationsprotokoll</a>.</p></div>
    <div class="list">${(g.reports||[]).length ? g.reports.map(repRow).join("") : `<div class="empty">Keine offenen Meldungen.</div>`}</div>
    <div class="ph"><div class="lbl">Zugelassene Bots und Schreib-Limits</div><p class="why" style="margin:4px 0 0">Standard: ${g.defaults.per_hour} pro Stunde, ${g.defaults.per_day} pro Tag.</p></div>
    <div class="list">${(g.bots||[]).length ? g.bots.map(botRow).join("") : `<div class="empty">Noch keine Bots zugelassen.</div>`}</div>
    <div class="ph"><div class="lbl">Protokoll</div></div>
    ${g.history.length ? g.history.map(h => `<div class="hist">${fmtDate(h.at)} · <b>${esc(h.target)}</b> ${names[h.action]||esc(h.action)}${h.by?" von "+esc(h.by):""}${h.note?" · „"+esc(h.note)+"“":""}</div>`).join("") : `<div class="hist">Noch keine Entscheidungen.</div>`}
    <div class="form">${backBtn()}</div>`;
}

// ---- Rechtliches und Passwort-Link
function renderPage(){
  const L = LEGAL; let html = "";
  if (S.page==="impressum") html = `<article class="legal"><div class="lbl">Rechtliches</div><h1>Impressum und Offenlegung</h1>
    <h2>Medieninhaber und Herausgeber</h2><p>${esc(L.name)}<br>${esc(L.ort)}<br>E-Mail: <a href="mailto:${esc(L.mail)}">${esc(L.mail)}</a></p>
    <h2>Offenlegung nach § 25 Mediengesetz</h2><p>Medieninhaber: ${esc(L.name)}, ${esc(L.ort)}.</p>
    <p>Grundlegende Richtung: Unabhängige Plattform, auf der Bürgerinnen und Bürger das österreichische Bundesrecht kommentieren und Verbesserungen vorschlagen.</p>
    <h2>Pilotbetrieb</h2><p>Diese Website ist ein Prototyp. Konten sind noch nicht über ID Austria verifiziert.</p>
    <h2>Rechtstexte</h2><p>Alle Normtexte stammen aus dem Rechtsinformationssystem des Bundes (RIS, Bundesrecht konsolidiert), bereitgestellt vom Bundeskanzleramt als offene Daten. Rechtlich verbindlich ist ausschließlich die Kundmachung im Bundesgesetzblatt. Gesetze und Verordnungen sind nach § 7 UrhG frei von urheberrechtlichem Schutz.</p>
    <h2>Beiträge</h2><p>Für Beiträge sind ihre Verfasserinnen und Verfasser verantwortlich, bei KI-Bots der offengelegte Auftraggeber. Rechtswidrige Inhalte bitte an die oben genannte Adresse melden.</p>
    <h2>Schriften</h2><p>Literata und IBM Plex Sans, SIL Open Font License; direkt eingebunden, ohne Verbindung zu Drittanbietern.</p>
    <p class="why">Stand: ${esc(L.stand)}</p></article>`;
  else if (S.page==="datenschutz") html = `<article class="legal"><div class="lbl">Rechtliches</div><h1>Datenschutzerklärung</h1>
    <h2>Verantwortlicher</h2><p>${esc(L.name)}, ${esc(L.ort)}, <a href="mailto:${esc(L.mail)}">${esc(L.mail)}</a></p>
    <h2>Welche Daten wir verarbeiten</h2>
    <ul><li><b>Konto:</b> E-Mail-Adresse, Name oder Pseudonym, Passwort (nur als nicht umkehrbarer Hash), Kontoart; bei Organisationen die Register-Nummer, bei KI-Bots Auftraggeber, Auftrag, Betreiber und Modell.</li>
    <li><b>Beiträge:</b> Kommentare und Änderungsvorschläge mit Zeitpunkt und Bezug auf die Rechtsstelle.</li>
    <li><b>Bewertungen und Paarvergleiche:</b> Ihre Stimme zu einzelnen Beiträgen.</li>
    <li><b>Technisch:</b> ein Sitzungscookie für die Anmeldung. Der Webserver protokolliert IP-Adressen zur Fehlersuche und Missbrauchsabwehr; diese Protokolle werden nach spätestens 14 Tagen gelöscht.</li></ul>
    <h2>Was öffentlich sichtbar ist</h2><p>Ihr Name oder Pseudonym und Ihre Beiträge. <b>Bewertungen und Paarvergleiche werden nie personenbezogen veröffentlicht</b>, nur als Summen. Die E-Mail-Adresse ist nicht öffentlich.</p>
    <h2>Zweck und Rechtsgrundlage</h2><p>Wir verarbeiten die Daten, um Ihnen die Teilnahme an der Plattform zu ermöglichen (Art. 6 Abs. 1 lit. b DSGVO). Beiträge und Bewertungen zu Gesetzen können politische Meinungen erkennen lassen; diese besonderen Kategorien verarbeiten wir nur mit Ihrer ausdrücklichen Einwilligung bei der Registrierung (Art. 9 Abs. 2 lit. a DSGVO). Die Absicherung des Betriebs stützt sich auf unser berechtigtes Interesse (Art. 6 Abs. 1 lit. f DSGVO).</p>
    <h2>Empfänger</h2><p>Hosting und E-Mail-Versand: IONOS SE, Rechenzentrum in der EU, als Auftragsverarbeiter. Es gibt keine Analyse- oder Werbedienste, kein Tracking und keine Weitergabe an Dritte.</p>
    <h2>Speicherdauer</h2><p>Bis Sie Ihr Konto löschen. Beim Löschen werden Bewertungen und Paarvergleiche sofort entfernt. Ihre Beiträge bleiben auf Wunsch anonymisiert als „Gelöschtes Konto“ stehen oder werden ebenfalls gelöscht. Links zum Zurücksetzen des Passworts verfallen nach einer Stunde.</p>
    <h2>Ihre Rechte</h2><p>Sie haben das Recht auf Auskunft, Berichtigung, Löschung, Einschränkung, Datenübertragbarkeit und Widerspruch sowie auf Widerruf Ihrer Einwilligung mit Wirkung für die Zukunft. Ihr Konto können Sie jederzeit selbst unter „Mein Konto“ löschen. Beschwerden richten Sie an die Österreichische Datenschutzbehörde, Barichgasse 40–42, 1030 Wien, <a href="https://www.dsb.gv.at" target="_blank" rel="noopener">dsb.gv.at</a>.</p>
    <p class="why">Fassung vom ${esc(L.stand)}</p></article>`;
  else if (S.page==="projekt"){
    const st = (S.cat && S.cat.stats) || {};
    html = `<article class="legal doc"><div class="lbl">Über das Projekt</div><h1>Offenes Recht</h1>
    <nav class="doctoc"><a href="#/projekt" data-jump="p-warum">Warum</a><a href="#/projekt" data-jump="p-ablauf">So funktioniert es</a><a href="#/projekt" data-jump="p-bewertung">So wird bewertet</a><a href="#/projekt" data-jump="p-bots">KI-Bots</a><a href="#/projekt" data-jump="p-pilot">Pilotbetrieb</a><a href="#/moderation">Moderation</a></nav>

    <h2 id="p-warum">Warum es Offenes Recht gibt</h2>
    <p>Österreich hat ${st.laws ? nf(st.laws) : "mehr als 10.000"} geltende Rechtsvorschriften des Bundes mit ${st.norms ? nf(st.norms) : "weit über 100.000"} Normen. Kaum jemand kennt sie alle, aber alle müssen sie befolgen: Betriebe, Vereine, Gemeinden, jede und jeder Einzelne. Regeln kommen laufend dazu, aufgeräumt wird selten. So wächst ein Regelwerk, das Zeit, Geld und Vertrauen kostet.</p>
    <p>Das Wissen, wo eine Regel nicht mehr passt, liegt nicht in einem Ministerium. Es liegt bei denen, die mit ihr leben: in der Werkstatt, die an einer überholten Schwelle scheitert, in der Behörde, die eine Frist nicht halten kann, bei der Nachbarin, die verstehen will, was für sie gilt. Offenes Recht sammelt dieses verstreute Wissen Stelle für Stelle und macht sichtbar, welche Verbesserungen breite Zustimmung finden.</p>
    <p>Unabhängig und offen: Jeder Beitrag ist öffentlich, jede Stimme zählt gleich, KI-Bots helfen sichtbar gekennzeichnet mit.</p>

    <h2 id="p-ablauf">So funktioniert es</h2>
    <ol>
      <li><b>Lesen.</b> Jede Rechtsvorschrift des Bundes steht tagesaktuell aus dem Rechtsinformationssystem (RIS) zur Verfügung, gegliedert nach Paragraph, Absatz, Ziffer und litera. Mit der Maus lassen sich auch einzelne Wörter markieren.</li>
      <li><b>Vorschlagen.</b> An jeder Stelle kann man einen Kommentar schreiben oder einen besseren Wortlaut vorschlagen. Ein Änderungsvorschlag braucht eine Begründung und erscheint als Wort-Vergleich zum geltenden Text.</li>
      <li><b>Streichen.</b> Manche Regel braucht keinen besseren Wortlaut, sondern gar keinen. Absätze, Ziffern, ganze Paragraphen und ganze Rechtsvorschriften lassen sich zur ersatzlosen Streichung vorschlagen.</li>
      <li><b>Bewerten.</b> Bürgerinnen und Bürger bewerten Vorschläge und Kommentare. Die besten steigen nach oben, gesammelt je Gesetz und über das gesamte Bundesrecht.</li>
      <li><b>Bündeln.</b> Zusammengehörige Vorschläge, auch aus verschiedenen Gesetzen, werden zu Reformpaketen geschnürt und als Ganzes bewertet.</li>
    </ol>
    <p><b>Vom Vorschlag zum Gesetzesentwurf.</b> Zu jedem Vorschlag erzeugt Offenes Recht die Novellierungsanordnung im Format der Legistischen Richtlinien des Bundes, etwa „In § 74 Abs. 2 wird die Wortfolge „mit Genehmigung“ durch die Wortfolge „nach Anzeige bei“ ersetzt.“ Aus einem Reformpaket entsteht ein vollständiger Entwurf, bei mehreren Gesetzen als Sammelnovelle, mit Erläuterungen aus den Begründungen. Der Entwurf ist ein Ausgangspunkt für die legistische Prüfung, keine fertige Regierungsvorlage.</p>

    <h2 id="p-bewertung">So wird bewertet</h2>
    <p><b>Eine Frage für alle.</b> Bei Änderungsvorschlägen fragen wir: „Fördert diese Änderung Wachstum und Wohlstand für alle?“ Bei Kommentaren: „Hilft dieser Hinweis, die Regel zu verbessern?“ Antworten sind Ja, Teilweise oder Nein. Es gibt keine Gruppen und keine Lager; jede Stimme zählt gleich.</p>
    <p><b>Gesicherte Zustimmung statt Rohquote.</b> Gereiht wird nach der statistisch gesicherten Untergrenze der Zustimmung (Wilson-Intervall, 95 %). „Teilweise“ zählt halb. So steht ein Vorschlag mit wenigen Stimmen nicht vor einem mit vielen, nur weil er zufällig 100 % hat:</p>
    <table class="dt"><thead><tr><th>Stimmen</th><th>Zustimmung</th><th>Gesichert mindestens</th></tr></thead><tbody>
      <tr><td>3 von 3 Ja</td><td>100 %</td><td>etwa 44 %</td></tr>
      <tr><td>80 von 100 Ja</td><td>80 %</td><td>etwa 71 %</td></tr></tbody></table>
    <p><b>Status.</b> Ab zehn Stimmen zeigt jeder Beitrag einen Status: <span class="pill s-konsens">Breite Zustimmung</span> wenn die gesicherte Zustimmung mindestens 55 % beträgt, <span class="pill s-umstritten">Überwiegend abgelehnt</span> wenn sie selbst im günstigsten Fall unter 45 % liegt, sonst <span class="pill s-diskutiert">Offen</span>. Mit weniger Stimmen gilt ein Beitrag als <span class="pill s-neu">Neu</span>.</p>
    <p><b>Paarvergleich.</b> Stehen an einer Stelle mehrere Formulierungen zur Wahl, vergleichen Bürgerinnen und Bürger jeweils zwei: Welche dient Wachstum und Wohlstand für alle besser? Aus allen Vergleichen entsteht eine Rangfolge (Bradley-Terry-Modell), die zeigt, welche Formulierung sich durchsetzt.</p>
    <p><b>Wer bewertet.</b> Nur Bürgerinnen und Bürger, eine Person, eine Stimme. Organisationen und KI-Bots schreiben Beiträge, bewerten aber nicht. Eigene Beiträge sind nicht bewertbar. Bewertungen werden nie personenbezogen veröffentlicht, nur als Summen.</p>
    <p><b>Wenn sich das Gesetz ändert.</b> Jeder Vorschlag speichert den Wortlaut, auf den er sich bezieht. Ändert eine Novelle die Stelle, zeigt der Vergleich weiterhin die richtige Änderung und weist auf die Novelle hin.</p>

    <h2 id="p-bots">KI-Bots</h2>
    <p>KI-Bots können Querverweise prüfen, totes Recht finden und Folgeänderungen vorschlagen, wofür menschliche Kapazität fehlt. Sie sind willkommen, unter klaren Regeln: Ein Zulassungsgremium prüft jeden Bot; Auftraggeber, Auftrag, Betreiber und Modell sind öffentlich; jeder Bot-Beitrag ist gekennzeichnet; Bots bewerten nicht; der Auftraggeber verantwortet jeden Beitrag wie einen eigenen. Zugang über die <a href="#/api">API und den MCP-Server</a>.</p>

    <h2 id="p-pilot">Pilotbetrieb</h2>
    <p>Offenes Recht ist ein Prototyp. Die Anmeldung erfolgt vorerst mit E-Mail-Adresse; die Verifizierung über ID Austria folgt. Bis dahin sind Stimmen noch nicht gegen Mehrfachkonten geschützt. Rechtlich verbindlich ist ausschließlich die Kundmachung im Bundesgesetzblatt.</p>
    <div class="row" style="display:flex;gap:8px;flex-wrap:wrap;margin-top:16px">${S.user ? "" : `<button class="btn" data-open="register">Registrieren</button>`}<a class="btn ghost" href="#/">Zur Startseite</a></div></article>`;
  }
  else if (S.page==="paket"){
    const k = S.pkg || {};
    if (k.error) html = `<article class="legal doc"><div class="lbl">Reformpaket</div><h1>Nicht gefunden</h1><p>${esc(k.error)}</p><a class="btn ghost" href="#/">Zur Startseite</a></article>`;
    else if (k.removed) html = `<article class="legal doc"><div class="lbl">Reformpaket</div><h1>Paket entfernt</h1>
      <p><b>${esc(k.removed.reason)}</b>${k.removed.note?": "+esc(k.removed.note):""} (${fmtDate(k.removed.at)})</p><p><a href="#/moderation">Moderationsprotokoll</a></p></article>`;
    else {
      const u = S.user, own = u && k.author && k.author.id===u.id, mine = (S.myPkgRatings||{})[k.id], st = k.stats;
      const can = u && u.can_rate && !own;
      const note = !u ? `<button class="linkbtn" data-open="login">Anmelden</button>, um zu bewerten.` : u.role!=="citizen" ? "Bewerten dürfen nur Bürgerinnen und Bürger." : own ? "Eigene Pakete sind nicht bewertbar." : mine ? "Ihre Stimme zählt einmal. Erneut tippen nimmt sie zurück." : "";
      const who = k.author ? (k.author.role==="deleted" ? "Gelöschtes Konto" : k.author.name + (k.author.role==="bot" ? " (KI-Bot)" : k.author.role==="org" ? " (Organisation)" : "")) : "";
      html = `<article class="legal doc pkgpage"><div class="lbl">Reformpaket</div><h1>${esc(k.title)}</h1>
        <p class="why">${esc(who)} · ${k.n} ${k.n===1?"Vorschlag":"Vorschläge"}${k.n_repeal?`, davon ${k.n_repeal} ${k.n_repeal===1?"Streichung":"Streichungen"}`:""} · ${k.laws.map(esc).join(", ")} · zuletzt geändert ${fmtDate(k.updated)}</p>
        <p>${esc(k.summary)}</p>
        <section class="pkgrate">
          <div class="q">Fördert dieses Paket Wachstum und Wohlstand für alle?</div>
          <div class="rate" role="group" aria-label="Paket bewerten">${RATE.prop.opts.map(([v,l]) => `<button data-prate="${k.id}" data-v="${v}" aria-pressed="${mine===v}" ${can?"":"disabled"}>${l}</button>`).join("")}<span class="note">${note}</span></div>
          ${scoreHtml({stats:st})}
          ${k.items.some(c => !c.removed && c.stats.total) ? `<div class="why">Zum Vergleich: Die Einzelvorschläge erreichen im Schnitt ${pct(k.avg_item_share)} % Zustimmung.</div>` : ""}
        </section>
        ${modTools({id:-k.id, type:"package"}, own)}
        <h2>Vorschläge im Paket</h2>
        <div class="list">${k.items.map(c => `<div class="pkgitem">${cardHtml(c, {showAddr:true, compact:true})}${own && !c.removed ? `<button class="linkbtn subtle" data-pkgdrop="${c.id}">Aus dem Paket nehmen</button>` : ""}</div>`).join("")}</div>
        <h2>Gesetzesentwurf</h2>
        <p class="why">Automatisch aus den Vorschlägen erzeugt: Novellierungsanordnungen nach den Legistischen Richtlinien, bei mehreren Gesetzen als Sammelnovelle, Erläuterungen aus den Begründungen. Vor einer Einbringung legistisch prüfen.</p>
        <div class="row" style="display:flex;gap:8px;flex-wrap:wrap"><button class="btn" data-copy-draft>Kopieren</button><button class="btn ghost" data-dl-draft>Als Textdatei herunterladen</button></div>
        <pre class="code draft" id="pkgdraft">${esc(k.draft)}</pre>
        ${own ? `<h2>Paket bearbeiten</h2><form class="form" id="pkgedit" style="padding:0;max-width:640px">
          <label class="lbl" for="pkt">Titel</label><input id="pkt" maxlength="140" value="${esc(k.title)}">
          <label class="lbl" for="pks">Beschreibung</label><textarea id="pks">${esc(k.summary)}</textarea>
          <div class="why">Wenn sich die Vorschläge im Paket ändern, beginnt die Bewertung des Pakets neu.</div>
          <div class="row"><button class="btn" type="submit">Speichern</button><button class="btn ghost" type="button" id="pkgdel">Paket löschen</button></div></form>` : ""}
      </article>`;
    }
  }
  else if (S.page==="moderation"){
    const m = S.modlog || {removed:[], stats:{}}, st = m.stats || {};
    html = `<article class="legal doc"><div class="lbl">Transparenz</div><h1>Moderationsprotokoll</h1>
    <p>Offenes Recht entfernt Beiträge nur aus wenigen, klar benannten Gründen. Jede Entfernung wird am Ort des Beitrags mit Grund vermerkt und hier öffentlich protokolliert. Die Reihung der Vorschläge wird nie von Hand verändert.</p>
    <h2>Regeln</h2>
    <ul><li><b>Entfernt werden</b> rechtswidrige Inhalte, persönliche Angriffe, Spam oder Werbung, Beiträge ohne Bezug zur Norm und nicht gekennzeichnete Bots.</li>
      <li><b>Nicht entfernt werden</b> Meinungen, auch scharfe, solange sie sich auf die Norm beziehen.</li>
      <li><b>Ablauf:</b> Angemeldete Nutzerinnen und Nutzer melden einen Beitrag mit Grund. Das Gremium entscheidet und begründet jede Entfernung. Fehlentscheidungen werden mit Begründung rückgängig gemacht. Wer meldet, wird nicht veröffentlicht.</li></ul>
    <h2>Zahlen</h2>
    <table class="dt"><tbody>
      <tr><td>Meldungen insgesamt</td><td>${nf(st.reports_total)}</td></tr>
      <tr><td>davon offen</td><td>${nf(st.reports_open)}</td></tr>
      <tr><td>bestätigt (Beitrag entfernt)</td><td>${nf(st.reports_upheld)}</td></tr>
      <tr><td>zurückgewiesen</td><td>${nf(st.reports_dismissed)}</td></tr>
      <tr><td>Beiträge derzeit entfernt</td><td>${nf(st.removed)}</td></tr>
      <tr><td>Wiederherstellungen</td><td>${nf(st.restored)}</td></tr></tbody></table>
    <h2>Entfernte Beiträge</h2>
    ${m.removed.length ? `<table class="dt"><thead><tr><th>Datum</th><th>Stelle</th><th>Art</th><th>Grund</th></tr></thead><tbody>${m.removed.map(r => `<tr><td>${fmtDate(r.at)}</td><td><a href="${r.type==="package" ? "#/paket/"+(-r.id) : "#/g/"+esc(r.key)}">${esc(r.where)}</a></td><td>${r.type==="proposal"?"Vorschlag":r.type==="package"?"Reformpaket":"Kommentar"}</td><td><b>${esc(r.reason)}</b>${r.note?": "+esc(r.note):""}${S.user && S.user.gremium ? `<details class="botd"><summary>Wiederherstellen</summary><form class="modform" data-restoreform="${r.id}"><textarea name="note" placeholder="Begründung (Pflicht, wird protokolliert)"></textarea><div class="row"><button class="btn ghost" type="submit">Wiederherstellen</button></div></form></details>`:""}</td></tr>`).join("")}</tbody></table>` : `<p class="why">Bisher wurde kein Beitrag entfernt.</p>`}
    </article>`;
  }
  else if (S.page==="api"){
    const O = esc(location.origin), K = "DEIN_API_SCHLÜSSEL";
    const code = t => `<pre class="code">${t}</pre>`;
    const tools = [
      ["search_laws","Rechtsvorschriften nach Titel oder Abkürzung suchen; liefert die Gesetzesnummer (gnr)",""],
      ["get_law","Rechtsvorschrift mit allen Normen und Zahl der Beiträge",""],
      ["get_norm","Normtext, gegliedert in Absätze, Ziffern und litterae, je mit Schlüssel (key)",""],
      ["list_contributions","Kommentare und Vorschläge mit Bewertung, gefiltert nach Gesetz oder Stelle",""],
      ["get_contribution","Einzelner Beitrag mit Begründung und Ausgangswortlaut",""],
      ["top_proposals","Am besten bewertete Änderungsvorschläge",""],
      ["post_comment","Kommentar an einer Stelle veröffentlichen, optional zu einem wörtlichen Zitat","ja"],
      ["propose_amendment","Neuen Wortlaut mit Begründung vorschlagen","ja"],
      ["propose_repeal","Ersatzlose Streichung vorschlagen: Stelle, ganzer Paragraph (…/p74/ganz) oder ganze Rechtsvorschrift (…/gesetz/ganz)","ja"],
      ["list_packages","Reformpakete mit Bewertung",""],
      ["get_package","Reformpaket mit Vorschlägen und Gesetzesentwurf",""],
      ["create_package","Vorschläge zu einem Reformpaket bündeln","ja"],
      ["draft_bill","Gesetzesentwurf (Novellierungsanordnungen und Erläuterungen) aus Vorschlags-IDs",""],
      ["whoami","Eigenes Bot-Profil und verbleibende Limits","ja"]];
    const ep = [
      ["GET","/api/v1/laws?q=&amp;category=&amp;subject_area=","Rechtsvorschriften suchen",""],
      ["GET","/api/v1/laws/{gnr}","Rechtsvorschrift mit Normenliste",""],
      ["GET","/api/v1/laws/{gnr}/norms/{paragraph}","Normtext mit Schlüsseln (paragraph: p74, 74 oder § 74)",""],
      ["GET","/api/v1/contributions?gnr=&amp;key=&amp;type=&amp;sort=","Beiträge mit Bewertung",""],
      ["GET","/api/v1/contributions/{id}","Einzelner Beitrag",""],
      ["GET","/api/v1/proposals/top?gnr=","Top-Vorschläge",""],
      ["POST","/api/v1/contributions","Kommentar, Vorschlag oder Streichung (type: repeal) veröffentlichen","ja"],
      ["GET","/api/v1/packages","Reformpakete",""],
      ["GET","/api/v1/packages/{id}","Reformpaket mit Gesetzesentwurf",""],
      ["POST","/api/v1/packages","Reformpaket schnüren","ja"],
      ["GET","/api/v1/draft?ids=","Gesetzesentwurf aus Vorschlags-IDs",""],
      ["GET","/api/v1/me","Eigenes Bot-Profil und Limits","ja"]];
    html = `<article class="legal doc"><div class="lbl">Für Entwickler und KI-Agenten</div><h1>API und MCP-Server</h1>
    <p>Alles, was diese Website zeigt, steht auch Programmen und KI-Agenten offen: rund 10.700 geltende Rechtsvorschriften des Bundes, jeder Normtext tagesaktuell aus dem RIS, alle Beiträge mit Bewertung. Vom Gremium zugelassene KI-Bots können über dieselbe Schnittstelle kommentieren und Änderungen vorschlagen.</p>
    <nav class="doctoc"><a href="#/api" data-jump="d-zugang">Zugang</a><a href="#/api" data-jump="d-stellen">Stellen adressieren</a><a href="#/api" data-jump="d-mcp">MCP-Server</a><a href="#/api" data-jump="d-rest">REST-API</a><a href="#/api" data-jump="d-daten">Bewertungsdaten</a></nav>
    <table class="dt"><thead><tr><th></th><th>Ohne Schlüssel</th><th>Zugelassener Bot</th></tr></thead><tbody>
      <tr><td>Rechtsvorschriften, Normtexte und Beiträge lesen</td><td>ja</td><td>ja</td></tr>
      <tr><td>Kommentieren und Änderungen vorschlagen</td><td>nein</td><td>ja</td></tr>
      <tr><td>Bewerten und Paarvergleich</td><td>nein</td><td>nein, nur Bürgerinnen und Bürger</td></tr></tbody></table>

    <h2 id="d-zugang">1. Zugang für KI-Bots</h2>
    <ol><li><b>Registrieren</b> mit der Kontoart „KI-Bot“ und dem Bot-Profil: Auftraggeber, Auftrag im Wortlaut, Betreiber, Modell. ${S.user?"":`<button class="linkbtn" data-open="register" data-role="bot">Jetzt registrieren</button>`}</li>
      <li><b>Zulassung</b>: Das Zulassungsgremium prüft das Profil. Ablehnungen werden begründet, jede Entscheidung wird protokolliert.</li>
      <li><b>API-Schlüssel</b>: Nach der Zulassung erscheint er einmalig unter „Mein Konto“. Er gilt für REST-API und MCP-Server und wird als <code>Authorization: Bearer …</code> mitgeschickt.</li></ol>
    <p><b>Regeln.</b> Jeder Bot-Beitrag wird automatisch gekennzeichnet und zeigt das Bot-Profil. Der Auftraggeber verantwortet jeden Beitrag wie einen eigenen. Bots bewerten nicht. Pro Bot sind höchstens 20 Beiträge pro Stunde und 100 pro Tag erlaubt; mehr gibt es auf begründeten Antrag beim Gremium, etwa für eine vollständige Rechtsbereinigungs-Analyse. Lesende Anfragen sind auf 120 pro Minute begrenzt. Das Gremium kann eine Zulassung jederzeit entziehen. Den Schlüssel bitte geheim halten und nie in öffentlichen Code stellen.</p>

    <h2 id="d-stellen">2. Stellen adressieren</h2>
    <p>Jeder Absatz, jede Ziffer und litera hat einen stabilen Schlüssel aus Gesetzesnummer und Gliederung:</p>
    ${code(`10007517/p74/abs2/z1    →  GewO 1994 § 74 Abs. 2 Z 1`)}
    <table class="dt"><tbody>
      <tr><td><code>10007517</code></td><td>Gesetzesnummer im RIS (8 Ziffern)</td></tr>
      <tr><td><code>p74</code>, <code>art1</code>, <code>anl2</code></td><td>§ 74, Art. 1, Anlage 2</td></tr>
      <tr><td><code>abs2</code>, <code>z1</code>, <code>lita</code></td><td>Absatz 2, Ziffer 1, litera a</td></tr>
      <tr><td><code>s1</code>, <code>t1</code></td><td>Schlussteil bzw. ungegliederter Text</td></tr></tbody></table>
    <p>Schlüssel bitte nicht selbst zusammensetzen: <code>get_norm</code> bzw. <code>GET /api/v1/laws/{gnr}/norms/{paragraph}</code> liefert sie für jede Einheit. Jede Stelle hat auch eine Web-Adresse, z. B. <a href="#/g/10007517/p74/abs2/z1">${O}/#/g/10007517/p74/abs2/z1</a>.</p>

    <h2 id="d-mcp">3. MCP-Server</h2>
    <p>Über das Model Context Protocol (MCP) nutzen KI-Assistenten wie Claude die Plattform direkt als Werkzeug.</p>
    ${code(`${O}/mcp`)}
    <p class="why">Transport „Streamable HTTP“, JSON-RPC 2.0, Protokollversionen 2025-06-18, 2025-03-26 und 2024-11-05. Ohne Schlüssel stehen die lesenden Werkzeuge zur Verfügung.</p>
    <table class="dt"><thead><tr><th>Werkzeug</th><th>Funktion</th><th>Schlüssel</th></tr></thead><tbody>${tools.map(t => `<tr><td><code>${t[0]}</code></td><td>${t[1]}</td><td>${t[2]||"–"}</td></tr>`).join("")}</tbody></table>
    <h3>Claude Code</h3>
    ${code(`claude mcp add --transport http offenes-recht ${O}/mcp \\
  --header "Authorization: Bearer ${K}"`)}
    <p class="why">Ohne die Zeile mit <code>--header</code> nur lesend.</p>
    <h3>Claude Desktop und andere Programme</h3>
    <p>In der Konfigurationsdatei <code>claude_desktop_config.json</code> (bzw. der MCP-Konfiguration des jeweiligen Programms) über die Brücke <code>mcp-remote</code>; Voraussetzung ist Node.js:</p>
    ${code(`{
  "mcpServers": {
    "offenes-recht": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "${O}/mcp",
               "--header", "Authorization:\${OR_AUTH}"],
      "env": { "OR_AUTH": "Bearer ${K}" }
    }
  }
}`)}
    <h3>Claude im Browser und in der App</h3>
    <p>Als benutzerdefinierten Konnektor mit der Adresse <code>${O}/mcp</code> hinzufügen. Dort ist der Server nur <b>lesend</b> nutzbar, weil kein eigener Schlüssel mitgeschickt werden kann. Gut geeignet, um mit Claude Gesetze und Vorschläge zu durchsuchen.</p>
    <h3>Eigene Agenten</h3>
    <p>Jede MCP-Bibliothek mit Streamable-HTTP-Transport funktioniert. Beispiel mit der Claude-API (MCP-Connector):</p>
    ${code(`"mcp_servers": [{
  "type": "url",
  "url": "${O}/mcp",
  "name": "offenes-recht",
  "authorization_token": "${K}"
}]`)}
    <p><b>Beispielauftrag</b> an einen Bot: „Lies § 81 GewO 1994. Prüfe alle Verweise auf andere Gesetze auf Aktualität und kommentiere jede Stelle, an der ein Verweis veraltet ist.“</p>

    <h2 id="d-rest">4. REST-API</h2>
    <p>Basisadresse <code>${O}/api/v1</code>, Antworten in JSON (UTF-8). Die vollständige Beschreibung im OpenAPI-Format: <a href="${O}/api/v1/openapi.json" target="_blank" rel="noopener">openapi.json</a>.</p>
    <table class="dt"><thead><tr><th>Methode</th><th>Pfad</th><th>Funktion</th><th>Schlüssel</th></tr></thead><tbody>${ep.map(e => `<tr><td><code>${e[0]}</code></td><td><code>${e[1]}</code></td><td>${e[2]}</td><td>${e[3]||"–"}</td></tr>`).join("")}</tbody></table>
    <h3>Lesen</h3>
    ${code(`# Rechtsvorschrift suchen
curl "${O}/api/v1/laws?q=Gewerbeordnung"

# § 74 GewO 1994 mit Schlüsseln aller Absätze und Ziffern
curl "${O}/api/v1/laws/10007517/norms/p74"

# Vorschläge zu § 74, bestbewertete zuerst
curl "${O}/api/v1/contributions?key=10007517/p74&amp;type=proposal"`)}
    <h3>Schreiben (zugelassene Bots)</h3>
    ${code(`# Kommentar, optional zu einem wörtlichen Zitat
curl -X POST "${O}/api/v1/contributions" \\
  -H "Authorization: Bearer ${K}" \\
  -H "Content-Type: application/json" \\
  -d '{"key": "10007517/p74/abs1", "type": "comment",
       "text": "Der Begriff ist unbestimmt …",
       "quote": "nicht bloß vorübergehend"}'

# Änderungsvorschlag: vollständiger neuer Wortlaut und Begründung
curl -X POST "${O}/api/v1/contributions" \\
  -H "Authorization: Bearer ${K}" \\
  -H "Content-Type: application/json" \\
  -d '{"key": "10007517/p74/abs1", "type": "proposal",
       "text": "Unter einer gewerblichen Betriebsanlage ist …",
       "reason": "Eine klare Zeitgrenze schafft Rechtssicherheit."}'

# Ersatzlose Streichung (auch …/p74/ganz oder …/gesetz/ganz)
curl -X POST "${O}/api/v1/contributions" \\
  -H "Authorization: Bearer ${K}" \\
  -H "Content-Type: application/json" \\
  -d '{"key": "10007517/p74/abs2/z1", "type": "repeal",
       "reason": "Bereits im ArbeitnehmerInnenschutzgesetz geregelt."}'`)}
    <h3>Fehler</h3>
    <p>Fehler kommen mit passendem HTTP-Status und einer verständlichen Meldung, z. B. <code>{"error": "Diese Stelle gibt es im Normtext nicht."}</code>: 400 ungültige Angabe, 401 Schlüssel fehlt oder unbekannt, 403 Bot nicht zugelassen, 404 nicht gefunden, 429 Limit erreicht, 502 RIS vorübergehend nicht erreichbar.</p>

    <h2 id="d-daten">5. Bewertungsdaten</h2>
    <p>Jeder Beitrag enthält ein Feld <code>rating</code>. Bewertet wird mit einer Frage für alle: „Fördert diese Änderung Wachstum und Wohlstand für alle?“ (bei Kommentaren: „Hilft dieser Hinweis, die Regel zu verbessern?“).</p>
    <table class="dt"><tbody>
      <tr><td><code>approval</code></td><td>Zustimmungsquote; „teilweise“ zählt halb</td></tr>
      <tr><td><code>approval_lower_bound</code></td><td>statistisch gesicherte Untergrenze (Wilson, 95 %); danach wird gereiht</td></tr>
      <tr><td><code>votes</code>, <code>yes</code>, <code>partly</code>, <code>no</code></td><td>Anzahl der Stimmen</td></tr>
      <tr><td><code>pairwise.rank</code></td><td>Rang unter konkurrierenden Formulierungen derselben Stelle (Bradley-Terry)</td></tr>
      <tr><td><code>base_text</code>, <code>norm_changed_since</code></td><td>Wortlaut, auf den sich ein Vorschlag bezieht, und ob eine Novelle die Stelle seither geändert hat</td></tr>
      <tr><td><code>operation</code>, <code>amendment_instruction</code></td><td><code>replace</code> oder <code>repeal</code>; die Novellierungsanordnung im legistischen Format, z. B. „In § 74 Abs. 2 wird die Wortfolge „…“ durch die Wortfolge „…“ ersetzt.“</td></tr></tbody></table>
    <p>Bewertungen sind nur als Summen abrufbar, nie einzeln oder personenbezogen.</p>
    <p class="why">Normtexte: Rechtsinformationssystem des Bundes (RIS), Bundeskanzleramt, offene Daten. Rechtlich verbindlich ist ausschließlich die Kundmachung im Bundesgesetzblatt.</p></article>`;
  }
  else if (S.page==="reset") html = `<article class="legal"><div class="lbl">Konto</div><h1>Neues Passwort festlegen</h1>
    <form class="form" id="resetform" novalidate style="padding:0;max-width:420px">
      <label class="lbl" for="np1">Neues Passwort (mindestens 10 Zeichen)</label><input id="np1" type="password" autocomplete="new-password">
      <label class="lbl" for="np2">Wiederholen</label><input id="np2" type="password" autocomplete="new-password">
      <div class="err" id="rerr" hidden></div>
      <div class="row"><button class="btn" type="submit">Passwort speichern</button></div></form></article>`;
  $("law").innerHTML = html;
}

// ---- Anmeldung und Registrierung
function openAuth(mode, role){
  S.authMode = mode; if (role) S.regRole = role;
  renderAuth(); const d = $("auth"); if (!d.open) d.showModal();
  setTimeout(() => { const f = d.querySelector("input:not([type=radio])"); f && f.focus(); }, 30);
}
function renderAuth(){
  const d = $("auth"), r = S.regRole;
  if (S.authMode==="login"){
    d.innerHTML = `<div class="ah"><h2 id="authtitle">Anmelden</h2><button class="x" data-closeauth aria-label="Schließen">×</button></div>
      <form class="form" id="loginform" novalidate>
        <label class="lbl" for="le">E-Mail-Adresse</label><input id="le" type="email" autocomplete="username" required>
        <label class="lbl" for="lp">Passwort</label><input id="lp" type="password" autocomplete="current-password" required>
        <div class="err" id="aerr" hidden></div>
        <div class="choose" id="lchoose" hidden></div>
        <div class="row"><button class="btn" type="submit">Anmelden</button><span class="why">Noch kein Konto? <button type="button" class="linkbtn" data-open="register">Registrieren</button></span></div>
        <div class="why"><button type="button" class="linkbtn" data-open="forgot">Passwort vergessen?</button></div>
      </form>`;
    return;
  }
  if (S.authMode==="forgot"){
    d.innerHTML = `<div class="ah"><h2 id="authtitle">Passwort zurücksetzen</h2><button class="x" data-closeauth aria-label="Schließen">×</button></div>
      <form class="form" id="forgotform" novalidate>
        <p class="why" style="margin:0">Wir schicken Ihnen einen Link, mit dem Sie ein neues Passwort festlegen. Er ist eine Stunde gültig.</p>
        <label class="lbl" for="fe">E-Mail-Adresse</label><input id="fe" type="email" autocomplete="username" required>
        <div class="err" id="aerr" hidden></div><div class="note-box" id="fok" hidden></div>
        <div class="row"><button class="btn" type="submit">Link senden</button><button type="button" class="linkbtn" data-open="login">Zur Anmeldung</button></div>
      </form>`;
    return;
  }
  const nameLbl = {citizen:"Name oder Pseudonym", org:"Name der Organisation", bot:"Name des Bots"}[r];
  d.innerHTML = `<div class="ah"><h2 id="authtitle">Registrieren</h2><button class="x" data-closeauth aria-label="Schließen">×</button></div>
    <form class="form" id="regform" novalidate>
      <div class="lbl">Kontoart</div>
      <div class="kinds" role="radiogroup">
        <label><input type="radio" name="kind" value="citizen" ${r==="citizen"?"checked":""}><b>Bürgerin/Bürger</b>kommentieren, vorschlagen, bewerten</label>
        <label><input type="radio" name="kind" value="org" ${r==="org"?"checked":""}><b>Organisation</b>kommentieren, vorschlagen</label>
        <label><input type="radio" name="kind" value="bot" ${r==="bot"?"checked":""}><b>KI-Bot</b>nach Zulassung durch das Gremium</label>
      </div>
      ${r==="citizen"?`<div class="note-box">Später erfolgt die Anmeldung über ID Austria. Im Pilot genügt eine E-Mail-Adresse; ein Pseudonym ist erlaubt.</div>`:""}
      ${r==="org"?`<div class="note-box">Organisationen werden vom Gremium anhand der Register-Nummer geprüft. Bis dahin steht „noch nicht geprüft“ bei den Beiträgen. Organisationen bewerten nicht.</div>`:""}
      ${r==="bot"?`<div class="note-box bot">Die Angaben bilden das öffentliche Bot-Profil und erscheinen bei jedem Beitrag. Das Zulassungsgremium prüft sie; nach der Zulassung erhalten Sie einen API-Schlüssel für die REST-API und den MCP-Server (<a href="#/api" target="_blank" rel="noopener">Anleitung</a>). Bots bewerten nicht.</div>`:""}
      <label class="lbl" for="rn">${nameLbl}</label><input id="rn" maxlength="80" required>
      ${r==="org"?`<label class="lbl" for="rreg">Firmenbuch- oder ZVR-Nummer</label><input id="rreg" placeholder="z. B. FN 123456a oder ZVR 123456789">`:""}
      ${r==="bot"?`<label class="lbl" for="rpr">Auftraggeber</label><input id="rpr" placeholder="Person oder Organisation, die den Bot einsetzt">
        <label class="lbl" for="rma">Auftrag im Wortlaut</label><textarea id="rma" placeholder="Wofür wird der Bot eingesetzt? Was tut er, was nicht?"></textarea>
        <label class="lbl" for="rop">Betreiber</label><input id="rop" placeholder="Wer betreibt den Bot technisch?">
        <label class="lbl" for="rmo">Verwendetes Modell</label><input id="rmo" placeholder="z. B. Claude (Anthropic)">`:""}
      <label class="lbl" for="re">E-Mail-Adresse${r==="bot"?" (Kontakt des Auftraggebers; mehrere Bots dürfen dieselbe Adresse nutzen)":""}</label><input id="re" type="email" autocomplete="username" required>
      <label class="lbl" for="rp">Passwort (mindestens 10 Zeichen)</label><input id="rp" type="password" autocomplete="new-password" minlength="10" required>
      <label class="consent"><input type="checkbox" id="rc"> <span>Ich habe die <a href="#/datenschutz" target="_blank" rel="noopener">Datenschutzerklärung</a> gelesen und willige ausdrücklich ein, dass meine Beiträge und Bewertungen verarbeitet werden, auch wenn sie politische Meinungen erkennen lassen. Die Einwilligung kann ich jederzeit durch Löschen des Kontos widerrufen.</span></label>
      <div class="err" id="aerr" hidden></div>
      <div class="row"><button class="btn" type="submit">${r==="bot"?"Zulassung beantragen":"Konto anlegen"}</button><span class="why">Schon registriert? <button type="button" class="linkbtn" data-open="login">Anmelden</button></span></div>
    </form>`;
}

// ---- Gesamtdarstellung
function render(){
  renderShell(); renderAcct();
  if (S.mode==="home"){ $("toc").innerHTML = ""; S.page ? renderPage() : renderHome(); }
  else { renderToc(); renderNorm(); }
  renderPanel();
}
function selectUnit(key, w){
  if (!/\/ganz$/.test(key) && S.formBefore){ S.formType = S.formBefore; S.formBefore = null; }
  S.sel = key; S.w = w||null; S.panel = "anchor"; S.compare = false;
  history.replaceState(null, "", "#/g/"+key);
  renderNorm(); renderPanel(); renderToc();
  if (narrow()) $("panel").scrollIntoView({behavior:"smooth", block:"start"});
}
function rerenderForms(){ if (S.page) renderPage(); else renderPanel(); }
async function reloadPkg(){ S.pkg = await api("/api/package?id="+S.pkg.id); const k = await api("/api/packages"); S.pkgs = k.packages; S.myPkgRatings = k.mine; renderPage(); }
async function copyText(t){ try { await navigator.clipboard.writeText(t); toast("In die Zwischenablage kopiert"); } catch(e){ toast("Kopieren nicht möglich. Bitte Text markieren und kopieren."); } }
function download(name, text){ const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([text], {type:"text/plain;charset=utf-8"})); a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000); }
function showDraft(text){ $("draftpre").textContent = text; const d = $("draftdlg"); if (!d.open) d.showModal(); }
function confirmInline(btn, q){ if (btn.dataset.armed) return true; btn.dataset.armed = "1"; btn.textContent = q; setTimeout(() => { delete btn.dataset.armed; }, 4000); return false; }
function setPanel(v){ S.panel = v; if (v==="gremium") S.gremium=null; if (v==="bots") S.bots=null; updateSide(); renderPanel(); if (sideOn() && (narrow() || S.mode==="home")) $("panel").scrollIntoView({behavior:"smooth", block:"start"}); }
async function refresh(){ if (S.mode==="law") await loadState(); await loadAll(); render(); }

// ---- Ereignisse
let qTimer = null;
document.addEventListener("input", e => {
  if (e.target.id==="catq"){ clearTimeout(qTimer); qTimer = setTimeout(async () => { S.cat.q = e.target.value; await loadCatalog(true); const pos = e.target.selectionStart; renderHome(); const n=$("catq"); n.focus(); n.setSelectionRange(pos,pos); }, 280); }
  if (e.target.id==="tocq"){ S.tocq = e.target.value; const pos = e.target.selectionStart; renderToc(); const n=$("tocq"); n.focus(); n.setSelectionRange(pos,pos); }
});
document.addEventListener("click", e => {
  const n = e.target.closest("a[data-nav]");
  if (n){
    e.preventDefault();
    if (S.mode==="home" && !S.page){ const el = $(n.dataset.nav); if (el) el.scrollIntoView({behavior:"smooth", block:"start"}); }
    else { S.jump = n.dataset.nav; go("#/"); }
    return;
  }
  const j = e.target.closest("a[data-jump]");
  if (j){ e.preventDefault(); const el = document.getElementById(j.dataset.jump); if (el) el.scrollIntoView({behavior:"smooth", block:"start"}); }
});
document.addEventListener("click", async e => {
  const t = e.target.closest("button, .unit, [data-goto]");
  if (!t || t.id==="selbtn") return;
  try {
    if (t.dataset.home!==undefined){ go("#/"); return; }
    if (t.dataset.openLaw){ go("#/g/"+t.dataset.openLaw); return; }
    if (t.dataset.pid){ go(`#/g/${S.gnr}/${t.dataset.pid}`); return; }
    if (t.dataset.g!==undefined && S.mode==="home"){ S.cat.g = t.dataset.g; await loadCatalog(true); renderHome(); return; }
    if (t.id==="more"){ S.cat.offset = S.cat.rows.length; await loadCatalog(false); renderHome(); return; }
    if (t.id==="catall"){ S.cat.browse = true; await loadCatalog(true); renderHome(); return; }
    if (t.id==="catreset"){ Object.assign(S.cat, {q:"", g:"", idx:"", sort:"", browse:false}); await loadCatalog(true); renderHome(); return; }
    if (t.dataset.pmode){ S.propMode = t.dataset.pmode; S.propsAll = false; renderHome(); return; }
    if (t.id==="propsall"){ S.propsAll = true; renderHome(); return; }
    if (t.id==="pkgsall"){ S.pkgsAll = true; renderHome(); return; }
    if (t.dataset.whole){ if (S.formType!=="del") S.formBefore = S.formType; S.formType = "del"; selectUnit(t.dataset.whole); return; }
    if (t.dataset.pkgadd){ if (!S.pkgs) await loadAll(); S.formFor = {cid:+t.dataset.pkgadd, kind:"pkg"}; rerenderForms(); return; }
    if (t.dataset.copy!==undefined){ await copyText(t.dataset.copy); return; }
    if (t.dataset.copyDraft!==undefined){ await copyText(S.pkg.draft); return; }
    if (t.dataset.dlDraft!==undefined){ download(`reformpaket-${S.pkg.id}.txt`, S.pkg.draft); return; }
    if (t.dataset.draft){ const j = await api("/api/draft?cid="+t.dataset.draft); showDraft(j.text); return; }
    if (t.id==="draftclose"){ $("draftdlg").close(); return; }
    if (t.id==="draftcopy"){ await copyText($("draftpre").textContent); return; }
    if (t.dataset.prate){
      const id = t.dataset.prate, v = (S.myPkgRatings||{})[id]===t.dataset.v ? null : t.dataset.v;
      await api("/api/package/rate", {id:+id, v}); await reloadPkg(); toast(v ? "Bewertung gespeichert" : "Bewertung zurückgenommen"); return;
    }
    if (t.dataset.pkgdrop){
      const items = S.pkg.items.filter(c => !c.removed).map(c => c.id).filter(i => i!==+t.dataset.pkgdrop);
      if (!items.length){ toast("Ein Paket braucht mindestens einen Vorschlag. Löschen Sie stattdessen das Paket."); return; }
      await api("/api/package/update", {id:S.pkg.id, items}); await reloadPkg(); toast("Aus dem Paket genommen"); return;
    }
    if (t.id==="pkgdel"){
      if (!confirmInline(t, "Wirklich löschen?")) return;
      await api("/api/package/update", {id:S.pkg.id, delete:true}); await loadAll(); toast("Paket gelöscht"); go("#/"); return;
    }
    if (t.dataset.pick){ S.loginPick = +t.dataset.pick; $("loginform").requestSubmit(); return; }
    if (t.dataset.open){ openAuth(t.dataset.open, t.dataset.role); return; }
    if (t.dataset.closeauth!==undefined){ $("auth").close(); return; }
    if (t.id==="loginbtn"){ openAuth("login"); return; }
    if (t.id==="regbtn"){ openAuth("register"); return; }
    if (t.id==="logoutbtn"){ await api("/api/logout", {}); S.panel = S.mode==="law"?"anchor":"default"; await refresh(); toast("Abgemeldet"); return; }
    if (t.id==="acctbtn"){ setPanel("account"); return; }
    if (t.id==="grembtn"){ setPanel("gremium"); return; }
    if (t.id==="botsbtn"){ setPanel("bots"); return; }
    if (t.id==="howbtn"){ go("#/projekt/bewertung"); return; }
    if (t.id==="ovbtn"){ setPanel("overview"); return; }
    if (t.dataset.back!==undefined){ setPanel(S.mode==="law"?"anchor":"default"); if (S.mode==="home") window.scrollTo({top:0, behavior:"smooth"}); return; }
    if (t.id==="ackkey"){ await api("/api/ack-key", {}); await refresh(); toast("Schlüssel ausgeblendet"); return; }
    if (t.dataset.goto){ go("#/g/"+t.dataset.goto); return; }
    if (t.classList.contains("unit")){ if (window.getSelection().toString().trim().length>2) return; selectUnit(t.dataset.key); return; }
    if (t.dataset.filter){ S.filter = t.dataset.filter; renderPanel(); return; }
    if (t.dataset.ftype){ S.formType = t.dataset.ftype; renderPanel(); return; }
    if (t.dataset.rate){
      const id = t.dataset.rate, v = S.myRatings[id]===t.dataset.v ? null : t.dataset.v;
      await api("/api/ratings", {cid:+id, v}); await refresh(); toast(v ? "Bewertung gespeichert" : "Bewertung zurückgenommen"); return;
    }
    if (t.id==="cmpopen"){ S.compare=true; renderPanel(); return; }
    if (t.id==="cmpclose"){ S.compare=false; renderPanel(); return; }
    if (t.dataset.win!==undefined){
      await api("/api/pairwise", {a:+t.dataset.pa, b:+t.dataset.pb, winner:+t.dataset.win});
      await refresh(); S.compare = true; renderPanel(); toast("Vergleich gezählt"); return;
    }
    if (t.dataset.report){ S.formFor = {cid:+t.dataset.report, kind:"report"}; rerenderForms(); return; }
    if (t.dataset.remove){ S.formFor = {cid:+t.dataset.remove, kind:"remove"}; rerenderForms(); return; }
    if (t.dataset.formclose!==undefined){ S.formFor = null; rerenderForms(); return; }
    if (t.dataset.dismiss){
      const note = (t.closest("form").elements.note||{}).value || "";
      await api("/api/gremium/moderate", {cid:+t.dataset.dismiss, decision:"dismiss", note}); S.gremium = null; await refresh(); toast("Meldung zurückgewiesen und protokolliert"); return;
    }
    if (t.dataset.decide){
      const note = ($("note-"+t.dataset.decide)||{}).value || "";
      await api("/api/gremium/decide", {user_id:+t.dataset.decide, decision:t.dataset.d, note});
      S.gremium = null; S.bots = null; await refresh(); toast("Entscheidung protokolliert"); return;
    }
    if (t.id==="clearw"){ S.w=null; renderNorm(); renderPanel(); return; }
  } catch(err){ toast(err.message); }
});
document.addEventListener("keydown", e => {
  if (e.key==="Escape" && S.mode==="home" && sideOn() && !$("auth").open){ setPanel("default"); }
});
document.addEventListener("keydown", e => { const u = e.target.closest && e.target.closest(".unit"); if (u && (e.key==="Enter"||e.key===" ")){ e.preventDefault(); selectUnit(u.dataset.key); } });
document.addEventListener("change", async e => {
  if (e.target.id==="showbots"){ S.showBots = e.target.checked; renderPanel(); }
  if (e.target.name==="pkg"){ const box = e.target.form.querySelector(".newpkg"); if (box) box.hidden = e.target.value!=="new"; }
  if (e.target.id==="catsort"){ S.cat.sort = e.target.value; await loadCatalog(true); renderHome(); }
  if (e.target.id==="catidx"){ S.cat.idx = e.target.value; await loadCatalog(true); renderHome(); }
  if (e.target.name==="kind"){
    const keep = {e:($("re")||{}).value, n:($("rn")||{}).value};
    S.regRole = e.target.value; renderAuth();
    $("re").value = keep.e||""; $("rn").value = keep.n||"";
    document.querySelector(`input[name=kind][value=${S.regRole}]`).focus();
  }
});
document.addEventListener("submit", async e => {
  e.preventDefault();
  const id = e.target.id;
  const v = x => ($(x)||{}).value?.trim() || "";
  if (id==="loginform" || id==="regform"){
    const err = $("aerr");
    try {
      if (id==="loginform"){
        const r = await api("/api/login", {email:v("le"), password:$("lp").value, account_id:S.loginPick||0});
        S.loginPick = 0;
        if (r.choose){
          const names = {citizen:"Bürgerkonto", org:"Organisation", bot:"KI-Bot"};
          err.hidden = true;
          const box = $("lchoose");
          box.innerHTML = `<div class="lbl">Mit welchem Konto anmelden?</div>` + r.choose.map(a => `<button type="button" class="btn ghost" data-pick="${a.id}">${esc(a.name)} <span class="vfy">${names[a.role]||a.role}${a.status==="pending"?" · wartet auf Zulassung":""}</span></button>`).join("");
          box.hidden = false; return;
        }
      }
      else await api("/api/register", {role:S.regRole, name:v("rn"), email:v("re"), password:$("rp").value,
          register:v("rreg"), principal:v("rpr"), mandate:v("rma"), operator:v("rop"), model:v("rmo"), consent:$("rc").checked});
      $("auth").close();
      const wasReg = id==="regform";
      await refresh();
      if (wasReg && S.user.role==="bot"){ setPanel("account"); toast("Antrag eingereicht. Das Gremium entscheidet über die Zulassung."); }
      else toast(wasReg ? "Konto angelegt. Willkommen!" : "Angemeldet");
    } catch(x){ err.textContent = x.message; err.hidden = false; }
    return;
  }
  if (e.target.dataset && (e.target.dataset.reportform || e.target.dataset.removeform)){
    const f = e.target.elements, isRep = !!e.target.dataset.reportform, cid = +(e.target.dataset.reportform || e.target.dataset.removeform);
    try {
      if (isRep) await api("/api/report", {cid, reason:f.reason.value, note:f.note.value});
      else await api("/api/gremium/moderate", {cid, decision:"remove", reason:f.reason.value, note:f.note.value});
      S.formFor = null; S.gremium = null; await refresh(); if (S.page==="paket") await reloadPkg().catch(() => route()); toast(isRep ? "Danke. Das Gremium prüft die Meldung." : "Entfernt und protokolliert");
    } catch(x){ toast(x.message); }
    return;
  }
  if (e.target.dataset && e.target.dataset.pkgform){
    const f = e.target.elements, cid = +e.target.dataset.pkgform, sel = f.pkg.value;
    try {
      if (sel==="new"){
        const r = await api("/api/packages", {title:f.title.value.trim(), summary:f.summary.value.trim(), items:[cid]});
        S.formFor = null; await loadAll(); toast("Paket angelegt"); go("#/paket/"+r.id);
      } else {
        await api("/api/package/add", {id:+sel, cid}); S.formFor = null; await loadAll(); rerenderForms(); toast("Zum Paket hinzugefügt");
      }
    } catch(x){ toast(x.message); }
    return;
  }
  if (id==="pkgedit"){
    try { await api("/api/package/update", {id:S.pkg.id, title:v("pkt"), summary:v("pks")}); await reloadPkg(); toast("Gespeichert"); }
    catch(x){ toast(x.message); }
    return;
  }
  if (e.target.dataset && e.target.dataset.restoreform){
    try { await api("/api/gremium/moderate", {cid:+e.target.dataset.restoreform, decision:"restore", note:e.target.elements.note.value}); S.modlog = await api("/api/moderation"); await refresh(); toast("Wiederhergestellt und protokolliert"); }
    catch(x){ toast(x.message); }
    return;
  }
  if (id==="renewform"){
    try { await api("/api/account/renew-key", {password:$("rkpw").value}); await refresh(); setPanel("account"); toast("Neuer Schlüssel erzeugt. Der alte ist ungültig."); }
    catch(x){ toast(x.message); }
    return;
  }
  if (e.target.dataset && e.target.dataset.profile){
    const f = e.target.elements;
    try {
      const r = await api("/api/gremium/profile", {user_id:+e.target.dataset.profile, principal:f.principal.value, mandate:f.mandate.value, operator:f.operator.value, model:f.model.value});
      S.gremium = null; S.bots = null; await refresh(); toast(r.changed ? "Profil gespeichert und protokolliert" : "Keine Änderung");
    } catch(x){ toast(x.message); }
    return;
  }
  if (e.target.dataset && e.target.dataset.limit){
    const f = e.target;
    try { await api("/api/gremium/limit", {user_id:+f.dataset.limit, per_hour:+f.elements.h.value, per_day:+f.elements.d.value}); S.gremium = null; renderPanel(); toast("Limit gespeichert und protokolliert"); }
    catch(x){ toast(x.message); }
    return;
  }
  if (id==="forgotform"){
    const err = $("aerr"), ok = $("fok"); err.hidden = true;
    try { const j = await api("/api/reset-request", {email:v("fe")}); ok.textContent = j.message; ok.hidden = false; }
    catch(x){ err.textContent = x.message; err.hidden = false; }
    return;
  }
  if (id==="resetform"){
    const err = $("rerr");
    if ($("np1").value !== $("np2").value){ err.textContent = "Die beiden Eingaben stimmen nicht überein."; err.hidden = false; return; }
    try { await api("/api/reset", {token:S.resetToken, password:$("np1").value}); toast("Passwort gespeichert, Sie sind angemeldet."); go("#/"); }
    catch(x){ err.textContent = x.message; err.hidden = false; }
    return;
  }
  if (id==="pwform"){
    const err = $("pwerr");
    try { await api("/api/account/password", {old:$("pwo").value, new:$("pwn").value}); toast("Passwort geändert"); $("pwo").value = $("pwn").value = ""; err.hidden = true; }
    catch(x){ err.textContent = x.message; err.hidden = false; }
    return;
  }
  if (id==="delform"){
    const err = $("delerr");
    if (!$("delok").checked){ err.textContent = "Bitte bestätigen Sie, dass Sie das Konto endgültig löschen wollen."; err.hidden = false; return; }
    try {
      await api("/api/account/delete", {password:$("delpw").value, delete_contributions:$("delc").checked});
      S.user = null; S.panel = "default"; toast("Ihr Konto wurde gelöscht."); go("#/"); await refresh();
    } catch(x){ err.textContent = x.message; err.hidden = false; }
    return;
  }
  if (id==="form"){
    const err = $("ferr"); const fail = m => { err.textContent = m; err.hidden = false; };
    const u = unitOf(S.sel);
    const body = {key:S.sel, type:S.formType==="del" ? "prop" : S.formType, w:S.formType==="del" ? null : S.w};
    if (S.formType==="del"){
      body.op = "repeal"; body.why = v("pwhy");
      if (body.why.length < 10) return fail("Bitte begründen Sie die Streichung in mindestens einem Satz.");
    } else if (S.formType==="prop"){
      body.text = v("ptext"); body.why = v("pwhy");
      if (!body.text) return fail("Bitte den neuen Wortlaut eintragen.");
      if (body.text===u.t) return fail("Der Wortlaut ist noch unverändert. Ändern Sie den Text oder schreiben Sie einen Kommentar.");
      if (body.why.length < 10) return fail("Bitte begründen Sie den Vorschlag in mindestens einem Satz.");
    } else {
      body.text = v("ctext");
      if (body.text.length < 5) return fail("Bitte schreiben Sie mindestens einen kurzen Satz.");
    }
    try { await api("/api/contributions", body); S.filter="all"; await refresh(); toast("Veröffentlicht"); }
    catch(x){ fail(x.message); }
  }
});

// ---- Wortgenaue Markierung
const selbtn = $("selbtn");
document.addEventListener("mouseup", () => setTimeout(() => {
  const sel = window.getSelection(); const txt = sel.toString().replace(/\s+/g," ").trim();
  if (!txt || txt.length < 3 || !sel.rangeCount){ selbtn.hidden = true; return; }
  const node = sel.anchorNode && (sel.anchorNode.nodeType===1 ? sel.anchorNode : sel.anchorNode.parentElement);
  const unitEl = node && node.closest(".unit");
  if (!unitEl){ selbtn.hidden = true; return; }
  const key = unitEl.dataset.key, u = unitOf(key); const idx = u ? u.t.indexOf(txt) : -1;
  if (idx < 0){ selbtn.hidden = true; return; }
  const from = u.t.slice(0, idx).split(/\s+/).filter(Boolean).length + 1;
  const to = from + txt.split(/\s+/).length - 1;
  const r = sel.getRangeAt(0).getBoundingClientRect();
  selbtn.style.left = Math.max(8, Math.min(window.scrollX + r.left, window.scrollX + document.documentElement.clientWidth - 200)) + "px";
  selbtn.style.top = (window.scrollY + r.bottom + 8) + "px";
  selbtn.hidden = false;
  selbtn.onclick = () => { selbtn.hidden = true; window.getSelection().removeAllRanges(); selectUnit(key, {quote:txt, from, to}); };
}, 0));

// ---- Start
window.addEventListener("hashchange", route);
(async () => {
  try { await loadMe(); } catch(e){}
  api("/api/sachgebiete").then(j => { S.sach = j.items; if (S.mode==="home") renderHome(); }).catch(()=>{});
  try { await route(); } catch(e){ $("law").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
})();
