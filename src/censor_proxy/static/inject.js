// Censor proxy video control. Injected at the top of <head> on allowlisted
// sites, before the page's own scripts, so it can see every <video> (including
// inside shadow roots) and the HLS/DASH manifests players fetch.
//
// Every video is paused and covered. "Play censored" asks the proxy for a
// censored copy and plays it in our own <video> laid over the original.
// Videos whose source can't be found stay covered.
(() => {
  "use strict";
  if (window.__censorProxy) return;

  const ORIGIN = "__CENSOR_ORIGIN__";
  const DEBUG = "__DEBUG__" === "true";
  const MAX_HEIGHT = Number("__MAX_HEIGHT__") || 720;  // video.max_height

  // Debug mode reports what the script sees to the proxy log.
  function report(event, data) {
    if (!DEBUG) return;
    try {
      navigator.sendBeacon(ORIGIN + "/debug",
        JSON.stringify({ event, page: location.href, ...data }));
    } catch (_) { /* best effort */ }
  }

  // ---- Branding: everything visual lives here --------------------------
  // Sissy Censor System: plum ground, petal accent, the 3x3 mosaic mark.
  // The brand fonts are used only if installed locally (the proxy makes no
  // external requests); otherwise the system fonts.
  const MOSAIC = [.25, .6, .25, .6, 1, .6, .25, .6, .25];
  const STROKE = { fill: "none", stroke: "currentColor", "stroke-width": 1.6,
                   "stroke-linecap": "round", "stroke-linejoin": "round" };
  const BRAND = {
    name: "__BRAND_NAME__",
    strings: {
      idle: "This video is paused and covered.",
      play: "Play censored",
      preparing: "Censoring video…",
      unavailable: "This video can't be censored",
      unavailableDetail: "It stays covered. The page didn't expose a source the proxy can fetch.",
      failed: "Couldn't censor this video",
      retry: "Try again",
    },
    // Icons as [viewBox, svg attributes, [tag, attributes]...], built with
    // DOM calls (innerHTML is blocked on Trusted Types pages). Coloured by
    // CSS through currentColor.
    icons: {
      mark: ["0 0 46 46", { fill: "currentColor" }, ...MOSAIC.map((o, i) => ["rect",
        { x: i % 3 * 16, y: Math.floor(i / 3) * 16, width: 14, height: 14, rx: 3, opacity: o }])],
      play: ["0 0 14 14", { fill: "currentColor" }, ["path", { d: "M3 1.8v10.4L12 7z" }]],
      retry: ["0 0 24 24", { ...STROKE, "stroke-width": 2 },
        ["path", { d: "M20 11a8 8 0 1 0-2.3 5.7" }], ["path", { d: "M20 5v6h-6" }]],
      unavailable: ["0 0 24 24", STROKE,
        ["path", { d: "M2.5 12s3.5-6.5 9.5-6.5 9.5 6.5 9.5 6.5-3.5 6.5-9.5 6.5S2.5 12 2.5 12z" }],
        ["circle", { cx: 12, cy: 12, r: 2.8 }], ["path", { d: "M4 20 20 4" }]],
      failed: ["0 0 24 24", STROKE,
        ["circle", { cx: 12, cy: 12, r: 9 }], ["path", { d: "M12 7.5v5.5" }], ["path", { d: "M12 16.5h.01" }]],
    },
    css: `
      :host { all: initial; }
      .card {
        --censor-bg: #16121A;
        --censor-fg: #F7F1F5;
        --censor-muted: #A898A6;
        --censor-accent: #FF6FAE;
        --censor-on-accent: #16121A;
        --censor-line: #4A3D52;
        --censor-track: #FFFFFF22;
        --censor-radius: 10px;
        --censor-font: "Bricolage Grotesque", system-ui, -apple-system, "Segoe UI", sans-serif;
        --censor-mono: "IBM Plex Mono", ui-monospace, "SF Mono", Menlo, monospace;
        position: absolute; inset: 0; box-sizing: border-box; overflow: hidden;
        container-type: size;
        display: flex; flex-direction: column; align-items: center; justify-content: center;
        gap: 16px; padding: 12px; text-align: center;
        background: var(--censor-bg); color: var(--censor-fg);
        font: 16px/1.35 var(--censor-font); border-radius: var(--censor-radius);
        pointer-events: auto;
      }
      .card [hidden] { display: none !important; }
      svg { display: block; }
      .brand { display: flex; align-items: center; gap: 10px; color: var(--censor-accent); }
      .brand svg { width: 20px; height: 20px; }
      .brand span {
        font: 500 11px/1 var(--censor-mono); letter-spacing: .14em; text-transform: uppercase;
        color: var(--censor-muted);
      }
      .icon { width: 36px; height: 36px; color: var(--censor-muted); }
      .icon.failed { color: var(--censor-accent); }
      .msg { max-width: 90%; font-weight: 600; font-size: 18px; }
      .msg.idle { font-weight: 400; font-size: 16px; color: var(--censor-muted); }
      .detail { max-width: 70%; font-size: 14px; color: var(--censor-muted); }
      button {
        display: flex; align-items: center; gap: 8px;
        font: 600 15px/1 var(--censor-font); color: var(--censor-on-accent);
        background: var(--censor-accent); border: 0; border-radius: 999px;
        padding: 13px 22px 13px 18px; min-height: 44px; box-sizing: border-box; cursor: pointer;
      }
      button svg { width: 14px; height: 14px; }
      button.secondary {
        color: var(--censor-fg); background: transparent;
        border: 1.5px solid var(--censor-line); padding: 12px 20px 12px 16px;
      }
      button.secondary svg { width: 15px; height: 15px; }
      button:focus-visible { outline: 2px solid var(--censor-fg); outline-offset: 2px; }
      .bar { width: 55%; height: 4px; border-radius: 2px; background: var(--censor-track); overflow: hidden; }
      .bar > i { display: block; height: 100%; width: 0; background: var(--censor-accent); transition: width .3s; }
      video { position: absolute; inset: 0; width: 100%; height: 100%; background: #000; }
      .card.playing { padding: 0; }
      .card.playing > :not(video) { display: none !important; }
      /* Small players: the brand name goes first, then the extra copy; the
         button always stays. */
      @container (max-width: 380px) or (max-height: 230px) {
        .card { gap: 12px; }
        .brand span, .detail { display: none; }
        .icon { width: 28px; height: 28px; }
        .msg { font-size: 15px; }
        .msg.idle { font-size: 14px; }
        button { font-size: 14px; padding: 12px 18px 12px 15px; }
      }
      @container (max-width: 260px) or (max-height: 150px) {
        .card { gap: 10px; }
        .msg.idle, .icon { display: none; }
        button { font-size: 13px; padding: 11px 16px 11px 13px; }
      }
    `,
  };
  // ---------------------------------------------------------------------

  const MANIFEST = /\.(m3u8|mpd)(\?|#|$)/i;
  const FILE = /\.(mp4|webm|m4v|mov)(\?|#|$)/i;
  const ours = new WeakSet();       // our own players, allowed to play
  const guarded = new Map();        // page <video> -> card state
  const manifests = [];             // manifest URLs seen, newest last

  window.__censorProxy = { version: 1 };

  // ---- Remember manifests the page's players fetch ----------------------
  function note(url) {
    try {
      const u = new URL(url, location.href).href;
      if (MANIFEST.test(u) && !manifests.includes(u)) manifests.push(u);
    } catch (_) { /* not a URL */ }
  }
  const origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function (input, init) {
      note(typeof input === "string" ? input : input && input.url);
      return origFetch.apply(this, arguments);
    };
  }
  const origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    note(url);
    return origOpen.apply(this, arguments);
  };

  // ---- Block playback until censored ------------------------------------
  const origPlay = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function () {
    if (this instanceof HTMLVideoElement && !ours.has(this)) {
      guard(this);
      this.pause();
      return Promise.reject(new DOMException("Paused until censored", "NotAllowedError"));
    }
    return origPlay.apply(this, arguments);
  };

  // ---- Find videos, including inside shadow roots -----------------------
  const origAttachShadow = Element.prototype.attachShadow;
  Element.prototype.attachShadow = function () {
    const root = origAttachShadow.apply(this, arguments);
    watch(root);
    return root;
  };

  function scan(node) {
    if (node instanceof HTMLVideoElement) guard(node);
    else if (node.querySelectorAll) node.querySelectorAll("video").forEach(guard);
  }

  function watch(root) {
    scan(root);
    new MutationObserver((records) => {
      for (const r of records) r.addedNodes.forEach(scan);
    }).observe(root, { childList: true, subtree: true });
  }

  // ---- Cards -------------------------------------------------------------
  // Each card lives right next to its video, in the same tree, so it shares
  // whatever dialog, top layer or stacking context the video is in (a card in
  // a separate page-level layer is hidden under modal dialogs). A closed
  // shadow root keeps page CSS out.
  const HOST_CSS = "position:fixed;z-index:2147483647;display:block;margin:0;padding:0;" +
    "border:0;pointer-events:auto;contain:layout;";

  function makeHost(card) {
    const host = document.createElement("censor-proxy-card");
    host.style.cssText = HOST_CSS.replace(/;/g, " !important;");
    const root = origAttachShadow.call(host, { mode: "closed" });
    const style = document.createElement("style");
    style.textContent = BRAND.css;
    root.append(style, card);
    return host;
  }

  let positioning = false;
  function startPositioning() {
    if (!positioning) {
      positioning = true;
      requestAnimationFrame(position);
    }
  }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text) e.textContent = text;
    return e;
  }

  const SVG_NS = "http://www.w3.org/2000/svg";
  function icon(name) {
    const [viewBox, attrs, ...children] = BRAND.icons[name];
    const make = (tag, a) => {
      const e = document.createElementNS(SVG_NS, tag);
      for (const [k, v] of Object.entries(a)) e.setAttribute(k, v);
      return e;
    };
    const svg = make("svg", { viewBox, "aria-hidden": "true", ...attrs });
    for (const [tag, a] of children) svg.appendChild(make(tag, a));
    return svg;
  }

  function describe(video) {
    const r = video.getBoundingClientRect();
    return { size: [Math.round(r.width), Math.round(r.height)],
             src: (video.currentSrc || video.src || "").slice(0, 200),
             inShadow: video.getRootNode() !== document };
  }

  const reported = new Set();

  function guard(video) {
    if (ours.has(video) || guarded.has(video)) return;
    const info = describe(video);
    if (!reported.has(info.src)) {  // sites that recreate players would flood the log
      reported.add(info.src);
      report("guard", info);
    }
    video.autoplay = false;
    video.pause();
    video.style.setProperty("opacity", "0", "important");
    video.addEventListener("play", () => { if (!ours.has(video)) video.pause(); }, true);

    const card = el("div", "card");
    const brand = el("div", "brand");
    brand.appendChild(icon("mark"));
    brand.appendChild(el("span", "", BRAND.name));
    const badge = el("div", "icon");
    const msg = el("div", "msg");
    const detail = el("div", "detail");
    const bar = el("div", "bar");
    bar.appendChild(el("i"));
    const button = el("button");
    button.type = "button";
    button.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      start(video, state);
    });
    card.append(brand, badge, msg, detail, button, bar);
    const state = { card, host: makeHost(card), brand, badge, msg, detail, bar, button, player: null, jobId: null };
    show(state, "idle");
    guarded.set(video, state);
    startPositioning();
  }

  function show(state, mode, why) {
    const { card, brand, badge, msg, detail, bar, button } = state;
    const failed = mode === "failed";
    card.classList.toggle("playing", mode === "playing");
    // Failure states lead with their icon; the brand line moves below.
    const iconName = { unavailable: "unavailable", failed: "failed" }[mode];
    badge.hidden = !iconName;
    if (iconName) {
      badge.className = "icon " + iconName;
      badge.replaceChildren(icon(iconName));
    }
    card.append(...(iconName ? [badge, msg, detail, button, bar, brand] : [brand, badge, msg, detail, button, bar]),
                ...(state.player ? [state.player] : []));
    bar.hidden = mode !== "preparing";
    button.hidden = !(mode === "idle" || failed);
    button.className = failed ? "secondary" : "";
    button.replaceChildren(icon(failed ? "retry" : "play"), failed ? BRAND.strings.retry : BRAND.strings.play);
    msg.className = mode === "idle" ? "msg idle" : "msg";
    msg.textContent = {
      idle: BRAND.strings.idle,
      preparing: BRAND.strings.preparing,
      unavailable: BRAND.strings.unavailable,
      failed: BRAND.strings.failed,
      playing: "",
    }[mode];
    msg.hidden = !msg.textContent;
    detail.textContent = mode === "unavailable" ? BRAND.strings.unavailableDetail : (why || "");
    detail.hidden = !detail.textContent;
  }

  // The box to cover: the video's own, or, while the video has no size yet
  // (common before a player loads), the nearest enclosing element that has
  // one, crossing shadow roots.
  function coverRect(video) {
    let node = video;
    for (let depth = 0; node && depth < 8; depth++) {
      if (node.getBoundingClientRect) {
        const r = node.getBoundingClientRect();
        if (r.width >= 40 && r.height >= 40) return r;
      }
      node = node.parentElement || (node.getRootNode && node.getRootNode().host) || null;
    }
    return null;
  }

  // Keep each card over its video. Cards for videos that are gone or
  // can't be placed are hidden.
  function position() {
    for (const [video, state] of guarded) {
      const host = state.host;
      const r = video.isConnected ? coverRect(video) : null;
      const visible = r && r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
      if (!video.isConnected) {
        // The page removed the video (some sites recreate players on every
        // hover); drop its card so they don't pile up.
        if (state.player) state.player.pause();
        release(state);
        host.remove();
        guarded.delete(video);
        continue;
      }
      if (!visible) {
        host.style.setProperty("display", "none", "important");
        continue;
      }
      if (host.previousSibling !== video) video.after(host);  // (re)attach next to the video
      const s = host.style;
      s.setProperty("display", "block", "important");
      s.setProperty("width", r.width + "px", "important");
      s.setProperty("height", r.height + "px", "important");
      s.setProperty("left", r.left + "px", "important");
      s.setProperty("top", r.top + "px", "important");
      // position:fixed is relative to a transformed ancestor if there is
      // one; measure where the card landed and correct for the offset.
      const h = host.getBoundingClientRect();
      if (Math.abs(h.left - r.left) > 0.5 || Math.abs(h.top - r.top) > 0.5) {
        s.setProperty("left", (2 * r.left - h.left) + "px", "important");
        s.setProperty("top", (2 * r.top - h.top) + "px", "important");
      }
    }
    requestAnimationFrame(position);
  }

  // ---- Source discovery ------------------------------------------------
  // The page itself is never the video: an empty src="" resolves to it.
  function httpUrl(value) {
    if (!value) return null;
    try {
      const u = new URL(value, location.href);
      if (u.protocol !== "https:" && u.protocol !== "http:") return null;
      return u.href.split("#")[0] === location.href.split("#")[0] ? null : u.href;
    } catch (_) { return null; }
  }

  // Elements around the video, crossing shadow roots.
  function* around(video) {
    let node = video;
    for (let depth = 0; node && depth < 10; depth++) {
      yield node;
      node = node.parentElement || (node.getRootNode && node.getRootNode().host) || null;
    }
  }

  function attributeUrl(video, pattern) {
    for (const node of around(video)) {
      for (const a of node.attributes || []) {
        if (pattern.test(a.value)) {
          const u = httpUrl(a.value);
          if (u) return u;
        }
      }
    }
    return null;
  }

  // Reddit's players load a low-resolution rendition file directly (e.g.
  // CMAF_96.mp4 is 96x170); the HLS playlist next to it has every rendition
  // and the audio.
  function siteHint(url) {
    const m = url && /^https:\/\/v\.redd\.it\/([a-z0-9]+)\//i.exec(url);
    return m ? `https://v.redd.it/${m[1]}/HLSPlaylist.m3u8` : null;
  }

  // Players that fetch their manifest only once playback starts (Pornhub's)
  // never show it to note(); the page's inline config usually lists it,
  // often JSON-escaped and once per quality. Picks the tallest rendition the
  // proxy won't scale down (heights read from names like 720P_4000K), else
  // the smallest.
  const INLINE_MANIFEST = /https?:\\?\/\\?\/[^"'\s<>]+?\.(?:m3u8|mpd)(?:\?[^"'\s<>]*)?(?=["'\s<>]|$)/gi;
  function pageManifest() {
    const found = new Set();
    for (const s of document.querySelectorAll("script:not([src])")) {
      for (const m of s.textContent.matchAll(INLINE_MANIFEST)) {
        const u = httpUrl(m[0].replace(/\\\//g, "/").replace(/\\u0026/gi, "&"));
        if (u) found.add(u);
      }
    }
    const height = (u) => +((/(?:^|[^0-9])(\d{3,4})p(?![a-z])/i.exec(new URL(u).pathname) || [])[1] || 0);
    const urls = [...found].sort((a, b) => height(a) - height(b));
    const fits = urls.filter((u) => height(u) <= MAX_HEIGHT);
    return fits.length ? fits[fits.length - 1] : urls[0] || null;
  }

  function findSource(video) {
    // Raw attributes: the .src properties turn src="" into the page URL.
    const source = video.querySelector("source[src]");
    const direct = httpUrl(video.currentSrc) || httpUrl(video.getAttribute("src")) ||
      httpUrl(source && source.getAttribute("src"));
    return attributeUrl(video, MANIFEST) ||   // a player's manifest beats any single file
      siteHint(direct) ||
      direct ||
      attributeUrl(video, FILE) ||
      (manifests.length ? manifests[manifests.length - 1] : null) ||
      pageManifest();
  }

  // ---- Censoring -------------------------------------------------------
  async function start(video, state) {
    const src = findSource(video);
    report("start", { ...describe(video), found: src, manifests: manifests.slice(-3) });
    if (!src) return show(state, "unavailable");
    show(state, "preparing");
    try {
      const res = await origFetch.call(window, ORIGIN + "/video/jobs", {
        method: "POST",
        body: JSON.stringify({ src, page: location.href }),
        headers: { "Content-Type": "text/plain" },
      });
      let job = await res.json();
      if (!res.ok) throw new Error(job.error || res.status);
      state.jobId = job.id;
      while (!job.ready) {
        if (job.state === "error") throw new Error(job.error);
        state.msg.textContent = job.state === "queued" ? BRAND.strings.queued : BRAND.strings.preparing;
        if (job.duration) state.bar.firstChild.style.width =
          Math.min(100, 100 * job.seconds_done / job.duration) + "%";
        await new Promise((r) => setTimeout(r, 1000));
        job = await (await origFetch.call(window, statusUrl(state))).json();
      }
      play(video, state, job.id);
    } catch (e) {
      report("failed", { error: String(e && e.message || e) });
      show(state, "failed", String(e && e.message || e).slice(0, 80));
    }
  }

  // ---- Telling the proxy what we still want ----------------------------
  // The proxy cancels jobs nobody asks about (video.abandon_seconds), and
  // lets a job whose video is paused or in a background tab give its slot
  // to one someone is waiting on (video.idle_seconds). Heartbeats say which.
  const HEARTBEAT_MS = 2000;

  function active(state) {
    const p = state.player;
    return document.visibilityState === "visible" && !(p && (p.paused || p.ended));
  }

  function statusUrl(state) {
    return ORIGIN + "/video/" + state.jobId + "/status?active=" + (active(state) ? 1 : 0);
  }

  function beat(state) {
    if (!state.jobId) return Promise.resolve(null);
    return origFetch.call(window, statusUrl(state)).then((r) => r.json()).catch(() => null);
  }

  // The page no longer needs this job: free its slot now rather than after
  // the timeout. A beacon still goes out while the page is unloading.
  function release(state) {
    if (!state.jobId) return;
    try {
      navigator.sendBeacon(ORIGIN + "/video/" + state.jobId + "/cancel", "");
    } catch (_) { /* the timeout catches it */ }
    state.jobId = null;
  }

  addEventListener("pagehide", () => guarded.forEach(release));
  document.addEventListener("visibilitychange", () => guarded.forEach(beat));

  function play(video, state, id) {
    const url = ORIGIN + "/video/" + id + "/stream";
    const player = document.createElement("video");
    const timer = setInterval(() => {
      if (!player.isConnected) return clearInterval(timer);
      beat(state);
    }, HEARTBEAT_MS);
    player.addEventListener("pause", () => beat(state));
    // Resuming after the proxy stopped the job (paused while another video
    // waited) would just run out where the job stopped; say so instead.
    player.addEventListener("play", async () => {
      const job = await beat(state);
      if (job && job.state === "error" && player.isConnected) {
        player.pause();
        player.remove();
        state.player = null;
        show(state, "failed", job.error.replace(/^\w+: /, ""));
      }
    });
    ours.add(player);
    player.controls = true;
    player.playsInline = true;
    player.muted = video.muted;
    player.src = url;
    player.addEventListener("error", () => {
      report("player-error", { code: player.error && player.error.code, url });
      player.remove();
      state.player = null;
      show(state, "failed");
    });
    state.card.appendChild(player);
    state.player = player;
    show(state, "playing");
    player.play().catch(() => { /* autoplay refused; controls let the user start it */ });
  }

  watch(document);
})();
