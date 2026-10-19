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

  // ---- Branding: everything visual lives here --------------------------
  const BRAND = {
    name: "__BRAND_NAME__",
    strings: {
      play: "Play censored",
      preparing: "Censoring video…",
      unavailable: "This video can't be censored",
      failed: "Couldn't censor this video",
      retry: "Try again",
    },
    css: `
      :host { all: initial; }
      .card {
        --censor-bg: #111418;
        --censor-fg: #f2f4f7;
        --censor-muted: #9aa3ad;
        --censor-accent: #4f7cff;
        --censor-radius: 10px;
        --censor-font: system-ui, -apple-system, "Segoe UI", sans-serif;
        position: fixed; box-sizing: border-box; overflow: hidden;
        display: flex; flex-direction: column; align-items: center; justify-content: center;
        gap: 10px; padding: 12px; text-align: center;
        background: var(--censor-bg); color: var(--censor-fg);
        font: 14px/1.3 var(--censor-font); border-radius: var(--censor-radius);
        pointer-events: auto; z-index: 2147483647;
      }
      .card[hidden] { display: none; }
      .brand { font-size: 12px; letter-spacing: .04em; text-transform: uppercase; color: var(--censor-muted); }
      .msg { max-width: 90%; }
      button {
        font: 600 14px/1 var(--censor-font); color: #fff; background: var(--censor-accent);
        border: 0; border-radius: 999px; padding: 10px 18px; cursor: pointer;
      }
      button:focus-visible { outline: 2px solid var(--censor-fg); outline-offset: 2px; }
      .bar { width: 60%; height: 4px; border-radius: 2px; background: #ffffff22; overflow: hidden; }
      .bar > i { display: block; height: 100%; width: 0; background: var(--censor-accent); transition: width .3s; }
      video { position: absolute; inset: 0; width: 100%; height: 100%; background: #000; }
      .card.playing { padding: 0; }
      .card.playing > :not(video) { display: none; }
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

  // ---- Overlay layer -----------------------------------------------------
  let layer = null;
  function getLayer() {
    if (layer) return layer;
    const host = document.createElement("censor-proxy-layer");
    host.style.cssText = "position:fixed;inset:0;pointer-events:none;z-index:2147483647;";
    const root = origAttachShadow.call(host, { mode: "closed" });
    const style = document.createElement("style");
    style.textContent = BRAND.css;
    root.appendChild(style);
    document.documentElement.appendChild(host);
    layer = root;
    requestAnimationFrame(position);
    return layer;
  }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text) e.textContent = text;
    return e;
  }

  function guard(video) {
    if (ours.has(video) || guarded.has(video)) return;
    video.autoplay = false;
    video.pause();
    video.style.setProperty("opacity", "0", "important");
    video.addEventListener("play", () => { if (!ours.has(video)) video.pause(); }, true);

    const card = el("div", "card");
    card.hidden = true;
    card.appendChild(el("div", "brand", BRAND.name));
    const msg = el("div", "msg");
    const bar = el("div", "bar");
    bar.appendChild(el("i"));
    const button = el("button", "", BRAND.strings.play);
    button.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      start(video, state);
    });
    card.append(msg, button, bar);
    const state = { card, msg, bar, button, player: null };
    show(state, "idle");
    guarded.set(video, state);
    const attach = () => getLayer().appendChild(card);
    if (document.documentElement) attach();
    else document.addEventListener("DOMContentLoaded", attach, { once: true });
  }

  function show(state, mode, detail) {
    const { card, msg, bar, button } = state;
    card.classList.toggle("playing", mode === "playing");
    bar.hidden = mode !== "preparing";
    button.hidden = !(mode === "idle" || mode === "failed");
    button.textContent = mode === "failed" ? BRAND.strings.retry : BRAND.strings.play;
    msg.textContent = {
      idle: "",
      preparing: BRAND.strings.preparing,
      unavailable: BRAND.strings.unavailable,
      failed: BRAND.strings.failed,
      playing: "",
    }[mode] + (detail ? ` (${detail})` : "");
    msg.hidden = !msg.textContent;
  }

  // Keep each card over its video. Cards for videos that are gone or have
  // no size are hidden.
  function position() {
    for (const [video, state] of guarded) {
      const r = video.isConnected ? video.getBoundingClientRect() : null;
      const visible = r && r.width > 0 && r.height > 0 &&
        r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
      state.card.hidden = !visible;
      if (visible) {
        const s = state.card.style;
        s.left = r.left + "px"; s.top = r.top + "px";
        s.width = r.width + "px"; s.height = r.height + "px";
      }
      if (!video.isConnected && state.player) {
        state.player.pause();
      }
    }
    requestAnimationFrame(position);
  }

  // ---- Source discovery ------------------------------------------------
  function httpUrl(value) {
    try {
      const u = new URL(value, location.href);
      return (u.protocol === "https:" || u.protocol === "http:") ? u.href : null;
    } catch (_) { return null; }
  }

  function findSource(video) {
    const direct = video.currentSrc || video.src ||
      (video.querySelector("source[src]") || {}).src;
    if (direct && httpUrl(direct)) return httpUrl(direct);
    // Players that use Media Source Extensions have a blob: src. Look for a
    // manifest or file URL on the elements around the video (crossing
    // shadow roots), e.g. Reddit's player element carries its HLS URL.
    let node = video;
    for (let depth = 0; node && depth < 10; depth++) {
      if (node.attributes) {
        for (const a of node.attributes) {
          if (MANIFEST.test(a.value) || FILE.test(a.value)) {
            const u = httpUrl(a.value);
            if (u) return u;
          }
        }
      }
      node = node.parentElement || (node.getRootNode && node.getRootNode().host) || null;
    }
    // Last resort: the most recent manifest the page fetched.
    return manifests.length ? manifests[manifests.length - 1] : null;
  }

  // ---- Censoring -------------------------------------------------------
  async function start(video, state) {
    const src = findSource(video);
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
      while (!job.ready) {
        if (job.state === "error") throw new Error(job.error);
        if (job.duration) state.bar.firstChild.style.width =
          Math.min(100, 100 * job.seconds_done / job.duration) + "%";
        await new Promise((r) => setTimeout(r, 1000));
        job = await (await origFetch.call(window, ORIGIN + "/video/" + job.id + "/status")).json();
      }
      play(video, state, ORIGIN + "/video/" + job.id + "/stream");
    } catch (e) {
      show(state, "failed", String(e && e.message || e).slice(0, 80));
    }
  }

  function play(video, state, url) {
    const player = document.createElement("video");
    ours.add(player);
    player.controls = true;
    player.playsInline = true;
    player.muted = video.muted;
    player.src = url;
    player.addEventListener("error", () => {
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
