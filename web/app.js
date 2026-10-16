/*
 * MDP 3.0 dashboard UI.
 *
 * Three sources, one set of views (ladder, chart, tape, feeds, inspector,
 * definitions):
 *   simulated  a session from /api/session (session.py). Tuning re-runs the
 *              in-page engine (engine.js — Python-identical) at once, then the
 *              server re-runs the session through the real publisher, loss
 *              model and feed handler; that response replaces the preview.
 *   capture    a pcap from /api/capture (picked or uploaded), decoded the same way.
 *   live       /api/live server-sent events: the feed handler on real multicast.
 *
 * A session is {instruments[], frames[{t, i:[{m,bb,bo,b,o,tr,s,r}]}], packets,
 * events, channels, samples, schema}. Live snapshots are folded into the same
 * frame shape, so every view renders any source.
 *
 * Everything that reaches innerHTML goes through esc(): captures carry
 * arbitrary bytes, symbols included.
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
  const STATIC = location.protocol === "file:";
  const STATE = ["live", "recovering", "waiting"];
  const MAX_PRODUCTS = 4;
  const LIVE_HISTORY = 1500;                      // 5 Hz x 5 min
  const LANES = [["I", "A"], ["I", "B"], ["S", "A"], ["S", "B"], ["N", "A"], ["N", "B"]];
  const KIND_NAME = { I: "Incremental", S: "Snapshot", N: "Instrument" };
  const PRESETS = {
    calm: { volatility: 0.25, reversion: 0.02, jump_prob: 0, jump_size: 8, loss_a: 0, loss_b: 0 },
    normal: { volatility: 0.6, reversion: 0.004, jump_prob: 0, jump_size: 8, loss_a: 0, loss_b: 0 },
    volatile: { volatility: 1.2, reversion: 0.002, jump_prob: 0, jump_size: 8 },
    jumpy: { volatility: 0.6, reversion: 0.004, jump_prob: 0.04, jump_size: 12 },
    lossy: { loss_a: 0.1, loss_b: 0.1 },
  };

  // ------------------------------------------------------------------ state ----
  const initial = JSON.parse($("session-data").textContent || "null");
  const query = new URLSearchParams(location.search);
  const st = {
    mode: query.get("mode") || "simulated",
    sim: { session: initial, preview: null, params: null, prior: {} },
    cap: { session: null, file: null, src: null, list: null },
    live: { es: null, hist: [], instruments: [], channels: [], events: [], schema: null,
            products: (query.get("products") || query.get("product") || "ES").toUpperCase()
              .split(",").filter(Boolean).slice(0, MAX_PRODUCTS), connected: false, packets: 0 },
    sel: 0, cur: 0, playing: false, speed: 1, timer: null, inspTpl: null,
    pending: null, debounce: null, cum: null,
  };

  // ---------------------------------------------------------------- helpers ----
  function parseMap(s) {
    const out = {};
    if (!s) return out;
    for (const part of String(s).split(",")) {
      const [k, v] = part.split("=");
      if (k && v !== undefined && v !== "" && isFinite(+v)) out[k.trim().toUpperCase()] = +v;
    }
    return out;
  }
  const mapStr = (m) => Object.entries(m).map(([k, v]) => `${k}=${v}`).join(",");
  const num = (v, d) => (v === undefined || v === null || v === "" || !isFinite(+v) ? d : +v);

  function paramsFromSession(s) {
    const q = (s && s.source && s.source.params) || {};
    const fromUrl = (k) => query.get(k);
    const pick = (k, d) => num(fromUrl(k) ?? q[k], d);
    return {
      products: String(fromUrl("products") || fromUrl("product") || q.products || "ES")
        .toUpperCase().split(",").filter(Boolean).slice(0, MAX_PRODUCTS),
      schema: fromUrl("schema") || q.schema || (s && s.schema && s.schema.name) || "cme",
      steps: pick("steps", 400), depth: pick("depth", 10),
      volatility: pick("volatility", 0.6), reversion: pick("reversion", 0.004),
      jump_prob: pick("jump_prob", 0), jump_size: pick("jump_size", 8), seed: pick("seed", 20260820),
      loss_a: pick("loss_a", 0), loss_b: pick("loss_b", 0),
      snapshot_every: pick("snapshot_every", 10), secdef_every: pick("secdef_every", 50),
      feeds: String(fromUrl("feeds") || q.feeds || "AB").toUpperCase(),
      start: parseMap(fromUrl("start") ?? q.start), low: parseMap(fromUrl("low") ?? q.low),
      high: parseMap(fromUrl("high") ?? q.high), band: parseMap(fromUrl("band") ?? q.band),
    };
  }

  function toQuery(p) {
    const q = new URLSearchParams();
    q.set("products", p.products.join(","));
    q.set("schema", p.schema);
    for (const k of ["steps", "depth", "volatility", "reversion", "jump_prob", "jump_size",
                     "seed", "loss_a", "loss_b", "snapshot_every", "secdef_every", "feeds"]) {
      q.set(k, p[k]);
    }
    for (const k of ["start", "low", "high", "band"]) {
      const s = mapStr(p[k]);
      if (s) q.set(k, s);
    }
    return q;
  }

  /* Mirrors SessionParams.simulator_command() in session.py. */
  function cliString(p) {
    const g = (x) => String(+(+x).toPrecision(6));
    const parts = ["python3 simulator.py", `--product ${p.products.join(",")}`,
      `--schema ${p.schema}`, "--duration 3600 --rate 10",
      `--volatility ${g(p.volatility)}`, `--reversion ${g(p.reversion)}`];
    if (p.jump_prob) parts.push(`--jump-prob ${g(p.jump_prob)} --jump-size ${g(p.jump_size)}`);
    for (const [flag, m] of [["--start", p.start], ["--low", p.low], ["--high", p.high], ["--band", p.band]]) {
      const e = Object.entries(m);
      if (!e.length) continue;
      parts.push(`${flag} ${p.products.length === 1 ? g(e[0][1]) : e.map(([c, v]) => `${c}=${g(v)}`).join(",")}`);
    }
    if (p.loss_a) parts.push(`--loss-a ${g(p.loss_a)}`);
    if (p.loss_b) parts.push(`--loss-b ${g(p.loss_b)}`);
    if (p.feeds !== "AB") parts.push(`--feeds ${p.feeds}`);
    parts.push(`--depth ${p.depth} --seed ${p.seed}`);
    return parts.join(" ");
  }

  // ---------------------------------------------------------------- sources ----
  function session() {
    if (st.mode === "simulated") return st.sim.session;
    if (st.mode === "capture") return st.cap.session;
    return null;
  }
  function instruments() {
    if (st.mode === "live") return st.live.instruments;
    const s = session();
    return s ? s.instruments : [];
  }
  function frames() {
    if (st.mode === "live") return st.live.hist;
    if (st.mode === "simulated" && st.sim.preview) return st.sim.preview;
    const s = session();
    return s ? s.frames : [];
  }
  const inst = () => instruments()[st.sel] || null;
  const decimalsOf = (i) => (i && i.definition && i.definition.decimals != null ? i.definition.decimals
                             : i && i.decimals != null ? i.decimals : 2);
  const tickOf = (i) => (i && i.definition && i.definition.tick ? i.definition.tick : i ? i.tick : 0.01);
  const fmt = (i, p, extra) => (p == null ? "—" : Number(p).toFixed(decimalsOf(i) + (extra || 0)));
  const row = (k, idx) => { const f = frames()[k]; return f ? f.i[idx ?? st.sel] : null; };

  function stepMs() {
    const s = session();
    return (s && s.stepMs) || 100;
  }

  /* Cumulative packet/event counters aligned with the frames (sim + capture). */
  function buildCumulative() {
    const s = session();
    st.cum = null;
    if (!s || st.mode === "live") return;
    const fr = s.frames, pk = s.packets, ev = s.events;
    const sent = new Array(fr.length), bytes = new Array(fr.length), drop = new Array(fr.length),
          rec = new Array(fr.length), gaps = new Array(fr.length);
    let p = 0, e = 0, a = 0, b = 0, d = 0, r = 0, g = 0;
    for (let k = 0; k < fr.length; k++) {
      const t = fr[k].t;
      while (p < pk.length && pk[p][0] <= t) {
        if (pk[p][6] === "drop") d++; else { a++; b += pk[p][5]; }
        p++;
      }
      while (e < ev.length && ev[e].t <= t) {
        if (ev[e].kind === "recovered") r++;
        if (ev[e].kind === "gap" && ev[e].text.startsWith("ch ")) g++;
        e++;
      }
      sent[k] = a; bytes[k] = b; drop[k] = d; rec[k] = r; gaps[k] = g;
    }
    st.cum = { sent, bytes, drop, rec, gaps };
  }

  // ------------------------------------------------------------------- status --
  function setStatus(text, busy) {
    const el = $("status");
    el.textContent = text || "";
    el.classList.toggle("busy", !!busy);
  }
  function banner(text, kind) {
    const el = $("banner");
    el.hidden = !text;
    el.className = "banner" + (kind ? " " + kind : "");
    el.textContent = text || "";
  }

  async function getJSON(url, opts) {
    const r = await fetch(url, opts);
    let body = null;
    try { body = await r.json(); } catch (_) { /* non-JSON error page */ }
    if (!r.ok) throw new Error((body && body.error) || `${r.status} ${r.statusText}`);
    return body;
  }

  // ------------------------------------------------------- simulated: params ---
  function priorCloses(s) {
    for (const i of (s && s.instruments) || []) {
      if (i.startSource === "priorClose" && st.sim.prior[i.code] == null) st.sim.prior[i.code] = i.start;
    }
  }

  /* Instant preview: re-run each instrument's engine in the page. */
  function preview() {
    const s = st.sim.session, p = st.sim.params;
    if (!s || !window.MDPEngine) return;
    const insts = s.instruments;
    if (insts.length !== p.products.length || insts.some((i, k) => i.code !== p.products[k])) return;
    const per = insts.map((i, k) => {
      const low = p.low[i.code] ?? i.low, high = p.high[i.code] ?? i.high;
      let start = p.start[i.code];
      if (start === undefined) start = (i.startSource === "center" ? null : i.start);
      return MDPEngine.simulate({ low, high, tick: i.tick, start, depth: p.depth,
        volatility: p.volatility, reversion: p.reversion, jumpProb: p.jump_prob,
        jumpSize: p.jump_size, seed: p.seed + k, steps: p.steps });
    });
    const out = [];
    for (let k = 0; k < p.steps; k++) {
      out.push({ t: k * stepMs() + stepMs() - 1e-6, i: per.map((r) => r.frames[k]) });
    }
    insts.forEach((i, k) => { i.low = per[k].low; i.high = per[k].high; i.center = per[k].center; });
    st.sim.preview = out;
  }

  function changed(opts) {
    const p = st.sim.params;
    if (opts && opts.preview) preview();
    history.replaceState(null, "", "?" + toQuery(p).toString());
    syncTuner();
    renderAll();
    if (STATIC) { setStatus(st.sim.preview ? "preview (static page: no server to re-run the wire)" : ""); return; }
    setStatus(st.sim.preview ? "preview · re-running on the server…" : "re-running on the server…", true);
    clearTimeout(st.debounce);
    st.debounce = setTimeout(refreshSim, opts && opts.now ? 0 : 350);
  }

  async function refreshSim() {
    if (st.pending) st.pending.abort();
    const ctl = (st.pending = new AbortController());
    const t0 = performance.now();
    try {
      const s = await getJSON("api/session?" + toQuery(st.sim.params).toString(), { signal: ctl.signal });
      if (ctl.signal.aborted) return;
      st.sim.session = s;
      st.sim.preview = null;
      priorCloses(s);
      banner("");
      setStatus(`wire session · ${s.schema.label} · ${Math.round(performance.now() - t0)} ms`);
      if (st.sel >= s.instruments.length) st.sel = 0;
      onSessionChanged();
    } catch (e) {
      if (e.name === "AbortError") return;
      banner(`Could not build the session: ${e.message}`);
      setStatus("");
    } finally {
      if (st.pending === ctl) st.pending = null;
    }
  }

  // ------------------------------------------------------------ tuner wiring ---
  const SLIDERS = [
    ["cVolatility", "vVolatility", "volatility", (v) => v.toFixed(2) + " ticks", true],
    ["cReversion", "vReversion", "reversion", (v) => v.toFixed(3), true],
    ["cJumpProb", "vJumpProb", "jump_prob", (v) => (v * 100).toFixed(1) + "% / step", true],
    ["cJumpSize", "vJumpSize", "jump_size", (v) => v.toFixed(1) + " ticks", true],
    ["cLossA", "vLossA", "loss_a", (v) => (v * 100).toFixed(0) + "%", false],
    ["cLossB", "vLossB", "loss_b", (v) => (v * 100).toFixed(0) + "%", false],
    ["cSnap", "vSnap", "snapshot_every", (v) => (v ? `every ${v} steps · ${(v * stepMs() / 1000).toFixed(1)} s` : "off"), false],
    ["cDefs", "vDefs", "secdef_every", (v) => (v ? `every ${v} steps · ${(v * stepMs() / 1000).toFixed(1)} s` : "off"), false],
  ];

  function syncTuner() {
    const p = st.sim.params;
    if (!p) return;
    for (const [c, v, key, f] of SLIDERS) { $(c).value = p[key]; $(v).textContent = f(+p[key]); }
    const i = inst();
    const code = i ? i.code : p.products[0];
    $("startSym").textContent = code || "";
    $("cStart").value = p.start[code] ?? (i && i.start != null ? i.start : "");
    $("cLow").value = p.low[code] ?? (i ? i.low : "");
    $("cHigh").value = p.high[code] ?? (i ? i.high : "");
    const step = i ? tickOf(i) : 0.25;
    $("cStart").step = $("cLow").step = $("cHigh").step = step;
    $("cSeed").value = p.seed;
    $("cFeeds").value = p.feeds;
    const prior = st.sim.prior[code];
    $("resetStart").hidden = prior == null;
    $("resetStart").disabled = (p.start[code] ?? (i && i.start)) === prior;
    $("resetStart").title = prior == null ? "" : `Back to the prior close ${prior}`;
    const cli = cliString(p);
    $("cliText").innerHTML = esc(cli).replace(/--(\S+) (\S+)/g, "--$1 <b>$2</b>");
    const dl = $("dlPcap");
    if (STATIC) { dl.setAttribute("aria-disabled", "true"); dl.removeAttribute("href");
                  dl.title = "A static page has no server; use simulator.py --pcap"; }
    else { dl.href = "api/session.pcap?" + toQuery(p).toString(); dl.removeAttribute("aria-disabled"); }
    for (const id of ["cLossA", "cLossB", "cSnap", "cDefs", "cFeeds"]) $(id).disabled = STATIC;
  }

  function wireTuner() {
    for (const [c, , key, , pre] of SLIDERS) {
      $(c).addEventListener("input", (e) => {
        st.sim.params[key] = +e.target.value;
        clearPresets();
        changed({ preview: pre });
      });
    }
    const bandOf = (code) => {
      const i = instruments().find((x) => x.code === code) || inst();
      return [st.sim.params.low[code] ?? i.low, st.sim.params.high[code] ?? i.high, tickOf(i)];
    };
    const setStart = (code, v) => {
      const p = st.sim.params;
      let [lo, hi, tick] = bandOf(code);
      if (v < lo || v > hi - tick) {             // slide the band along with it
        const w = hi - lo;
        lo = v - w / 2; hi = v + w / 2;
      }
      p.start[code] = v; p.low[code] = +lo.toFixed(10); p.high[code] = +hi.toFixed(10);
      delete p.band[code];
    };
    $("cStart").addEventListener("change", (e) => {
      const code = inst() && inst().code, p = st.sim.params;
      if (!code) return;
      if (e.target.value === "") { delete p.start[code]; changed({ preview: true }); return; }
      const v = +e.target.value;
      if (!isFinite(v)) return syncTuner();
      setStart(code, v);
      changed({ preview: true });
    });
    $("resetStart").addEventListener("click", () => {
      const code = inst() && inst().code;
      if (code && st.sim.prior[code] != null) { setStart(code, st.sim.prior[code]); changed({ preview: true }); }
    });
    const bandEdit = (which) => (e) => {
      const code = inst() && inst().code, p = st.sim.params, v = +e.target.value;
      if (!code || !isFinite(v)) return syncTuner();
      const [lo, hi] = bandOf(code);
      if (which === "low" ? v >= hi : v <= lo) return syncTuner();
      p.low[code] = which === "low" ? v : lo;
      p.high[code] = which === "high" ? v : hi;
      delete p.band[code];
      changed({ preview: true });
    };
    $("cLow").addEventListener("change", bandEdit("low"));
    $("cHigh").addEventListener("change", bandEdit("high"));
    $("cSeed").addEventListener("change", (e) => {
      st.sim.params.seed = Math.max(0, Math.floor(+e.target.value || 0));
      changed({ preview: true });
    });
    $("reseed").addEventListener("click", () => {
      st.sim.params.seed = Math.floor(Math.random() * 1e6);
      changed({ preview: true });
    });
    $("cFeeds").addEventListener("change", (e) => { st.sim.params.feeds = e.target.value; changed(); });
    document.querySelectorAll(".presets button").forEach((b) => b.addEventListener("click", () => {
      Object.assign(st.sim.params, PRESETS[b.dataset.preset]);
      clearPresets();
      b.setAttribute("aria-pressed", "true");
      const lossOnly = b.dataset.preset === "lossy";
      changed({ preview: !lossOnly });
    }));
    $("copyCli").addEventListener("click", async () => {
      const text = cliString(st.sim.params);
      let ok = true;
      try { await navigator.clipboard.writeText(text); } catch (_) { ok = false; }
      $("toast").textContent = ok ? "copied ✓" : "select the command below to copy";
      setTimeout(() => ($("toast").textContent = ""), 1600);
    });
  }
  function clearPresets() {
    document.querySelectorAll(".presets button").forEach((b) => b.setAttribute("aria-pressed", "false"));
  }

  // ---------------------------------------------------------- products/schema --
  function catalog() {
    const s = st.sim.session || st.cap.session;
    return (s && s.catalog) || [];
  }
  function currentProducts() {
    if (st.mode === "live") return st.live.products;
    if (st.mode === "simulated") return st.sim.params ? st.sim.params.products : [];
    return instruments().map((i) => i.code);
  }
  function renderToolbar() {
    const sel = $("schema");
    const s = session();
    const schemas = (s && s.schemas) || (st.sim.session && st.sim.session.schemas) ||
      [{ name: "cme", label: "CME templates_FixBinary.xml" }, { name: "subset", label: "working subset (mdp3.xml)" }];
    const opts = (st.mode === "simulated" ? [] : [{ name: "auto", label: "auto-detect" }]).concat(schemas);
    let cur;
    if (st.mode === "simulated") cur = st.sim.params && st.sim.params.schema;
    else if (st.mode === "live") cur = st.live.schemaChoice || "auto";
    else cur = st.cap.schemaChoice || "auto";
    sel.innerHTML = opts.map((o) => `<option value="${esc(o.name)}"${o.name === cur ? " selected" : ""}>${esc(o.label)}</option>`).join("");
    const desc = st.mode === "live" ? st.live.schema : s && s.schema;
    $("schemaSub").textContent = desc ? `${desc.label || desc.name}${desc.auto ? " (detected)" : ""} · id ${desc.id} v${desc.version}` : "";

    const products = currentProducts();
    const editable = st.mode !== "capture";
    $("productChips").innerHTML = products.map((c) =>
      `<span class="pchip">${esc(c)}${editable && products.length > 1 ? `<button data-rm="${esc(c)}" aria-label="remove ${esc(c)}">×</button>` : ""}</span>`).join("");
    const add = $("productAdd");
    add.hidden = !editable;
    const cat = catalog();
    const groups = [...new Set(cat.map((p) => p.group))];
    add.innerHTML = `<option value="">${products.length >= MAX_PRODUCTS ? "max 4 products" : "+ add product"}</option>` +
      groups.map((g) => `<optgroup label="${esc(g)}">` + cat.filter((p) => p.group === g && !products.includes(p.code))
        .map((p) => `<option value="${esc(p.code)}">${esc(p.code)} — ${esc(p.label)} (${esc(p.symbol)}) · ch ${p.channel}</option>`).join("") + "</optgroup>").join("");
    add.disabled = products.length >= MAX_PRODUCTS || STATIC;
  }
  function setProducts(list) {
    if (st.mode === "simulated") {
      const p = st.sim.params;
      p.products = list;
      for (const k of ["start", "low", "high", "band"]) for (const c of Object.keys(p[k])) if (!list.includes(c)) delete p[k][c];
      st.sel = Math.min(st.sel, list.length - 1);
      st.sim.preview = null;
      changed({ now: true });
    } else if (st.mode === "live") {
      st.live.products = list;
      st.sel = Math.min(st.sel, list.length - 1);
      connectLive();
    }
  }
  function wireToolbar() {
    $("productChips").addEventListener("click", (e) => {
      const c = e.target.closest("button[data-rm]");
      if (c) setProducts(currentProducts().filter((x) => x !== c.dataset.rm));
    });
    $("productAdd").addEventListener("change", (e) => {
      const v = e.target.value;
      if (v && currentProducts().length < MAX_PRODUCTS) setProducts(currentProducts().concat([v]));
    });
    $("schema").addEventListener("change", (e) => {
      const v = e.target.value;
      if (st.mode === "simulated") { st.sim.params.schema = v; st.sim.preview = null; changed({ now: true }); }
      else if (st.mode === "live") { st.live.schemaChoice = v; connectLive(); }
      else { st.cap.schemaChoice = v; if (st.cap.file || st.cap.src) loadCapture(); }
    });
  }

  // ------------------------------------------------------------------- modes ---
  function setMode(mode, push) {
    if (STATIC && mode !== "simulated") return;
    if (st.mode === "live" && mode !== "live") disconnectLive();
    pause();
    st.mode = mode;
    st.sel = 0; st.cur = 0;
    document.querySelectorAll(".modes button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.mode === mode)));
    $("simPanel").hidden = mode !== "simulated";
    $("capPanel").hidden = mode !== "capture";
    $("livePanel").hidden = mode !== "live";
    $("controls").hidden = mode === "live";
    banner("");
    setStatus("");
    if (push !== false) {
      const q = mode === "simulated" && st.sim.params ? toQuery(st.sim.params) : new URLSearchParams();
      if (mode !== "simulated") q.set("mode", mode);
      if (mode === "live") q.set("products", st.live.products.join(","));
      history.replaceState(null, "", "?" + q.toString());
    }
    if (mode === "simulated" && !st.sim.session) { st.sim.params = st.sim.params || paramsFromSession(null); refreshSim(); }
    if (mode === "capture" && !st.cap.list) {
      loadCaptureList();
      const src = query.get("src");
      if (src && !st.cap.session) { st.cap.src = src; st.cap.schemaChoice = query.get("schema") || "auto"; loadCapture(); }
    }
    if (mode === "live") connectLive();
    onSessionChanged();
  }
  function wireModes() {
    document.querySelectorAll(".modes button").forEach((b) => {
      if (STATIC && b.dataset.mode !== "simulated") {
        b.disabled = true;
        b.title = "Needs the dashboard server: python3 app.py";
      }
      b.addEventListener("click", () => setMode(b.dataset.mode));
    });
  }

  // ----------------------------------------------------------------- capture ---
  async function loadCaptureList() {
    try {
      const d = await getJSON("api/captures");
      st.cap.list = d;
      const rows = d.local.concat(d.s3);
      $("capHint").textContent = `${d.local.length} in ${d.dir}` +
        (d.s3Configured ? ` · ${d.s3.length} in S3` : " · S3 not configured") + (d.s3Error ? ` (S3: ${d.s3Error})` : "");
      $("capList").innerHTML = rows.length ? `<table><thead><tr><th>where</th><th>capture</th><th class="num">size</th><th>modified</th><th></th></tr></thead><tbody>` +
        rows.map((c) => `<tr><td>${esc(c.where)}</td><td class="mono">${esc(c.name)}</td><td class="num mono">${(c.size / 1024).toFixed(1)} KB</td><td class="mono">${esc(String(c.mtime).slice(0, 19))}</td><td><button data-src="${esc(c.src)}">Open</button></td></tr>`).join("") +
        "</tbody></table>" : `<p class="desc">No captures yet. <code>./mdp3 up --capture</code>, <code>capture.py --local-dir pcaps</code> or <code>simulator.py --pcap pcaps/x.pcap</code> will put some here.</p>`;
    } catch (e) {
      banner(`Could not list captures: ${e.message}`);
    }
  }
  async function loadCapture(picks) {
    const q = new URLSearchParams();
    q.set("schema", st.cap.schemaChoice || "auto");
    if (picks && picks.length) q.set("instruments", picks.join(","));
    setStatus("decoding capture…", true);
    try {
      let s;
      if (st.cap.file) {
        const fd = new FormData();
        fd.append("file", st.cap.file);
        s = await getJSON("api/capture?" + q.toString(), { method: "POST", body: fd });
      } else {
        q.set("src", st.cap.src);
        s = await getJSON("api/capture?" + q.toString());
      }
      st.cap.session = s;
      st.sel = 0; st.cur = 0;
      if (st.cap.src) history.replaceState(null, "", "?" + new URLSearchParams({ mode: "capture", src: st.cap.src,
        schema: st.cap.schemaChoice || "auto" }).toString());
      banner(s.instruments.length ? "" : "No MDP instruments found in this capture.", "info");
      const r = s.source.read || {};
      setStatus(`${s.source.name} · ${s.totals.datagrams} datagrams · ${s.schema.label}${s.schema.auto ? " (detected)" : ""}` +
        (r.skipped ? ` · ${r.skipped} non-UDP frames skipped` : "") + (s.totals.unrouted ? ` · ${s.totals.unrouted} on unknown feeds` : ""));
      renderPicker();
      onSessionChanged();
    } catch (e) {
      banner(`Could not open the capture: ${e.message}`);
      setStatus("");
    }
  }
  function renderPicker() {
    const s = st.cap.session, el = $("capPick");
    const avail = (s && s.available) || [];
    if (avail.length <= instruments().length) { el.hidden = true; return; }
    const shown = new Set(instruments().map((i) => i.securityId));
    el.hidden = false;
    el.innerHTML = `<span class="field-lab">instruments (up to 4)</span>` + avail.slice(0, 40).map((a) =>
      `<label class="mini"><input type="checkbox" value="${a.securityId}"${shown.has(a.securityId) ? " checked" : ""}> ${esc(a.symbol || "sid " + a.securityId)} <small>(${a.entries})</small></label>`).join("") +
      ` <button id="capApply">Show</button>`;
    $("capApply").onclick = () => {
      const picks = [...el.querySelectorAll("input:checked")].map((x) => +x.value).slice(0, MAX_PRODUCTS);
      if (picks.length) loadCapture(picks);
    };
  }
  function wireCapture() {
    $("capList").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-src]");
      if (!b) return;
      st.cap.src = b.dataset.src; st.cap.file = null;
      loadCapture();
    });
    const drop = $("drop"), file = $("capFile");
    drop.addEventListener("click", () => file.click());
    drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); file.click(); } });
    file.addEventListener("change", () => { if (file.files[0]) { st.cap.file = file.files[0]; st.cap.src = null; loadCapture(); } });
    drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("over"));
    drop.addEventListener("drop", (e) => {
      e.preventDefault(); drop.classList.remove("over");
      if (e.dataTransfer.files[0]) { st.cap.file = e.dataTransfer.files[0]; st.cap.src = null; loadCapture(); }
    });
  }

  // -------------------------------------------------------------------- live ---
  function connectLive() {
    disconnectLive();
    const q = new URLSearchParams({ products: st.live.products.join(","), schema: st.live.schemaChoice || "auto",
      iface: $("lIface").value.trim() || "127.0.0.1", feeds: $("lFeeds").value });
    history.replaceState(null, "", "?mode=live&" + q.toString());
    Object.assign(st.live, { hist: [], instruments: [], channels: [], events: [], connected: false });
    const es = (st.live.es = new EventSource("api/live?" + q.toString()));
    $("liveState").textContent = "connecting…";
    $("liveState").classList.remove("on");
    $("lConnect").textContent = "Disconnect";
    es.onmessage = (m) => { ingestLive(JSON.parse(m.data)); };
    es.addEventListener("error", (m) => {
      if (m.data) { banner(`Live feed failed: ${JSON.parse(m.data).error}`); disconnectLive(); return; }
      $("liveState").textContent = es.readyState === 2 ? "disconnected" : "reconnecting…";
      $("liveState").classList.remove("on");
    });
    renderAll();
  }
  function disconnectLive() {
    if (st.live.es) st.live.es.close();
    st.live.es = null;
    st.live.connected = false;
    $("lConnect").textContent = "Connect";
    $("liveState").textContent = "disconnected";
    $("liveState").classList.remove("on");
  }
  function ingestLive(snap) {
    const L = st.live;
    L.connected = true;
    L.schema = snap.schema;
    L.channels = snap.channels;
    L.packets = snap.packets;
    L.events = L.events.concat(snap.events || []).slice(-400);
    const hist = L.hist;
    const rows = snap.instruments.map((r) => ({ m: r.m, bb: r.bb, bo: r.bo, b: r.b, o: r.o, tr: r.tr,
      s: STATE.indexOf(r.state), r: r.rptSeq }));
    hist.push({ t: snap.t * 1000, i: rows });
    if (hist.length > LIVE_HISTORY) hist.splice(0, hist.length - LIVE_HISTORY);
    L.instruments = snap.instruments.map((r, k) => {
      const mids = hist.map((f) => f.i[k] && f.i[k].m).filter((x) => x != null);
      const lo = mids.length ? Math.min(...mids) : null, hi = mids.length ? Math.max(...mids) : null;
      const pad = lo == null ? 0 : Math.max((hi - lo) * 0.15, r.tick * 4);
      return { key: r.code, code: r.code, symbol: r.symbol, label: r.label, securityId: r.securityId,
        channel: r.channel, tick: r.tick, decimals: r.decimals, definition: r.definition,
        low: lo == null ? null : lo - pad, high: hi == null ? null : hi + pad,
        center: lo == null ? null : (lo + hi) / 2, start: null, startSource: "live",
        summary: { state: r.state, rptSeq: r.rptSeq, gaps: r.gaps, recoveries: r.recoveries,
                   checks: r.checks, mismatches: r.mismatches },
        volume: r.volume, last: r.last };
    });
    st.cur = hist.length - 1;
    const det = snap.schema.detected ? `schema ${snap.schema.detected}` : "schema: detecting…";
    $("liveState").textContent = `connected · ${snap.packets.toLocaleString()} datagrams · ${det}`;
    $("liveState").classList.add("on");
    const sig = L.instruments.map((i) => i.symbol + (i.definition ? "+" : "")).join(",");
    if (sig !== L.sig) { L.sig = sig; renderToolbar(); renderTabs(); renderDefs(); }
    renderLiveFeeds();
    show(st.cur);
  }
  function wireLive() {
    $("lConnect").addEventListener("click", () => (st.live.es ? disconnectLive() : connectLive()));
    $("lFeeds").addEventListener("change", () => { if (st.live.es) connectLive(); });
    $("lIface").addEventListener("change", () => { if (st.live.es) connectLive(); });
  }

  // ------------------------------------------------------------------ render ---
  function onSessionChanged() {
    buildCumulative();
    const n = frames().length;
    st.cur = Math.min(st.cur, Math.max(0, n - 1));
    $("scrub").max = Math.max(0, n - 1);
    renderAll();
  }
  function renderAll() {
    renderToolbar();
    renderTabs();
    renderFeeds();
    renderInspector();
    renderDefs();
    show(st.cur);
  }

  function renderTabs() {
    const list = instruments();
    $("instTabs").innerHTML = list.map((i, k) =>
      `<button class="itab" role="tab" data-k="${k}" aria-selected="${k === st.sel}">` +
      `<span class="sym"><i class="dotst" id="dot${k}"></i>${esc(i.symbol)}</span><span class="mid" id="tmid${k}">—</span>` +
      `<span class="lbl">${esc(i.label)} · ch ${esc(i.channel)}${i.securityId != null ? " · sid " + esc(i.securityId) : ""}</span>` +
      `<svg viewBox="0 0 100 26" preserveAspectRatio="none" aria-hidden="true"><polyline id="spark${k}" points=""/></svg></button>`).join("") ||
      `<p class="desc">${st.mode === "capture" ? "Open a capture to see its instruments." : st.mode === "live" ? "Waiting for the live feed…" : ""}</p>`;
    const first = list[0];
    $("brandTitle").textContent = list.length ? `${list.map((i) => i.code).join(" · ")} Feed ${st.mode === "live" ? "Monitor" : st.mode === "capture" ? "Capture" : "Simulator"}` : "MDP 3.0 Feed Simulator";
    document.title = list.length ? `${list.map((i) => i.symbol).join(" ")} — MDP 3.0` : "MDP 3.0 Feed Simulator";
    if (!first) return;
  }
  function selectInstrument(k) {
    if (k < 0 || k >= instruments().length) return;
    st.sel = k;
    document.querySelectorAll(".itab").forEach((b) => b.setAttribute("aria-selected", String(+b.dataset.k === k)));
    if (st.mode === "simulated") syncTuner();
    show(st.cur);
  }

  function show(k) {
    const fr = frames();
    st.cur = Math.max(0, Math.min(fr.length - 1, k));
    const i = inst(), r = row(st.cur);
    // header price
    if (i && r && r.m != null) {
      $("bigPx").textContent = fmt(i, r.m, 1);
      const prev = row(Math.max(0, st.cur - 1));
      const d = prev && prev.m != null ? r.m - prev.m : 0;
      const ref = i.start != null ? i.start : i.center;
      const refLab = i.startSource === "priorClose" ? "prev close" : i.startSource === "explicit" ? "open" : "center";
      const vs = ref != null ? r.m - ref : null;
      $("bigChg").textContent = (d >= 0 ? "▲ +" : "▼ ") + fmt(i, d) + (vs != null && st.mode === "simulated" ? `  (${vs >= 0 ? "+" : ""}${fmt(i, vs)} vs ${refLab})` : "");
      $("bigChg").className = "chg mono " + (d >= 0 ? "up" : "down");
      $("bigLab").textContent = `${i.symbol} mid`;
    } else {
      $("bigPx").textContent = "—"; $("bigChg").innerHTML = "&nbsp;"; $("bigLab").textContent = "Mid";
    }
    // tabs
    instruments().forEach((ins, k2) => {
      const rr = row(st.cur, k2);
      const mid = $("tmid" + k2), dot = $("dot" + k2), sp = $("spark" + k2);
      if (mid) mid.textContent = rr && rr.m != null ? fmt(ins, rr.m, 1) : "—";
      if (dot) dot.className = "dotst " + (rr ? STATE[rr.s] || "waiting" : "waiting");
      if (sp) sp.setAttribute("points", sparkPoints(k2));
    });
    renderLadder(i, r);
    drawChart();
    renderTape();
    renderStats();
    renderEvents();
    moveFeedCursor();
    const t = fr[st.cur] ? fr[st.cur].t : 0;
    $("scrub").value = st.cur;
    $("tcount").textContent = `${fr.length ? st.cur + 1 : 0} / ${fr.length}  ·  ${(t / 1000).toFixed(1)} s`;
  }

  function sparkPoints(k) {
    const fr = frames();
    const n = Math.min(fr.length, st.cur + 1);
    if (n < 2) return "";
    const stride = Math.max(1, Math.floor(n / 90));
    const vals = [];
    for (let j = 0; j < n; j += stride) { const r = fr[j].i[k]; if (r && r.m != null) vals.push([j, r.m]); }
    if (vals.length < 2) return "";
    const lo = Math.min(...vals.map((v) => v[1])), hi = Math.max(...vals.map((v) => v[1]));
    const span = hi - lo || 1;
    const total = st.mode === "live" ? n - 1 : fr.length - 1;
    return vals.map(([j, m]) => `${(j / total * 100).toFixed(2)},${(24 - (m - lo) / span * 22).toFixed(2)}`).join(" ");
  }

  function lastTrade(k) {
    const fr = frames();
    for (let j = k; j >= 0; j--) {
      const r = fr[j] && fr[j].i[st.sel];
      if (r && r.tr && r.tr.length) return r.tr[r.tr.length - 1][0];
    }
    const i = inst();
    return i && i.last ? i.last[0] : null;
  }

  function renderLadder(i, r) {
    const el = $("ladder");
    if (!i || !r || (!r.b.length && !r.o.length)) {
      el.innerHTML = `<div class="empty">${!i ? "no instrument" : r && r.s === 1 ? "recovering — waiting for a snapshot" : "waiting for the book…"}</div>`;
      return;
    }
    const tick = tickOf(i), d = decimalsOf(i);
    const key = (p) => Number(p).toFixed(d);
    const bm = new Map(r.b.map((x) => [key(x[0]), x])), om = new Map(r.o.map((x) => [key(x[0]), x]));
    const prices = r.b.map((x) => x[0]).concat(r.o.map((x) => x[0]));
    const top = Math.max(...prices), bot = Math.min(...prices);
    const last = lastTrade(st.cur);
    let max = 1;
    for (const x of r.b.concat(r.o)) max = Math.max(max, x[1]);
    const eq = (a, b) => a != null && b != null && Math.abs(a - b) < tick / 4;
    const rows = Math.min(80, Math.round((top - bot) / tick));
    let h = `<div class="dom-head"><span class="b">bid qty</span><span class="p">price</span><span class="a">ask qty</span></div>`;
    if (r.s === 1) h += `<div class="stale-note">recovering: this book is stale until the next snapshot</div>`;
    for (let k = 0; k <= rows; k++) {
      const p = top - k * tick, kk = key(p), b = bm.get(kk), o = om.get(kk);
      const cls = [b && eq(p, r.bb) ? "bestbid" : "", o && eq(p, r.bo) ? "bestoff" : "", eq(p, last) ? "last" : ""].join(" ").trim();
      h += `<div class="lrow ${cls}"><div class="cell bidc">${b ? `<span class="bar" style="width:${(b[1] / max * 100).toFixed(1)}%"></span><span class="q">${b[1]}</span>` : ""}</div>` +
        `<div class="cell pxc">${kk}</div>` +
        `<div class="cell askc">${o ? `<span class="bar" style="width:${(o[1] / max * 100).toFixed(1)}%"></span><span class="q">${o[1]}</span>` : ""}</div></div>`;
    }
    h += `<div class="dom-foot"><span>spread <b>${r.bb != null && r.bo != null ? fmt(i, r.bo - r.bb) : "—"}</b></span>` +
      `<span>last <b>${fmt(i, last)}</b></span><span>rptSeq <b>${r.r ?? "—"}</b></span></div>`;
    el.innerHTML = h;
    $("ladderHint").textContent = `${i.symbol} · tick ${tickOf(i)}${i.definition ? " (from definition)" : ""}`;
  }

  // ------------------------------------------------------------------- chart ---
  const cv = $("chart"), cx = cv.getContext("2d");
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  function drawChart() {
    const r = cv.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
    const w = r.width || 600, h = 300;
    cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
    cx.setTransform(dpr, 0, 0, dpr, 0, 0);
    cx.clearRect(0, 0, w, h);
    const i = inst(), fr = frames();
    if (!i || !fr.length || i.low == null) { $("chartHint").textContent = ""; return; }
    const padL = 8, padR = 64, padT = 14, padB = 14, iw = w - padL - padR, ih = h - padT - padB;
    const lo = i.low, hi = i.high, span = hi - lo || 1;
    const y = (p) => padT + (1 - (p - lo) / span) * ih;
    const n = fr.length, x = (k) => padL + (n <= 1 ? 0 : k / (n - 1)) * iw;
    const live = st.mode === "live";
    // recovering shading
    cx.fillStyle = css("--recov");
    let runStart = -1;
    for (let k = 0; k <= n; k++) {
      const rr = k < n ? fr[k].i[st.sel] : null;
      const rec = rr && rr.s === 1;
      if (rec && runStart < 0) runStart = k;
      if (!rec && runStart >= 0) { cx.fillRect(x(runStart), padT, Math.max(2, x(k) - x(runStart)), ih); runStart = -1; }
    }
    if (!live) { cx.fillStyle = css("--grid"); cx.fillRect(padL, y(hi), iw, y(lo) - y(hi)); }
    cx.strokeStyle = css("--grid"); cx.fillStyle = css("--faint");
    cx.font = '10px "IBM Plex Mono",monospace'; cx.textBaseline = "middle"; cx.textAlign = "left";
    for (let g = 0; g <= 5; g++) {
      const p = lo + span * g / 5;
      cx.beginPath(); cx.moveTo(padL, y(p)); cx.lineTo(padL + iw, y(p)); cx.stroke();
      cx.fillText(fmt(i, p), padL + iw + 6, y(p));
    }
    if (i.start != null && i.start >= lo && i.start <= hi) {
      cx.strokeStyle = css("--muted"); cx.setLineDash([7, 4]); cx.globalAlpha = 0.75;
      cx.beginPath(); cx.moveTo(padL, y(i.start)); cx.lineTo(padL + iw, y(i.start)); cx.stroke();
      cx.setLineDash([]); cx.fillStyle = css("--muted"); cx.textBaseline = "bottom";
      cx.fillText((i.startSource === "priorClose" ? "prev close " : "open ") + fmt(i, i.start), padL + 4, y(i.start) - 3);
      cx.globalAlpha = 1; cx.textBaseline = "middle";
    }
    cx.strokeStyle = css("--accent"); cx.lineWidth = 1.6; cx.lineJoin = "round"; cx.beginPath();
    let pen = false;
    for (let k = 0; k <= st.cur && k < n; k++) {
      const rr = fr[k].i[st.sel];
      if (!rr || rr.m == null) { pen = false; continue; }
      pen ? cx.lineTo(x(k), y(rr.m)) : cx.moveTo(x(k), y(rr.m));
      pen = true;
    }
    cx.stroke();
    cx.fillStyle = css("--trade"); cx.globalAlpha = 0.85;
    for (let k = 0; k <= st.cur && k < n; k++) {
      const rr = fr[k].i[st.sel];
      if (rr && rr.tr) for (const t of rr.tr) { cx.beginPath(); cx.arc(x(k), y(t[0]), 2.2, 0, 7); cx.fill(); }
    }
    cx.globalAlpha = 1;
    const rr = fr[st.cur] && fr[st.cur].i[st.sel];
    if (rr && rr.m != null) {
      cx.fillStyle = css("--accent"); cx.beginPath(); cx.arc(x(st.cur), y(rr.m), 3.5, 0, 7); cx.fill();
      cx.strokeStyle = css("--panel"); cx.lineWidth = 1.5; cx.stroke();
    }
    $("chartHint").textContent = live ? `${i.symbol} · last ${(fr.length / 5).toFixed(0)} s` :
      `${i.symbol} · band [${fmt(i, lo)}, ${fmt(i, hi)}]` + (i.start != null ? ` · open ${fmt(i, i.start)}` : "");
  }

  // ------------------------------------------------------------------- tape ----
  function renderTape() {
    const i = inst(), fr = frames(), el = $("tape");
    if (!i) { el.innerHTML = ""; return; }
    const sec = (k) => (fr[k].t / 1000).toFixed(1) + "s";
    let h = "", n = 0;
    for (let k = st.cur; k >= 0 && n < 60; k--) {
      const r = fr[k].i[st.sel];
      if (!r || !r.tr) continue;
      for (let j = r.tr.length - 1; j >= 0; j--) {
        const t = r.tr[j];
        h += `<div class="ev"><span class="t">${sec(k)}</span><span class="tag trade">TRD</span><span class="body"><b>${esc(t[1])}</b> @ <b>${fmt(i, t[0])}</b> <span class="${t[2] === "Buy" ? "buy" : "sell"}">${esc(t[2])}</span></span></div>`;
        n++;
      }
    }
    el.innerHTML = h || `<div class="empty">no trades yet</div>`;
  }

  function renderStats() {
    const i = inst(), el = $("stats"), r = row(st.cur);
    const s = session(), c = st.cum, k = st.cur;
    const stat = (kk, v) => `<div class="stat"><div class="k">${esc(kk)}</div><div class="v">${v}</div></div>`;
    let h = "";
    if (st.mode === "live") {
      const ch = st.live.channels;
      const lost = ch.reduce((a, x) => a + x.arb.lost, 0), acc = ch.reduce((a, x) => a + x.arb.accepted, 0);
      h += stat("datagrams", st.live.packets.toLocaleString()) + stat("processed (first copy)", acc.toLocaleString()) +
        stat("lost on both feeds", lost) + stat("state", i ? esc(i.summary.state) : "—") +
        stat("gaps · recoveries", i ? `${i.summary.gaps} · ${i.summary.recoveries}` : "—") +
        stat("volume", i && i.volume != null ? i.volume.toLocaleString() : "—");
    } else if (s && c && c.sent.length) {
      h += stat("datagrams on wire", c.sent[k].toLocaleString()) + stat("bytes", c.bytes[k].toLocaleString()) +
        stat("dropped by loss", c.drop[k]) + stat("lost on both", c.gaps[k]) + stat("recoveries", c.rec[k]) +
        stat("state", r ? `${esc(STATE[r.s])} <small>rpt ${r.r ?? "—"}</small>` : "—");
    } else if (st.sim.preview) {
      h += stat("preview", "engine only") + stat("state", "—");
    }
    el.innerHTML = h;
  }

  // ------------------------------------------------------------------- feeds ---
  function renderFeeds() {
    const el = $("feeds");
    if (st.mode === "live") return renderLiveFeeds();
    const s = session();
    if (!s) { el.innerHTML = ""; return; }
    const total = Math.max(1, ...s.frames.map((f) => f.t), s.packets.length ? s.packets[s.packets.length - 1][0] : 1);
    el.innerHTML = s.channels.map((ch, ci) => {
      const a = ch.arb, only = a.only || {};
      const lanes = LANES.filter(([k, f]) => ch.feeds.some((x) => x.kind === k && x.feed === f));
      return `<div class="chan" data-ci="${ci}"><h3>channel ${esc(ch.id)} <span class="lbl">${esc(ch.label)}</span>` +
        `<span class="arb">won A <b>${a.won.A || 0}</b> / B <b>${a.won.B || 0}</b> · B covered <b>${only.B || 0}</b> · A covered <b>${only.A || 0}</b> · dup ${a.duplicates} · held ${a.held} · <span class="${a.lost ? "bad" : ""}">lost on both <b>${a.lost}</b></span>${ch.decodeErrors ? ` · <span class="bad">decode errors <b>${ch.decodeErrors}</b></span>` : ""}</span></h3>` +
        `<div class="lanes">` + lanes.map(([k, f]) => {
          const feed = ch.feeds.find((x) => x.kind === k && x.feed === f);
          const n = s.packets.filter((p) => p[1] === ci && p[2] === k && p[3] === f);
          const dropped = n.filter((p) => p[6] === "drop").length;
          return `<span class="lane-lab">${KIND_NAME[k]} ${f}<br><small>${esc(feed.ip)}:${feed.port}</small></span>` +
            `<canvas data-ci="${ci}" data-k="${k}" data-f="${f}" aria-label="${KIND_NAME[k]} feed ${f} packets"></canvas>` +
            `<span class="lane-num">${n.length - dropped} pkts${dropped ? ` · <span style="color:var(--offer)">${dropped} lost</span>` : ""}</span>`;
        }).join("") + `</div></div>`;
    }).join("") + `<div class="fkey"><span><i style="background:var(--bid)"></i>processed (first copy)</span><span><i style="background:var(--faint)"></i>duplicate</span><span><i style="background:var(--accent)"></i>held for reorder</span><span><i style="background:var(--offer)"></i>dropped (loss)</span><span><i style="background:var(--info)"></i>snapshot</span><span><i style="background:var(--violet)"></i>definition</span><span><i style="background:var(--ink)"></i>playhead</span></div>`;
    requestAnimationFrame(() => drawLanes(total));
  }
  function drawLanes(total) {
    const s = session();
    if (!s) return;
    st.laneTotal = total;
    const colors = { win: css("--bid"), dup: css("--faint"), held: css("--accent"), drop: css("--offer") };
    const loopColor = { S: css("--info"), N: css("--violet") };
    document.querySelectorAll(".lanes canvas").forEach((c) => {
      const ci = +c.dataset.ci, k = c.dataset.k, f = c.dataset.f;
      const r = c.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
      c.width = Math.max(1, Math.round(r.width * dpr)); c.height = Math.round(16 * dpr);
      const g = c.getContext("2d");
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      const w = r.width;
      for (const p of s.packets) {
        if (p[1] !== ci || p[2] !== k || p[3] !== f) continue;
        g.fillStyle = p[6] === "loop" ? loopColor[k] || colors.win : colors[p[6]] || colors.win;
        const x = p[0] / total * (w - 2);
        const tall = p[6] === "drop" || p[6] === "held";
        g.fillRect(x, tall ? 1 : 3, p[6] === "drop" ? 2 : 1.2, tall ? 14 : 10);
      }
      c.onclick = (e) => laneClick(e, ci, k, f, total, w);
    });
    moveFeedCursor();
  }
  function moveFeedCursor() {
    if (st.mode === "live") return;
    const fr = frames(), total = st.laneTotal;
    if (!fr.length || !total) return;
    const t = fr[st.cur].t;
    document.querySelectorAll(".lanes canvas").forEach((c) => {
      c.style.backgroundImage = `linear-gradient(90deg, transparent calc(${(t / total * 100).toFixed(3)}% - 1px), ${css("--ink")} calc(${(t / total * 100).toFixed(3)}% - 1px), ${css("--ink")} calc(${(t / total * 100).toFixed(3)}% + 0.5px), transparent calc(${(t / total * 100).toFixed(3)}% + 0.5px))`;
    });
  }
  function laneClick(e, ci, k, f, total, w) {
    const s = session();
    const t = (e.offsetX / (w - 2)) * total;
    let best = null;
    for (const p of s.packets) {
      if (p[1] !== ci || p[2] !== k || p[3] !== f) continue;
      if (!best || Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p;
    }
    if (!best) return;
    const ch = s.channels[ci];
    const tpls = best[7] ? best[7].split(",").map((x) => `${x} ${(s.samples[x] && s.samples[x].names[s.samples[x].templates.indexOf(+x)]) || ""}`) : [];
    $("feedDetail").textContent = `ch ${ch.id} · ${KIND_NAME[k]} ${f} · seq ${best[4]} · ${best[5]} B · ${(best[0] / 1000).toFixed(3)} s · ${best[6]} · templates ${tpls.join(", ") || "—"}`;
    const fr = frames();
    let lo = 0, hi = fr.length - 1;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (fr[mid].t < best[0]) lo = mid + 1; else hi = mid; }
    pause(); show(lo);
  }
  function renderLiveFeeds() {
    const el = $("feeds"), L = st.live;
    if (!L.channels.length) { el.innerHTML = `<p class="desc" style="padding:0 0 8px">${L.es ? "waiting for the first snapshot…" : "not connected"}</p>`; return; }
    const maxRate = Math.max(1, ...L.channels.flatMap((c) => c.feeds.map((f) => f.rate)));
    el.innerHTML = L.channels.map((ch) => {
      const a = ch.arb, only = a.only || {};
      return `<div class="chan"><h3>channel ${esc(ch.id)} <span class="arb">won A <b>${a.won.A || 0}</b> / B <b>${a.won.B || 0}</b> · B covered <b>${only.B || 0}</b> · A covered <b>${only.A || 0}</b> · dup ${a.duplicates} · <span class="${a.lost ? "bad" : ""}">lost on both <b>${a.lost}</b></span> · seq ${a.lastSeq ?? "—"}${ch.decodeErrors ? ` · <span class="bad">decode errors <b>${ch.decodeErrors}</b></span>` : ""}</span></h3><div class="lanes">` +
        LANES.map(([k, f]) => ch.feeds.find((x) => x.kind === k && x.feed === f)).filter(Boolean).map((fd) =>
          `<span class="lane-lab">${KIND_NAME[fd.kind]} ${fd.feed}<br><small>${esc(fd.ip)}:${fd.port}</small></span>` +
          `<span style="display:block;height:12px;border-radius:3px;background:var(--panel-2);overflow:hidden"><span style="display:block;height:100%;width:${(fd.rate / maxRate * 100).toFixed(1)}%;background:${fd.kind === "I" ? "var(--bid)" : fd.kind === "S" ? "var(--info)" : "var(--violet)"}"></span></span>` +
          `<span class="lane-num">${fd.rate.toFixed(1)}/s · ${fd.packets.toLocaleString()}</span>`).join("") + `</div></div>`;
    }).join("");
  }

  function renderEvents() {
    const el = $("events");
    let list;
    if (st.mode === "live") list = st.live.events;
    else {
      const s = session(), fr = frames();
      if (!s || !fr.length) { el.innerHTML = ""; return; }
      const t = fr[st.cur].t;
      list = s.events.filter((e) => e.t <= t);
    }
    const shown = list.slice(-200).reverse();
    el.innerHTML = shown.length ? shown.map((e) =>
      `<div class="ev"><span class="t">${(e.t / (st.mode === "live" ? 1 : 1000)).toFixed(2)}s</span><span class="tag ${esc(e.kind)}">${esc(e.kind)}</span><span class="body">${esc(e.text)}</span></div>`).join("")
      : `<div class="empty">no feed events yet</div>`;
  }

  // --------------------------------------------------------------- inspector ---
  function renderInspector() {
    const s = session(), tabs = $("inspTabs");
    if (!s || !s.samples || !Object.keys(s.samples).length) {
      tabs.innerHTML = "";
      $("hex").innerHTML = `<span class="desc">${st.mode === "live" ? "The inspector shows simulated sessions and captures." : "no packets"}</span>`;
      $("fields").innerHTML = "";
      $("inspHint").textContent = "";
      return;
    }
    const ids = Object.keys(s.samples).sort((a, b) => a - b);
    if (!ids.includes(st.inspTpl)) st.inspTpl = ids[0];
    tabs.innerHTML = ids.map((id) => {
      const sm = s.samples[id];
      const nm = sm.names[sm.templates.indexOf(+id)] || "";
      return `<button role="tab" data-tpl="${id}" aria-selected="${id === st.inspTpl}">${id} · ${esc(nm)}</button>`;
    }).join("");
    const sm = s.samples[st.inspTpl];
    $("inspHint").textContent = `${s.schema.label} · ${KIND_NAME[sm.kind[0]] || sm.kind} feed ${sm.feed} · seq ${sm.seq} · ${sm.hex.length / 2} bytes`;
    const bytes = sm.hex.match(/../g) || [];
    const segOf = new Array(bytes.length).fill(-1);
    sm.segments.forEach((g, gi) => { for (let b = g.off; b < g.off + g.len; b++) segOf[b] = gi; });
    let fieldIdx = 0;
    const parity = sm.segments.map((g) => (g.kind === "field" && g.len ? fieldIdx++ % 2 : 0));
    let h = "";
    bytes.forEach((by, b) => {
      const gi = segOf[b], g = sm.segments[gi] || { kind: "padding" };
      h += `<span class="hx ${g.kind}${parity[gi] ? " odd" : ""}" data-g="${gi}">${by}</span>${(b + 1) % 16 === 0 ? "<br>" : (b + 1) % 2 === 0 ? " " : ""}`;
    });
    $("hex").innerHTML = h;
    const val = (v) => (v === null || v === undefined ? "<span class='c'>null</span>" : esc(typeof v === "object" ? JSON.stringify(v) : v));
    $("fields").innerHTML = `<colgroup><col class="c-off"><col class="c-len"><col><col class="c-type"><col></colgroup>` +
      `<thead><tr><th>off</th><th>len</th><th>field</th><th>type</th><th>value</th></tr></thead><tbody>` +
      sm.segments.map((g, gi) => `<tr class="k-${esc(g.kind)}" data-g="${gi}" tabindex="0"><td>${g.off}</td><td>${g.len}</td>` +
        `<td class="p">${esc(g.path)}${g.canonical && g.canonical !== g.field ? ` <span class="c">(${esc(g.canonical)})</span>` : ""}</td>` +
        `<td class="c" title="${esc(g.type || "")}">${esc(g.type || "")}</td><td class="v">${g.kind === "padding" ? "" : val(g.value)}</td></tr>`).join("") + "</tbody>";
  }
  function hot(gi, scroll) {
    document.querySelectorAll(".hx.hot, table.fields tr.hot").forEach((x) => x.classList.remove("hot"));
    if (gi == null || gi < 0) return;
    document.querySelectorAll(`.hx[data-g="${gi}"], table.fields tr[data-g="${gi}"]`).forEach((x) => x.classList.add("hot"));
    if (scroll) {
      const r = document.querySelector(`table.fields tr[data-g="${gi}"]`);
      if (r) r.scrollIntoView({ block: "nearest" });
    }
  }
  function wireInspector() {
    $("inspTabs").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-tpl]");
      if (b) { st.inspTpl = b.dataset.tpl; renderInspector(); }
    });
    $("hex").addEventListener("mouseover", (e) => { const s = e.target.closest(".hx"); if (s) hot(+s.dataset.g, true); });
    $("hex").addEventListener("mouseleave", () => hot(-1));
    const tr = (e) => e.target.closest("tr[data-g]");
    $("fields").addEventListener("mouseover", (e) => { const r = tr(e); if (r) hot(+r.dataset.g); });
    $("fields").addEventListener("focusin", (e) => { const r = tr(e); if (r) hot(+r.dataset.g); });
    $("fields").addEventListener("mouseleave", () => hot(-1));
  }

  // -------------------------------------------------------------- definitions --
  function renderDefs() {
    const list = instruments(), el = $("defs");
    const withDef = list.filter((i) => i.definition);
    if (!withDef.length) {
      el.innerHTML = `<tbody><tr><td class="desc">${list.length ? "No instrument definitions received yet" + (st.mode === "simulated" && st.sim.params && !st.sim.params.secdef_every ? " — the definition loop is off." : ".") : ""}</td></tr></tbody>`;
      return;
    }
    el.innerHTML = `<thead><tr><th>symbol</th><th>security id</th><th>asset</th><th>exch</th><th>channel</th><th>tick</th><th>dec</th><th>tick $</th><th>size</th><th>maturity</th><th>limits</th><th>ref price</th><th>depth</th><th>state</th></tr></thead><tbody>` +
      withDef.map((i) => {
        const d = i.definition, sm = i.summary || {};
        return `<tr><td><b>${esc(d.symbol)}</b></td><td class="num">${esc(d.securityId ?? i.securityId)}</td><td>${esc(d.asset)}</td><td>${esc(d.exchange)}</td><td class="num">${esc(d.channel)}</td>` +
          `<td class="num">${esc(d.tick)}</td><td class="num">${esc(d.decimals)}</td><td class="num">${esc(d.tickValue ?? "")}</td><td class="num">${esc(d.multiplier ?? "")}</td><td>${esc(d.maturity)}</td>` +
          `<td>${d.lowLimit != null ? fmt(i, d.lowLimit) + " – " + fmt(i, d.highLimit) : "—"}</td><td class="num">${d.referencePrice != null ? fmt(i, d.referencePrice) : "—"}</td><td class="num">${esc(d.depth ?? "")}</td>` +
          `<td>${esc(sm.state || "")}${sm.recoveries ? ` · ${sm.recoveries} recov.` : ""}${sm.mismatches ? ` · <b style="color:var(--offer)">${sm.mismatches} mismatch</b>` : ""}</td></tr>`;
      }).join("") + "</tbody>";
  }

  // ---------------------------------------------------------------- playback ---
  function tick() {
    if (!st.playing) return;
    const fr = frames();
    if (st.cur >= fr.length - 1) { pause(); return; }
    show(st.cur + 1);
    const dt = fr[st.cur + 1] ? fr[st.cur + 1].t - fr[st.cur].t : stepMs();
    st.timer = setTimeout(() => requestAnimationFrame(tick), Math.max(16, Math.min(500, dt / st.speed)));
  }
  function play() {
    if (st.cur >= frames().length - 1) st.cur = 0;
    st.playing = true;
    $("play").textContent = "❚❚ Pause";
    tick();
  }
  function pause() {
    st.playing = false;
    clearTimeout(st.timer);
    $("play").textContent = "▶ Play";
  }
  function wirePlayback() {
    $("play").addEventListener("click", () => (st.playing ? pause() : play()));
    $("scrub").addEventListener("input", () => { pause(); show(+$("scrub").value); });
    document.querySelectorAll(".speed button").forEach((b) => b.addEventListener("click", () => {
      st.speed = +b.dataset.sp;
      document.querySelectorAll(".speed button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    }));
    $("instTabs").addEventListener("click", (e) => { const b = e.target.closest(".itab"); if (b) selectInstrument(+b.dataset.k); });
    document.addEventListener("keydown", (e) => {
      if (e.target.closest("input, select, textarea") || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key >= "1" && e.key <= "4") selectInstrument(+e.key - 1);
      else if (st.mode === "live") return;
      else if (e.key === " " && !e.target.closest("button")) { e.preventDefault(); st.playing ? pause() : play(); }
      else if (e.key === "ArrowRight") { pause(); show(st.cur + 1); }
      else if (e.key === "ArrowLeft") { pause(); show(st.cur - 1); }
    });
    let rt;
    window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { drawChart(); drawLanes(st.laneTotal); }, 120); });
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => renderAll());
  }

  // ------------------------------------------------------------------- start ---
  function init() {
    st.sim.params = paramsFromSession(st.sim.session);
    priorCloses(st.sim.session);
    wireModes(); wireToolbar(); wireTuner(); wireCapture(); wireLive(); wireInspector(); wirePlayback();
    syncTuner();
    setMode(st.mode, false);
    const at = parseInt(query.get("frame"), 10);       // deep link to a moment
    if (st.mode !== "live" && at > 0) show(at);
    if (STATIC) setStatus("static snapshot · tuning previews in the page; feed panels show the baked session");
    else if (st.mode === "simulated" && st.sim.session) setStatus(`wire session · ${st.sim.session.schema.label}`);
  }
  init();
})();
