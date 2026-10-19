// Sissy Censor System admin panel. Talks to /admin/api (censor_proxy/admin.py).
// Everything from the proxy (URLs and hosts in the log especially) is put in
// the page as text, never as HTML.
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k in el && typeof v !== "string") el[k] = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) {
      if (c === undefined || c === null || c === false) continue;
      el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }

  // ---- API ------------------------------------------------------------

  class ApiError extends Error {
    constructor(message, status, data) { super(message); this.status = status; this.data = data; }
  }

  async function api(path, { method = "GET", body, background = false } = {}) {
    const headers = {};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (background) headers["X-Admin-Background"] = "1";
    const r = await fetch("api/" + path, {
      method, headers, cache: "no-store", credentials: "same-origin",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = {};
    try { data = await r.json(); } catch (_) { /* empty body */ }
    if (r.status === 401 && data.locked) {
      showLocked();
      throw new ApiError(data.error || "Locked", 401, data);
    }
    if (!r.ok) throw new ApiError(data.error || `${r.status} ${r.statusText}`, r.status, data);
    return data;
  }

  let toastTimer;
  function toast(message, bad = false) {
    const t = $("toast");
    t.textContent = message;
    t.className = "toast" + (bad ? " bad" : "");
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, bad ? 6000 : 2500);
  }

  // ---- formatting -----------------------------------------------------

  const fmtInt = (n) => Number(n || 0).toLocaleString();
  function parseTs(ts) {
    // detection_log writes +0000 offsets; Date wants +00:00.
    const d = new Date(String(ts).replace(/([+-]\d\d)(\d\d)$/, "$1:$2"));
    return isNaN(d) ? null : d;
  }
  function fmtTime(d) {
    if (!d) return "";
    const today = new Date().toDateString() === d.toDateString();
    return today ? d.toLocaleTimeString() : d.toLocaleString();
  }
  function fmtDuration(seconds) {
    seconds = Math.max(0, Math.round(seconds || 0));
    const d = Math.floor(seconds / 86400), hr = Math.floor(seconds % 86400 / 3600);
    const m = Math.floor(seconds % 3600 / 60), s = seconds % 60;
    if (d) return `${d}d ${hr}h`;
    if (hr) return `${hr}h ${m}m`;
    if (m) return `${m}m ${s}s`;
    return `${s}s`;
  }
  function fmtClock(seconds) {
    if (seconds === null || seconds === undefined) return "?";
    const s = Math.round(seconds);
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }
  function hostOf(url) {
    try { return new URL(url).hostname; } catch (_) { return ""; }
  }
  function shorten(text, max) {
    text = String(text || "");
    return text.length > max ? text.slice(0, max - 1) + "…" : text;
  }

  // ---- lock -------------------------------------------------------------

  let session = { pin_set: false, unlocked: true };

  function showLocked() {
    stopPolling();
    session.unlocked = false;
    $("tabs").hidden = true;
    $("lock-now").hidden = true;
    $("attempts").hidden = true;
    for (const s of document.querySelectorAll("section.tab")) s.hidden = true;
    $("locked").hidden = false;
    $("unlock-pin").value = "";
    $("unlock-pin").focus();
  }

  // Wrong PIN entries since the report was last dismissed.
  async function loadAttempts() {
    let data;
    try { data = await api("attempts", { background: true }); } catch (_) { return; }
    const attempts = data.attempts || [];
    $("attempts").hidden = attempts.length === 0;
    if (!attempts.length) return;
    $("attempts-title").textContent =
      `${attempts.length} wrong PIN entr${attempts.length === 1 ? "y" : "ies"}`;
    $("attempts-list").replaceChildren(...attempts.slice().reverse().map((a) => h("li", {},
      h("span", { class: "time", text: new Date(a.ts * 1000).toLocaleString() }),
      h("span", { text: a.action === "unlock" ? "unlocking" : a.action }),
      h("span", { class: "muted", text: `via ${a.via}` }),
      a.user_agent ? h("span", { class: "ua", text: a.user_agent }) : null)));
  }

  $("attempts-ack").addEventListener("click", async () => {
    try { await api("attempts/ack", { method: "POST", body: {} }); } catch (err) {
      if (err.status !== 401) toast(err.message, true);
      return;
    }
    $("attempts").hidden = true;
  });

  $("unlock-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("unlock-error").textContent = "";
    try {
      await api("unlock", { method: "POST", body: { pin: $("unlock-pin").value } });
      session = await api("session");
      showApp();
    } catch (err) {
      $("unlock-pin").value = "";
      $("unlock-error").textContent = err.message;
      $("unlock-pin").focus();
    }
  });

  $("lock-now").addEventListener("click", async () => {
    try { await api("lock", { method: "POST", body: {} }); } catch (_) { /* locked anyway */ }
    showLocked();
  });

  // ---- tabs -------------------------------------------------------------

  const TABS = ["overview", "whitelist", "detections", "video", "settings", "lock"];
  let current = null;
  let poll = null;

  function stopPolling() { clearInterval(poll); poll = null; }

  function showApp() {
    $("locked").hidden = true;
    $("tabs").hidden = false;
    $("lock-now").hidden = !session.pin_set;
    loadAttempts();
    const wanted = location.hash.slice(1);
    showTab(TABS.includes(wanted) ? wanted : "overview");
  }

  function showTab(name) {
    current = name;
    stopPolling();
    for (const b of $("tabs").querySelectorAll("button")) b.classList.toggle("active", b.dataset.tab === name);
    for (const t of TABS) $(t).hidden = t !== name;
    if (location.hash.slice(1) !== name) history.replaceState(null, "", "#" + name);
    const loaders = { overview: loadOverview, whitelist: loadWhitelist, detections: loadDetections,
                      video: loadJobs, settings: loadSettings, lock: loadLock };
    loaders[name]();
    if (name === "overview") poll = setInterval(() => loadOverview(true), 3000);
    if (name === "video") poll = setInterval(() => loadJobs(true), 2500);
  }

  $("tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-tab]");
    if (b) showTab(b.dataset.tab);
  });

  // ---- overview ---------------------------------------------------------

  async function loadOverview(background = false) {
    let s;
    try { s = await api("status", { background }); } catch (err) {
      if (err.status !== 401 && !background) toast(err.message, true);
      return;
    }
    if (s.brand_name) $("brand-name").textContent = s.brand_name;
    // The counts live in memory, so a restart starts them again.
    $("overview-note").textContent = `Counts since the proxy started ${fmtDuration(s.uptime_seconds)} ago. `
      + `Updated ${new Date().toLocaleTimeString()}.`;
    const c = s.counts || {};
    const tiles = [
      ["Censored", c.censored, true], ["Clean", c.clean], ["Whitelisted", c.whitelisted],
      ["Failed closed", c.fail_closed], ["Errors", c.error], ["Unscanned", c.unscanned],
      ["Skipped", c.skipped], ["In flight", s.in_flight], ["Video jobs", s.video && s.video.active_jobs],
    ];
    $("stat-tiles").replaceChildren(...tiles.map(([label, value, accent]) =>
      h("div", { class: "tile" + (accent ? " accent" : "") },
        h("div", { class: "label", text: label }), h("div", { class: "value", text: fmtInt(value) }))));
    const dl = (rows) => rows.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", {}, v)]);
    $("info-detection").replaceChildren(...dl([
      ["Status", s.ready ? "Ready" : "Starting"],
      ["Censoring level", s.preset_label || title(s.preset)],
      ["Image model", `${s.model}${s.tiling ? ", tiling" : ""}`],
      ["Resources", `${s.resources_label}: ${plural(s.workers, "worker")}, ${plural(s.threads_per_worker, "thread")}`],
      ["Video", s.video.enabled ? `on, ${s.video.model}` : "off"],
      ["Censor style", s.censor_style],
      ["On failure", s.fail_closed ? "censor the whole image" : "deliver unmodified"],
      ["Cached results", fmtInt(s.cache_entries)],
    ]));
    $("info-proxy").replaceChildren(...dl([
      ["Uptime", fmtDuration(s.uptime_seconds)],
      ["Whitelist", [...s.whitelist, "the built-in list"].join(", ")],
      ["Detection log", h("span", {}, s.log_level, s.log_path ? [" → ", h("code", { text: s.log_path })] : null)],
      ["Config file", s.config_path ? `${s.config_path}${s.config_editable ? "" : " (read-only)"}` : "none"],
      ["Panel", `https://${s.admin_host}/admin`],
      ["PIN lock", s.pin_set ? "on" : "off"],
    ]));
  }

  // ---- config (whitelist and settings share it) -------------------------

  let cfgView = null;

  async function fetchConfig() {
    cfgView = await api("config");
    return cfgView;
  }

  function setting(view, section, key) {
    const s = view.sections.find((x) => x.name === section);
    return s && s.keys.find((k) => k.key === key);
  }

  async function saveSettings(set, reset = []) {
    cfgView = await api("config", { method: "POST", body: { set, reset } });
    return cfgView;
  }

  // ---- whitelist --------------------------------------------------------

  function normalizeDomain(value) {
    value = value.trim().toLowerCase();
    if (!value) return "";
    try { if (value.includes("://")) value = new URL(value).hostname; } catch (_) { /* as typed */ }
    value = value.split("/")[0].split("?")[0];
    if (!value.startsWith("[")) value = value.split(":")[0];
    return value.replace(/^\*\./, "").replace(/^\.+|\.+$/g, "").replace(/^www\./, "");
  }

  async function loadWhitelist() {
    $("wl-error").textContent = "";
    let view;
    try { view = await fetchConfig(); } catch (err) {
      if (err.status !== 401) $("wl-error").textContent = err.message;
      return;
    }
    renderWhitelist(view);
  }

  // A built-in pattern (a regex on host:port) as the domains it covers, for
  // display: one alternation group is expanded ("login\.(live|microsoftonline)\.com:"
  // is two domains). Anything else regex-like is shown as written.
  function readable(pattern) {
    const s = pattern.replace(/^\(\^\|\\\.\)/, "").replace(/^\^/, "").replace(/:$/, "");
    const m = s.match(/^([^()|]*)\(([^()]*)\)([^()|]*)$/);
    const parts = m ? m[2].split("|").map((a) => m[1] + a + m[3]) : [s];
    const plain = parts.map((p) => p.replace(/\\\./g, ".").replace("[^:]+", "*"));
    return plain.every((p) => /^[a-z0-9.*-]+$/i.test(p)) ? plain : [pattern];
  }

  function whitelisted(view, domain) {
    const domains = setting(view, "whitelist", "domains").value || [];
    if (domains.some((d) => domain === d || domain.endsWith("." + d))) return true;
    return (setting(view, "whitelist", "builtin").value || []).some((p) => {
      try { return new RegExp(p, "i").test(`${domain}:443`); } catch (_) { return false; }
    });
  }

  function renderWhitelist(view) {
    const domains = setting(view, "whitelist", "domains").value || [];
    const builtin = (setting(view, "whitelist", "builtin").value || []).flatMap(readable);
    const referer = setting(view, "whitelist", "match_referer").value;
    const editable = view.editable;
    $("wl-list").replaceChildren(...domains.map((d) => h("li", {},
      d, h("button", { type: "button", title: `Remove ${d}`, "aria-label": `Remove ${d}`, text: "×",
                       disabled: !editable, onclick: () => updateDomains(domains.filter((x) => x !== d), `${d} removed`) }))));
    $("wl-empty").hidden = domains.length > 0;
    $("wl-builtin-list").replaceChildren(...builtin.map((d) => h("li", { text: d })));
    $("wl-builtin-count").textContent = `(${builtin.length})`;
    $("wl-referer").checked = !!referer;
    $("wl-referer").disabled = !editable;
    for (const el of $("wl-form").elements) el.disabled = !editable;
    if (!editable) $("wl-error").textContent = view.path
      ? `${view.path} isn't writable, so the whitelist can't be changed here.`
      : "No config file is set (CENSOR_CONFIG), so the whitelist can't be changed here.";
  }

  async function updateDomains(domains, message) {
    $("wl-error").textContent = "";
    try {
      renderWhitelist(await saveSettings({ "whitelist.domains": domains }));
      toast(message);
    } catch (err) {
      if (err.status !== 401) $("wl-error").textContent = err.message;
    }
  }

  $("wl-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const domain = normalizeDomain($("wl-input").value);
    if (!domain) return;
    if (whitelisted(cfgView, domain)) { $("wl-error").textContent = `${domain} is already whitelisted.`; return; }
    const domains = setting(cfgView, "whitelist", "domains").value || [];
    await updateDomains([...domains, domain], `${domain} whitelisted`);
    if (!$("wl-error").textContent) $("wl-input").value = "";
  });

  $("wl-referer").addEventListener("change", async (e) => {
    try {
      renderWhitelist(await saveSettings({ "whitelist.match_referer": e.target.checked }));
      toast("Saved");
    } catch (err) {
      e.target.checked = !e.target.checked;
      if (err.status !== 401) $("wl-error").textContent = err.message;
    }
  });

  // ---- detections -------------------------------------------------------

  let logData = null;

  async function loadDetections() {
    try { logData = await api("log"); } catch (err) {
      if (err.status !== 401) toast(err.message, true);
      return;
    }
    renderDetections();
  }

  function renderDetections() {
    if (!logData) return;
    const action = $("det-action").value;
    const q = $("det-host").value.trim().toLowerCase();
    const hits = $("det-hits").checked;
    const rows = logData.entries.filter((e) =>
      (!action || e.action === action)
      && (!q || (e.host || "").toLowerCase().includes(q) || (e.url || "").toLowerCase().includes(q))
      && (!hits || (e.detections && e.detections.length)));
    const levelNote = {
      off: "Detection logging is off (logging.level), so there's nothing to show.",
      info: "Logging at info: images with a detection, failures and errors, by hostname only.",
      debug: "Logging at debug: every image with its full URL, and image requests that weren't scanned.",
    }[logData.level] || "";
    $("det-note").textContent = `${levelNote} Showing ${rows.length} of the latest ${logData.entries.length} entries.`;
    $("det-body").replaceChildren(...rows.map((e) => h("tr", {},
      h("td", { class: "time", text: fmtTime(parseTs(e.ts)) }),
      h("td", { class: "host" }, e.host || "", e.url ? h("span", { class: "url", title: e.url, text: shorten(e.url, 140) }) : null,
        e.source ? h("span", { class: "reason", text: `${e.source}, ${e.mime || ""}` }) : h("span", { class: "reason", text: e.mime || "" })),
      h("td", {}, h("span", { class: `badge ${e.action}`, text: e.action }),
        e.reason ? h("span", { class: "reason", text: e.reason }) : null,
        e.cached ? h("span", { class: "reason", text: "cached" }) : null),
      h("td", {}, (e.detections || []).map((d) => h("span", {
        class: "det" + (d.censor ? " hit" : ""), title: d.why || "",
        text: `${d.censor ? "■ " : "□ "}${d.label} ${Number(d.score).toFixed(2)}${d.frame !== undefined ? ` (frame ${d.frame})` : ""}`,
      }))),
      h("td", { class: "num", text: e.elapsed_ms !== undefined ? Math.round(e.elapsed_ms) : "" }))));
  }

  for (const id of ["det-action", "det-hits"]) $(id).addEventListener("change", renderDetections);
  $("det-host").addEventListener("input", renderDetections);
  $("det-refresh").addEventListener("click", loadDetections);

  // ---- video ------------------------------------------------------------

  async function loadJobs(background = false) {
    let data;
    try { data = await api("jobs", { background }); } catch (err) {
      if (err.status !== 401 && !background) toast(err.message, true);
      return;
    }
    const jobs = data.jobs || [];
    $("jobs-empty").hidden = jobs.length > 0;
    $("jobs-body").replaceChildren(...jobs.map((j) => {
      const pct = j.state === "done" ? 100 : j.duration ? Math.min(100, 100 * j.seconds_done / j.duration) : 0;
      const bar = h("div", { class: "bar" }, h("span"));
      bar.firstChild.style.width = pct.toFixed(1) + "%";
      const active = j.state === "queued" || j.state === "running";
      return h("tr", {},
        h("td", {}, h("span", { class: `badge ${j.state}`, text: j.state }),
          j.error || j.cancel_reason ? h("span", { class: "reason", text: j.error || j.cancel_reason }) : null),
        h("td", {}, `${fmtClock(j.seconds_done)} / ${fmtClock(j.duration)}`, bar),
        h("td", { class: "num" }, fmtInt(j.frames_censored),
          j.seconds_male ? h("span", { class: "reason", text: `${fmtClock(j.seconds_male)} male` }) : null),
        h("td", { class: "host" }, hostOf(j.src),
          h("span", { class: "url", title: j.src, text: shorten(j.src, 120) }),
          j.page ? h("span", { class: "reason", title: j.page, text: `on ${hostOf(j.page) || shorten(j.page, 60)}` }) : null),
        h("td", { class: "time", text: fmtTime(new Date(j.created * 1000)) }),
        h("td", {}, active ? h("button", { class: "ghost", type: "button", text: "Cancel",
          onclick: async (e) => {
            e.target.disabled = true;
            try { await api(`jobs/${j.id}/cancel`, { method: "POST", body: {} }); toast("Job cancelled"); }
            catch (err) { if (err.status !== 401) toast(err.message, true); }
            loadJobs();
          } }) : null));
    }));
  }

  // ---- settings ---------------------------------------------------------

  const SKIP_SECTIONS = new Set(["whitelist"]);  // has its own tab
  // The level pickers and switches above the sections.
  const SKIP_KEYS = new Set(["detection.preset", "model.resources", "model.workers",
                             "model.threads_per_worker", "updates.enabled", "video.start_muted"]);
  // Pairs of keys shown as one model picker (choices from model.available).
  const MODEL_PICKERS = { model: ["path", "resolution"], video: ["model_path", "model_resolution"],
                          video_attribution: ["model_path", "model_resolution"] };
  const CUSTOM = "custom";
  const isNum = (v) => typeof v === "number";
  const title = (name) => name.charAt(0).toUpperCase() + name.slice(1);

  // A path of null is the bundled 320n, whatever the resolution says.
  function modelIndex(models, [path, resolution]) {
    return models.findIndex((m) => m.path === path && (path === null || m.resolution === resolution));
  }

  function kindOf(k) {
    if (k.models) return "model";
    const d = k.default, v = k.value;
    if (typeof d === "boolean") return "bool";
    if (isNum(d)) return "number";
    if (Array.isArray(d)) return d.length && d.every(isNum) ? "numlist" : "list";
    if (d && typeof d === "object") return "dict";
    if (Array.isArray(v)) return "list";
    return "string";
  }

  // Lists are one item per line; "" stands for an empty string.
  const listText = (v) => (v || []).map((x) => (x === "" ? '""' : String(x))).join("\n");
  const dictText = (v) => Object.entries(v || {}).map(([a, b]) => `${a}: ${b}`).join("\n");

  function readField(kind, el, k) {
    if (kind === "model") {
      if (el.value === "other") return k.value;
      const m = k.models[Number(el.value)];
      return [m.path, m.resolution];
    }
    if (kind === "bool") return el.checked;
    if (kind === "number") {
      if (el.value.trim() === "") throw new Error(`${k.key}: enter a number`);
      return Number(el.value);
    }
    if (kind === "string") return el.value === "" && k.nullable ? null : el.value;
    const lines = el.value.split("\n").map((l) => l.trim()).filter(Boolean);
    if (kind === "list") return lines.map((l) => (l === '""' ? "" : l));
    if (kind === "numlist") return lines.map((l) => {
      const n = Number(l);
      if (l === "" || isNaN(n)) throw new Error(`${k.key}: "${l}" isn't a number`);
      return n;
    });
    const out = {};
    for (const l of lines) {
      const i = l.indexOf(":");
      if (i < 1) throw new Error(`${k.key}: write each entry as KEY: value`);
      const key = l.slice(0, i).trim(), raw = l.slice(i + 1).trim();
      out[key] = raw !== "" && !isNaN(Number(raw)) ? Number(raw) : raw;
    }
    return out;
  }

  function fillField(kind, el, value) {
    if (kind === "model") {
      const i = modelIndex(el.models, value);
      el.value = i >= 0 ? String(i) : "other";
    } else if (kind === "bool") el.checked = !!value;
    else if (kind === "number") el.value = value;
    else if (kind === "string") el.value = value === null ? "" : value;
    else if (kind === "dict") el.value = dictText(value);
    else el.value = listText(value);
  }

  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const openSections = new Set();

  // The last /status seen, for the version and update result on the Updates card.
  let lastStatus = null;

  async function loadSettings() {
    $("cfg-error").textContent = "";
    let view;
    try {
      [view, lastStatus] = await Promise.all([fetchConfig(), api("status").catch(() => lastStatus)]);
    } catch (err) {
      if (err.status !== 401) $("cfg-error").textContent = err.message;
      return;
    }
    renderSettings(view);
  }

  function renderSettings(view) {
    $("cfg-path").textContent = view.path
      ? `Saved to ${view.path}${view.editable ? "" : " (read-only: changes can't be saved)"}. Changes apply at once unless marked “restart”.`
      : "No config file is set (CENSOR_CONFIG): settings can't be saved.";
    if (view.error) $("cfg-error").textContent = view.error;
    $("cfg-reload").disabled = !view.path;
    renderUpdates(view);
    renderSpeeds(view);
    renderResources(view);
    renderLevels(view);
    // Every individual setting is shown only in custom mode.
    const custom = view.preset.active === CUSTOM;
    $("cfg-file").hidden = !custom;
    renderSwitch("vid-muted", view, "video", "start_muted");
    $("cfg-sections").replaceChildren(...(custom ? view.sections : [])
      .filter((s) => !SKIP_SECTIONS.has(s.name)).map((s) => renderSection(s, view)));
  }

  // A checkbox bound to one boolean key, saved as soon as it is toggled.
  function renderSwitch(id, view, section, key) {
    const k = setting(view, section, key);
    const box = $(id);
    if (!box.matches(":focus")) box.checked = !!(k && k.value);
    box.disabled = !view.editable || !k;
  }

  function bindSwitch(id, errorId, section, key, label) {
    $(id).addEventListener("change", async (e) => {
      $(errorId).textContent = "";
      try {
        renderSettings(await saveSettings({ [`${section}.${key}`]: e.target.checked }));
        toast(`${label} ${e.target.checked ? "on" : "off"}`);
      } catch (err) {
        e.target.checked = !e.target.checked;
        if (err.status !== 401) $(errorId).textContent = err.message;
      }
    });
  }

  bindSwitch("upd-enabled", "upd-error", "updates", "enabled", "Automatic updates");
  bindSwitch("vid-muted", "vid-error", "video", "start_muted", "Videos start muted");

  // scs-update's states (src/dist/scs-update), as the panel words them.
  const UPDATE_STATES = { up_to_date: "Up to date", pending: "Update waiting", updated: "Updated",
                          failed: "Update failed", skipped: "Update skipped", disabled: "Off",
                          error: "Couldn't check" };

  function renderUpdates(view) {
    const s = lastStatus || {};
    const last = s.updates && s.updates.last;
    const rows = [["Version", s.version || (lastStatus ? "dev" : "unknown")]];
    if (!lastStatus) {
      rows.push(["Last check", "unknown (couldn't read the status)"]);
    } else if (!last) {
      rows.push(["Last check", "never (no update job has run on this computer yet)"]);
    } else {
      const when = parseTs(last.checked_at);
      rows.push(["Last check", when ? `${fmtTime(when)}: ${UPDATE_STATES[last.state] || last.state}` : "unknown"]);
      if (last.message) rows.push(["", last.message]);
    }
    $("info-updates").replaceChildren(...rows.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: v })]));
    renderSwitch("upd-enabled", view, "updates", "enabled");
  }

  // The image model switch: one choice per model.available entry.
  function renderSpeeds(view) {
    const current = [setting(view, "model", "path").value, setting(view, "model", "resolution").value];
    const active = modelIndex(view.models, current);
    $("speeds").replaceChildren(...view.models.map((m, i) => h("button", {
      type: "button", class: "level" + (i === active ? " active" : ""), role: "radio",
      "aria-checked": i === active ? "true" : "false", disabled: !view.editable || !m.installed,
      onclick: async () => {
        if (i === active) return;
        $("cfg-error").textContent = "";
        try {
          renderSettings(await saveSettings({ "model.path": m.path, "model.resolution": m.resolution }));
          toast(`Image model: ${m.label}`);
        } catch (err) {
          if (err.status !== 401) $("cfg-error").textContent = err.message;
        }
      },
    }, h("span", { class: "title", text: m.label }),
       h("span", { class: "desc", text: `${m.name}. ${m.description}${m.installed ? "" : " Not installed."}` }))));
  }

  // The resource consumption switch: one choice per model.resource_levels
  // entry, and custom, which takes workers and threads from the inputs below.
  function renderResources(view) {
    const r = view.resources;
    // Custom's own numbers only once it's chosen: choosing it starts from the level.
    const options = [...r.options, {
      name: CUSTOM, ...(r.active === CUSTOM && { workers: r.workers, threads_per_worker: r.threads_per_worker }),
      description: "Set the workers and threads yourself. Starts from the level you had.",
    }];
    const choose = async (o) => {
      if (o.name === r.active) return;
      $("cfg-error").textContent = "";
      try {
        renderSettings(await saveSettings({ "model.resources": o.name }));
        toast(`Resource consumption: ${o.label || title(o.name)}`);
      } catch (err) {
        if (err.status !== 401) $("cfg-error").textContent = err.message;
      }
    };
    $("resources").replaceChildren(...options.map((o) => h("button", {
      type: "button", class: "level" + (o.name === r.active ? " active" : ""), role: "radio",
      "aria-checked": o.name === r.active ? "true" : "false", disabled: !view.editable,
      onclick: () => choose(o),
    }, h("span", { class: "title", text: o.label || title(o.name) }),
       o.workers ? h("span", { class: "desc", text: `${plural(o.workers, "worker")}, ${plural(o.threads_per_worker, "thread")}.` }) : null,
       o.description ? h("span", { class: "desc", text: o.description }) : null)));

    const custom = r.active === CUSTOM;
    $("resources-custom").hidden = !custom;
    if (!custom) return;
    const input = (value) => h("input", { type: "number", min: "1", step: "1", value: String(value),
                                          disabled: !view.editable });
    const workers = input(r.workers), threads = input(r.threads_per_worker);
    const save = h("button", { type: "button", class: "primary", text: "Save", disabled: !view.editable,
      onclick: async () => {
        $("cfg-error").textContent = "";
        try {
          renderSettings(await saveSettings({ "model.workers": Number(workers.value),
                                              "model.threads_per_worker": Number(threads.value) }));
          toast("Resource consumption saved");
        } catch (err) {
          if (err.status !== 401) $("cfg-error").textContent = err.message;
        }
      } });
    $("resources-custom").replaceChildren(
      h("label", {}, "Workers ", workers), h("label", {}, "Threads per worker ", threads), save);
  }

  function renderLevels(view) {
    const active = view.preset.active;
    const options = [...view.preset.options, {
      name: CUSTOM,
      description: "Shows every setting below so you can set each one yourself. Starts from the level you had.",
    }];
    $("levels").replaceChildren(...options.map((o) => h("button", {
      type: "button", class: "level" + (o.name === active ? " active" : ""), role: "radio",
      "aria-checked": o.name === active ? "true" : "false", disabled: !view.editable,
      onclick: async () => {
        if (o.name === active) return;
        $("cfg-error").textContent = "";
        try {
          renderSettings(await saveSettings({ "detection.preset": o.name }));
          toast(`Censoring level: ${o.label || title(o.name)}`);
        } catch (err) {
          if (err.status !== 401) $("cfg-error").textContent = err.message;
        }
      },
    }, h("span", { class: "title", text: o.label || title(o.name) }),
       o.censors && o.censors.length
         ? h("ul", { class: "censors" }, ...o.censors.map((c) => h("li", { text: c }))) : null,
       o.description ? h("span", { class: "desc", text: o.description }) : null)));
  }

  // The section's keys, with each model picker's pair of keys as one field.
  function sectionFields(section, view) {
    const pair = MODEL_PICKERS[section.name];
    const keys = section.keys.filter((k) => !SKIP_KEYS.has(`${section.name}.${k.key}`));
    if (!pair) return keys;
    const [a, b] = pair.map((name) => keys.find((k) => k.key === name));
    if (!a || !b) return keys;
    const picker = {
      key: "model", keys: pair, models: view.models,
      value: [a.value, b.value], default: [a.default, b.default],
      help: "One of the models baked into the image (model.available).",
    };
    return keys.flatMap((k) => (k === a ? [picker] : k === b ? [] : [k]));
  }

  function renderSection(section, view) {
    const editable = view.editable;
    const fields = [];
    const status = h("span", { class: "muted small" });
    const save = h("button", { type: "button", class: "primary", text: "Save", disabled: true });
    const discard = h("button", { type: "button", class: "ghost", text: "Discard", disabled: true });
    const overridden = section.keys.filter((k) => !same(k.value, k.default)).length;
    const details = h("details", { class: "section", open: openSections.has(section.name) },
      h("summary", {}, section.name,
        h("span", { class: "count", text: overridden ? `${overridden} changed from default` : "defaults" })),
      h("div", { class: "fields" }));
    details.addEventListener("toggle", () => {
      if (details.open) openSections.add(section.name); else openSections.delete(section.name);
    });

    const refresh = () => {
      let dirty = 0;
      for (const f of fields) {
        let changed;
        try { changed = !same(readField(f.kind, f.el, f.k), f.k.value); } catch (_) { changed = true; }
        f.row.classList.toggle("changed", changed);
        dirty += changed;
      }
      save.disabled = !editable || !dirty;
      discard.disabled = !dirty;
      status.textContent = dirty ? `${dirty} unsaved change${dirty === 1 ? "" : "s"}` : "";
    };

    for (const k of sectionFields(section, view)) {
      const kind = kindOf(k);
      let el;
      if (kind === "model") {
        el = h("select", { class: "combo" }, k.models.map((m, i) => h("option", {
          value: String(i), disabled: !m.installed,
          text: `${m.label} (${m.name}): ${m.description}${m.installed ? "" : " (not installed)"}`,
        })));
        if (modelIndex(k.models, k.value) < 0) {
          el.append(h("option", { value: "other", text: `Other: ${k.value[0] || "bundled"} at ${k.value[1]}` }));
        }
        el.models = k.models;
      } else if (kind === "bool") el = h("input", { type: "checkbox" });
      else if (kind === "number") el = h("input", { type: "number", step: "any" });
      else if (kind === "string") el = h("input", { type: "text", spellcheck: "false", placeholder: k.nullable ? "null" : "" });
      else el = h("textarea", { spellcheck: "false", rows: Math.min(8, Math.max(2, (k.value && Object.keys(k.value).length || 1) + 1)) });
      el.disabled = !editable;
      el.setAttribute("aria-label", `${section.name}.${k.key}`);
      fillField(kind, el, k.value);
      el.addEventListener("input", refresh);
      el.addEventListener("change", refresh);

      const resetBtn = h("button", { type: "button", class: "link", text: "Use default",
        onclick: () => { fillField(kind, el, k.default); refresh(); } });
      const defaultModel = kind === "model" ? k.models[modelIndex(k.models, k.default)] : null;
      const defaultText = kind === "model" ? (defaultModel ? defaultModel.name : String(k.default[0]))
        : kind === "dict" ? dictText(k.default) : Array.isArray(k.default) ? listText(k.default).replace(/\n/g, ", ")
        : k.default === null ? "null" : String(k.default);
      const row = h("div", { class: "field" },
        h("div", {},
          h("div", { class: "key" }, k.key,
            k.restart ? h("span", { class: "badge restart", title: "Read at startup", text: k.pending_restart ? "restart pending" : "restart" }) : null),
          k.help ? h("div", { class: "help", text: k.help }) : null),
        h("div", { class: "control" },
          kind === "bool" ? h("label", { class: "check inline" }, el, h("span", { text: "on" })) : el,
          h("div", { class: "meta" },
            h("span", { text: `default: ${shorten(defaultText, 80) || '""'}` }),
            !same(k.value, k.default) && editable ? resetBtn : null,
            kind === "list" || kind === "numlist" ? h("span", { text: "one per line" }) : null,
            kind === "dict" ? h("span", { text: "KEY: value per line" }) : null)));
      fields.push({ k, kind, el, row });
      details.lastChild.append(row);
    }

    save.addEventListener("click", async () => {
      $("cfg-error").textContent = "";
      const set = {};
      try {
        for (const f of fields) {
          const v = readField(f.kind, f.el, f.k);
          if (same(v, f.k.value)) continue;
          if (f.kind === "model") f.k.keys.forEach((key, i) => { set[`${section.name}.${key}`] = v[i]; });
          else set[`${section.name}.${f.k.key}`] = v;
        }
      } catch (err) { $("cfg-error").textContent = err.message; return; }
      save.disabled = true;
      status.textContent = "Saving…";
      try {
        const view = await saveSettings(set);
        const restart = Object.keys(set).filter((key) => {
          const [s, kk] = key.split(".");
          const k = setting(view, s, kk);
          return k && k.restart;
        });
        openSections.add(section.name);
        renderSettings(view);
        toast(restart.length ? `Saved. Restart the container to apply ${restart.join(", ")}.` : "Saved and applied");
      } catch (err) {
        if (err.status !== 401) $("cfg-error").textContent = err.message;
        refresh();
      }
    });
    discard.addEventListener("click", () => {
      for (const f of fields) fillField(f.kind, f.el, f.k.value);
      refresh();
    });
    details.lastChild.append(h("div", { class: "section-actions" }, save, discard, status));
    return details;
  }

  $("cfg-reload").addEventListener("click", async () => {
    $("cfg-error").textContent = "";
    try {
      renderSettings(await api("config/reload", { method: "POST", body: {} }));
      toast("Reloaded from file and applied");
    } catch (err) {
      if (err.status !== 401) $("cfg-error").textContent = err.message;
    }
  });

  // ---- PIN --------------------------------------------------------------

  async function loadLock() {
    try { session = await api("session"); } catch (_) { return; }
    for (const id of ["pin-current", "pin-new", "pin-confirm"]) $(id).value = "";
    $("pin-error").textContent = "";
    $("pin-ok").textContent = "";
    const set = session.pin_set;
    $("pin-title").textContent = set ? "PIN lock is on" : "PIN lock is off";
    $("pin-desc").textContent = set
      ? "The panel asks for the PIN when opened, and locks again after a while without use. Change or remove the PIN below; both need the current one."
      : "Set a PIN to lock the panel. Anyone opening it will need the PIN to see or change anything.";
    $("pin-current").hidden = !set;
    $("pin-current").required = set;
    $("pin-save").textContent = set ? "Change PIN" : "Set PIN";
    $("pin-remove").hidden = !set;
    $("lock-now").hidden = !set;
  }

  async function submitPin(body, message) {
    $("pin-error").textContent = "";
    $("pin-ok").textContent = "";
    try {
      await api("pin", { method: "POST", body });
      await loadLock();
      $("pin-ok").textContent = message;
    } catch (err) {
      if (err.status === 401) return;
      $("pin-error").textContent = err.message;
    }
  }

  $("pin-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const pin = $("pin-new").value;
    if (!/^\d{4,12}$/.test(pin)) { $("pin-error").textContent = "A PIN is 4 to 12 digits."; return; }
    if (pin !== $("pin-confirm").value) { $("pin-error").textContent = "The two new PINs don't match."; return; }
    submitPin({ current: $("pin-current").value, new: pin }, session.pin_set ? "PIN changed." : "PIN set. The panel is now locked when opened.");
  });

  $("pin-remove").addEventListener("click", () => {
    if (!$("pin-current").value) { $("pin-error").textContent = "Enter the current PIN to remove it."; return; }
    submitPin({ current: $("pin-current").value, new: null }, "PIN removed. The panel is no longer locked.");
  });

  // ---- start ------------------------------------------------------------

  window.addEventListener("hashchange", () => {
    const wanted = location.hash.slice(1);
    if (session.unlocked && TABS.includes(wanted) && wanted !== current) showTab(wanted);
  });

  (async () => {
    try { session = await api("session"); } catch (err) { toast(err.message, true); return; }
    if (session.pin_set && !session.unlocked) showLocked();
    else showApp();
  })();
})();
