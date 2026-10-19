// Censor proxy page script. Injected at the top of <head> on allowlisted
// sites, before the page's own scripts, so it can see every <video> and <img>
// (including inside shadow roots) and the HLS/DASH manifests players fetch.
//
// Video: every video is paused and covered. "Play censored" asks the proxy
// for a censored copy and plays it in our own <video> laid over the original.
// Videos whose source can't be found stay covered. Hover previews inside
// links are covered by their poster instead, and let the pointer through.
//
// Images: an image still loading a moment after it comes into view (most
// often waiting its turn in the censor) gets a placeholder until it arrives.
(() => {
  "use strict";
  // Run once per document. The flag is a non-enumerable symbol rather than a
  // named global, and the <script> tag is removed once it has run, so page
  // scripts have nothing named to look for (the patched prototypes below are
  // still detectable; that can't be helped).
  const RAN = Symbol.for("media-guard");
  if (document[RAN]) return;
  Object.defineProperty(document, RAN, { value: true });
  if (document.currentScript) document.currentScript.remove();

  const ORIGIN = "__CENSOR_ORIGIN__";
  const DEBUG = "__DEBUG__" === "true";
  const MAX_HEIGHT = Number("__MAX_HEIGHT__") || 720;  // video.max_height
  const AD_SOURCES = JSON.parse("__AD_SOURCES__").flatMap((p) => {  // video.ad_sources
    try { return [new RegExp(p, "i")]; } catch (_) { return []; }
  });
  const VIDEO = "__VIDEO__" === "true";                  // video.enabled
  const START_MUTED = "__START_MUTED__" === "true";      // video.start_muted
  const LINK_PREVIEWS = "__LINK_PREVIEWS__" === "true";  // video.link_previews
  const PLACEHOLDERS = "__PLACEHOLDERS__" === "true";    // images.placeholders
  const PLACEHOLDER_MS = Number("__PLACEHOLDER_MS__");   // images.placeholder_delay_ms
  const PLACEHOLDER_MIN_PX = Number("__PLACEHOLDER_MIN_PX__");  // images.placeholder_min_px
  const RESERVE_PX = Number("__PLACEHOLDER_RESERVE_PX__");      // images.placeholder_reserve_px
  if (!VIDEO && !PLACEHOLDERS) return;

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
      queued: "Waiting for another video…",
      preparing: "Censoring video…",
      unavailable: "This video can't be censored",
      unavailableDetail: "It stays covered. The page didn't expose a source the proxy can fetch.",
      failed: "Couldn't censor this video",
      retry: "Try again",
      image: "Censoring",
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
      /* Always indeterminate: playback starts with the first segment, so a
         fill would never get past 0. */
      .bar > i { display: block; height: 100%; width: 35%; background: var(--censor-accent); animation: censor-slide 1.4s ease-in-out infinite; }
      @keyframes censor-slide { from { transform: translateX(-100%); } to { transform: translateX(290%); } }
      @media (prefers-reduced-motion: reduce) {
        .bar > i { width: 100%; animation: censor-pulse 2s ease-in-out infinite; }
        @keyframes censor-pulse { 0%, 100% { opacity: .35; } 50% { opacity: 1; } }
      }
      video { position: absolute; inset: 0; width: 100%; height: 100%; background: #000; }
      .card.playing { padding: 0; }
      .card.playing > :not(video) { display: none !important; }
      /* Link previews: the video's poster (an image, so censored by the
         proxy) and a small mark; the pointer passes through to the page. */
      .card > .mark { display: none; }
      .card.preview { padding: 0; pointer-events: none; background: var(--censor-bg) center / cover no-repeat; }
      .card.preview > :not(.mark) { display: none !important; }
      .card.preview > .mark {
        display: block; position: absolute; left: 8px; bottom: 8px; width: 24px; height: 24px;
        padding: 5px; box-sizing: border-box; border-radius: 6px;
        background: var(--censor-bg); color: var(--censor-accent);
      }
      .card.preview > .mark svg { width: 100%; height: auto; }
      .card.preview:not(.poster) > .mark {
        position: static; width: clamp(14px, 22cqmin, 40px); height: auto; padding: 0; background: none;
      }
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
    // The image placeholder: the mark, its tiles pulsing corner to corner,
    // over the label. Small images keep just the mark.
    imageCss: `
      :host { all: initial; }
      .ph {
        position: absolute; inset: 0; box-sizing: border-box; overflow: hidden;
        container-type: size;
        display: flex; flex-direction: column; align-items: center; justify-content: center;
        gap: 10px; background: #16121A; color: #FF6FAE;
      }
      svg { display: block; width: clamp(14px, 22cqmin, 40px); height: auto; }
      rect { animation: censor-tile 1.4s ease-in-out infinite; }
      ${MOSAIC.map((_, i) => `rect:nth-child(${i + 1}) { animation-delay: ${(i % 3 + Math.floor(i / 3)) * 0.14}s; }`).join("\n      ")}
      @keyframes censor-tile { 0%, 100% { opacity: .2; } 40% { opacity: 1; } }
      @media (prefers-reduced-motion: reduce) { rect { animation: none; } }
      span {
        font: 500 11px/1 "IBM Plex Mono", ui-monospace, "SF Mono", Menlo, monospace;
        letter-spacing: .14em; text-transform: uppercase; color: #A898A6;
      }
      @container (max-width: 120px) or (max-height: 90px) { span { display: none; } }
    `,
  };
  // ---------------------------------------------------------------------

  const MANIFEST = /\.(m3u8|mpd)(\?|#|$)/i;
  const FILE = /\.(mp4|webm|m4v|mov)(\?|#|$)/i;
  const ours = new WeakSet();       // our own players, allowed to play
  const guarded = new Map();        // page <video> -> card state
  const manifests = [];             // manifest URLs seen, newest last

  // ---- Remember manifests the page's players fetch ----------------------
  function note(url) {
    try {
      const u = new URL(url, location.href).href;
      if (MANIFEST.test(u) && !manifests.includes(u)) manifests.push(u);
    } catch (_) { /* not a URL */ }
  }
  // Some players get their manifests from an API rather than the page
  // (Redtube's /media/hls?s=... answers with a JSON list of master.m3u8
  // URLs, one per quality) and fetch the one they want only once playback
  // starts, which never happens. JSON and XML responses are read for
  // manifest URLs.
  const listed = new Set();         // manifest URLs named in JSON the page fetched
  const JSON_MAX = 2 << 20;         // skip larger bodies
  // Players fetch their preroll ads as VAST: XML with <MediaFile> elements,
  // or the same shape in JSON (Pornhub's /_xa/ads?...&preroll_type=json
  // answers with ad.mediaFiles[].uri, on ad CDNs no pattern would name).
  // Those files are ads wherever they play, so never a video's source.
  const adFiles = new Set();
  function adUrl(value) {
    value = typeof value === "string" && value.trim();
    const u = /^https?:\/\//i.test(value) && httpUrl(value);  // not types like "video/mp4"
    if (u) adFiles.add(u);
  }
  function jsonAds(value, inAd) {
    if (typeof value === "string") { if (inAd) adUrl(value); return; }
    if (!value || typeof value !== "object") return;
    for (const [k, v] of Object.entries(value)) jsonAds(v, inAd || /^media_?files?$/i.test(k));
  }
  const VAST_MEDIA = /<MediaFile\b[^>]*>\s*(?:<!\[CDATA\[)?([^<\]]+)/gi;
  function scanText(text) {
    if (typeof text !== "string" || text.length > JSON_MAX) return;
    for (const u of inlineManifests(text)) listed.add(u);
    for (const m of text.matchAll(VAST_MEDIA)) adUrl(m[1]);
    if (/media_?file/i.test(text)) {
      try { jsonAds(JSON.parse(text), false); } catch (_) { /* not JSON */ }
    }
  }
  const scannable = (type) => /\b(?:json|xml)\b/i.test(type || "");
  const origFetch = window.fetch;
  if (VIDEO && origFetch) {
    window.fetch = function (input, init) {
      note(typeof input === "string" ? input : input && input.url);
      const p = origFetch.apply(this, arguments);
      p.then((res) => {
        if (scannable(res.headers.get("Content-Type")) &&
            !(Number(res.headers.get("Content-Length")) > JSON_MAX)) {
          res.clone().text().then(scanText, () => {});
        }
      }, () => {});
      return p;
    };
  }
  const origOpen = XMLHttpRequest.prototype.open;
  const xhrs = new WeakSet();
  function xhrLoaded() {
    try {
      if (!scannable(this.getResponseHeader("Content-Type"))) return;
      if (this.responseType === "" || this.responseType === "text") scanText(this.responseText);
      else if (this.responseType === "json" && this.response) scanText(JSON.stringify(this.response));
      else if (this.responseType === "document" && this.responseXML) {
        for (const m of this.responseXML.getElementsByTagName("MediaFile")) adUrl(m.textContent);
      }
    } catch (_) { /* not readable */ }
  }
  if (VIDEO) {
    XMLHttpRequest.prototype.open = function (method, url) {
      note(url);
      if (!xhrs.has(this)) {
        xhrs.add(this);
        this.addEventListener("load", xhrLoaded);
      }
      return origOpen.apply(this, arguments);
    };
  }

  // ---- Block playback until censored ------------------------------------
  // Checked by tag name, not instanceof: videos in same-origin frames come
  // from another window's prototypes.
  const XHTML = "http://www.w3.org/1999/xhtml";
  const isVideo = (node) => node.localName === "video" && node.namespaceURI === XHTML;

  // Patch a window's play(), and pause any video there that starts anyway
  // (autoplay doesn't go through play()).
  function blockPlayback(w) {
    const proto = w.HTMLMediaElement.prototype, play = proto.play;
    proto.play = function () {
      if (isVideo(this) && !ours.has(this)) {
        guard(this);
        this.pause();
        return Promise.reject(new w.DOMException("Paused until censored", "NotAllowedError"));
      }
      return play.apply(this, arguments);
    };
    w.addEventListener("play", (e) => {
      if (isVideo(e.target) && !ours.has(e.target)) {
        e.target.pause();
        guard(e.target);
      }
    }, true);
  }
  if (VIDEO) blockPlayback(window);

  // ---- Find videos and images, including inside shadow roots ------------
  const origAttachShadow = Element.prototype.attachShadow;
  function watchShadows(w, attach) {
    w.Element.prototype.attachShadow = function () {
      const root = attach.apply(this, arguments);
      watch(root);
      return root;
    };
  }
  watchShadows(window, origAttachShadow);

  // Frames without a URL of their own (srcdoc, about:blank, written by a
  // script) are never fetched, so the proxy can't inject into them; ad
  // networks play their videos in these (Redtube's in-page ads do). The
  // script reaches into them from here instead, as soon as the iframe is
  // added and again whenever it loads a new document. A same-origin frame
  // keeps the window of its first about:blank document, so the patches made
  // on insertion are already in place while the srcdoc parses. Frames from
  // other origins, and same-origin ones with a URL, get their own injected
  // copy (or are out of reach), so they are left alone: two copies patching
  // one window would each block the other's player.
  const frames = new WeakSet();
  function frame(iframe) {
    if (!frames.has(iframe)) {
      frames.add(iframe);
      iframe.addEventListener("load", () => enterFrame(iframe));
    }
    const src = (iframe.getAttribute("src") || "").trim();
    if (!src || /^(about|javascript):/i.test(src) || iframe.hasAttribute("srcdoc")) enterFrame(iframe);
  }

  function enterFrame(iframe) {
    let w, d;
    try {
      w = iframe.contentWindow;
      d = w && w.document;
    } catch (_) { return; }  // another origin
    if (!d || !/^about:/.test(d.URL)) return;
    if (!w[RAN]) {
      Object.defineProperty(w, RAN, { value: true });
      blockPlayback(w);
      watchShadows(w, w.Element.prototype.attachShadow);
    }
    if (!d[RAN]) {
      Object.defineProperty(d, RAN, { value: true });
      watch(d);
    }
  }

  const SELECTOR = [VIDEO && "video, iframe", PLACEHOLDERS && "img"].filter(Boolean).join(", ");

  function found(node) {
    if (VIDEO && isVideo(node)) guard(node);
    else if (VIDEO && node.localName === "iframe") frame(node);
    // Placeholders stay in this document: frames here are ad slots.
    else if (PLACEHOLDERS && node instanceof HTMLImageElement) trackImage(node);
  }

  function scan(node) {
    found(node);
    if (node.querySelectorAll) node.querySelectorAll(SELECTOR).forEach(found);
  }

  function watch(root) {
    scan(root);
    new MutationObserver((records) => {
      for (const r of records) {
        if (r.type === "attributes") found(r.target);  // an image given a new source
        else r.addedNodes.forEach(scan);
      }
    }).observe(root, { childList: true, subtree: true,
                       ...(PLACEHOLDERS && { attributes: true, attributeFilter: ["src", "srcset"] }) });
  }

  // ---- Cards -------------------------------------------------------------
  // Each card lives in its video's player (see playerBox()), in the same
  // tree, so it shares whatever dialog or top layer the video is in (a card
  // in a separate page-level layer is hidden under modal dialogs). A closed
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
    const info = { ...describe(video), preview: LINK_PREVIEWS && inLink(video) };
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
    const mark = el("div", "mark");
    mark.appendChild(icon("mark"));
    card.append(mark, brand, badge, msg, detail, button, bar);
    const state = { card, host: makeHost(card), brand, badge, msg, detail, bar, button, player: null, jobId: null,
                    frames: 0, container: null, preview: false, poster: "" };
    show(state, "idle");
    guarded.set(video, state);
    startPositioning();
  }

  // A video inside a link is a preview of what the link opens (thumbnails
  // that play a clip under the pointer): a click there follows the link, so
  // no real player sits in one. Its card must not take the pointer, or the
  // page sees the pointer leave the thumbnail, drops the preview, and adds
  // it back when the card goes: a flicker (Pornhub, YouPorn).
  function inLink(video) {
    for (const node of around(video)) {
      if (node.localName === "a" && node.hasAttribute("href")) return true;
    }
    return false;
  }

  // Switch a card between the full card and the preview card, and keep the
  // preview's poster current. Only http(s) posters: those come through the
  // proxy, censored like any image; a data: or blob: one might not have.
  function previewMode(video, state) {
    const preview = LINK_PREVIEWS && !state.player && inLink(video);
    const poster = preview && httpUrl(video.getAttribute("poster")) || "";
    const card = state.card;
    if (preview !== state.preview) {
      state.preview = preview;
      card.classList.toggle("preview", preview);
      state.host.style.setProperty("pointer-events", preview ? "none" : "auto", "important");
    }
    if (poster !== state.poster) {
      state.poster = poster;
      card.classList.toggle("poster", !!poster);
      card.style.backgroundImage = poster ? `url(${JSON.stringify(poster)})` : "";
    }
    if (preview) card.style.borderRadius = getComputedStyle(video).borderRadius;
    else card.style.removeProperty("border-radius");
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

  // The outermost element around the video that is still about the
  // video's box (the player), or null. The card goes in there, so it stacks
  // above the player's own overlays (a big play button, controls; see
  // stackAbove()), which sit
  // in the same container but outside any stacking context around the video
  // itself. It never climbs out of a dialog, popover or fullscreen element,
  // so it stays in their layer.
  const SAME_BOX = 1.15;
  function playerBox(video, r) {
    let box = null;
    for (let node = video.parentElement; node && node !== node.ownerDocument.body &&
         node !== node.ownerDocument.documentElement; node = node.parentElement) {
      const b = node.getBoundingClientRect();
      if (b.left > r.left + 1 || b.top > r.top + 1 || b.right < r.right - 1 || b.bottom < r.bottom - 1 ||
          b.width > r.width * SAME_BOX || b.height > r.height * SAME_BOX) break;
      box = node;
      if (node.localName === "dialog" || node.matches(":popover-open, :fullscreen")) break;
    }
    return box;
  }

  // A z-index just above everything in the player. Not the maximum: unless
  // the player is a stacking context of its own, the card stacks with the
  // rest of the page, and the page's modals (an age check over the player)
  // must stay on top of it.
  function stackAbove(box) {
    let z = 0;
    for (const e of box.querySelectorAll("*")) {
      if (e.localName === "censor-proxy-card") continue;
      const v = parseInt(getComputedStyle(e).zIndex, 10);
      if (v > z) z = v;
    }
    return Math.min(z + 1, 2147483647);
  }

  // Keep each card over its video. Cards for videos that are gone or
  // can't be placed are hidden.
  function position() {
    for (const [video, state] of guarded) {
      const host = state.host;
      // A frame that loaded a new document leaves the old one's videos
      // connected to a document with no window.
      const view = video.isConnected && video.ownerDocument.defaultView;
      const r = view ? coverRect(video) : null;
      const visible = r && r.bottom > 0 && r.right > 0 && r.top < view.innerHeight && r.left < view.innerWidth;
      if (!view) {
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
      // Where the card goes is looked up again now and then: players wrap
      // and rebuild their markup while they load.
      const s = host.style;
      if (++state.frames % 30 === 1) {
        previewMode(video, state);
        state.container = playerBox(video, r);
        const z = state.container ? String(stackAbove(state.container)) : "2147483647";
        if (z !== state.z) s.setProperty("z-index", state.z = z, "important");
      }
      const box = state.container;
      if (box && box.isConnected) {
        if (host.parentNode !== box) box.appendChild(host);
      } else if (host.previousSibling !== video) video.after(host);
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

  // ---- Image placeholders ----------------------------------------------
  // The proxy holds each image until it is censored, and on image-heavy
  // pages they queue for the workers, leaving blank boxes. An image still
  // loading PLACEHOLDER_MS after it came into view gets a card until it
  // loads or fails. Unlike video cards these stack with the image (its own
  // z-index, so a sticky header still covers them), are clipped by whatever
  // scrolling or clipping containers clip the image, and let clicks through
  // to the image and any link around it.
  const loading = new Map();        // <img> still loading -> placeholder state
  const inView = new Set();         // the ones near the viewport
  const listening = new WeakSet();
  const IMAGE_HOST_CSS = "position:absolute;display:none;margin:0;padding:0;" +
    "border:0;pointer-events:none;contain:strict;overflow:hidden;right:auto;bottom:auto;" +
    "min-width:0;min-height:0;max-width:none;max-height:none;transform:none;float:none;" +
    "opacity:1;visibility:visible;";

  const viewport = PLACEHOLDERS && new IntersectionObserver((entries) => {
    for (const e of entries) {
      const state = loading.get(e.target);
      if (!state) continue;
      if (e.isIntersecting) {
        inView.add(e.target);
        state.since = performance.now();  // lazy images only start loading now
      } else {
        inView.delete(e.target);
        hidePlaceholder(state);
      }
    }
    startPlacing();
  }, { rootMargin: "100px" });

  function trackImage(img) {
    if (!listening.has(img)) {
      listening.add(img);
      img.addEventListener("load", () => untrackImage(img));
      img.addEventListener("error", () => untrackImage(img));
    }
    if (img.complete) return untrackImage(img);
    const state = loading.get(img);
    if (state) state.since = performance.now();  // a new source starts the wait again
    else loading.set(img, { since: performance.now(), host: null, shown: false, left: 0, top: 0 });
    viewport.observe(img);
  }

  function untrackImage(img) {
    const state = loading.get(img);
    if (!state) return;
    if (state.host) state.host.remove();
    if (state.reserved) {
      for (const [prop, value, priority] of state.reserved) {
        if (value) img.style.setProperty(prop, value, priority);
        else img.style.removeProperty(prop);
      }
      if (!state.hadStyle && !img.getAttribute("style")) img.removeAttribute("style");
    }
    loading.delete(img);
    inView.delete(img);
    viewport.unobserve(img);
  }

  function hidePlaceholder(state) {
    if (!state.shown) return;
    state.shown = false;
    state.host.style.setProperty("display", "none", "important");
  }

  function makeImageHost() {
    const host = document.createElement("censor-proxy-placeholder");
    host.style.cssText = IMAGE_HOST_CSS.replace(/;/g, " !important;");
    host.setAttribute("aria-hidden", "true");
    const root = origAttachShadow.call(host, { mode: "closed" });
    const style = document.createElement("style");
    style.textContent = BRAND.imageCss;
    const card = el("div", "ph");
    card.append(icon("mark"), el("span", "", BRAND.strings.image));
    root.append(style, card);
    return host;
  }

  let placing = false;
  function startPlacing() {
    if (!placing && inView.size) {
      placing = true;
      requestAnimationFrame(placeImages);
    }
  }

  // Ancestors that clip the image, as [element, clips x, clips y]. An
  // absolutely positioned card escapes any that aren't also its containing
  // block, so it is clipped to them by hand. <html> and <body> hand their
  // overflow to the viewport and are left out.
  function clippers(img) {
    const list = [];
    let node = img.parentElement || img.getRootNode().host;
    while (node && node !== document.body && node !== document.documentElement) {
      const cs = getComputedStyle(node);
      const x = cs.overflowX !== "visible", y = cs.overflowY !== "visible";
      if (x || y) list.push([node, x, y]);
      node = node.parentElement || (node.getRootNode && node.getRootNode().host) || null;
    }
    return list;
  }

  // The card's clip-path: the image's box cut down to the inside of each
  // clipping ancestor. null when nothing of it is visible.
  function clipPath(r, clips) {
    let x0 = r.left, y0 = r.top, x1 = r.right, y1 = r.bottom;
    for (const [node, cx, cy] of clips) {
      const b = node.getBoundingClientRect();
      const left = b.left + node.clientLeft, top = b.top + node.clientTop;
      if (cx) { x0 = Math.max(x0, left); x1 = Math.min(x1, left + node.clientWidth); }
      if (cy) { y0 = Math.max(y0, top); y1 = Math.min(y1, top + node.clientHeight); }
    }
    if (x1 <= x0 || y1 <= y0) return null;
    const px = (v) => Math.max(0, Math.round(v)) + "px";
    return `inset(${px(y0 - r.top)} ${px(r.right - x1)} ${px(r.bottom - y1)} ${px(x0 - r.left)})`;
  }

  // An image with no size set takes no room until it loads (pasted GIFs on
  // forums, comment threads). If it sits alone in a sized box, such as a
  // thumbnail frame, the card covers that box.
  function wrapperRect(img) {
    let node = img;
    for (let depth = 0; depth < 3; depth++) {
      const parent = node.parentElement;
      if (!parent || parent === document.body || parent.textContent.trim() ||
          [...parent.children].some((c) => c !== node && c.localName !== "source" &&
                                           c.localName !== "censor-proxy-placeholder")) return null;
      const r = parent.getBoundingClientRect();
      if (r.width >= PLACEHOLDER_MIN_PX && r.height >= PLACEHOLDER_MIN_PX) return r;
      node = parent;
    }
    return null;
  }

  // Otherwise the image is held open at RESERVE_PX on each axis the page
  // left unsized, until it loads. Images sized by attribute or inline style
  // (tracking pixels, deliberately hidden images) are left alone.
  function reservable(img, r) {
    if (!RESERVE_PX || img.naturalWidth || getComputedStyle(img).visibility !== "visible") return null;
    const w = r.width === 0 && !img.hasAttribute("width") && !img.style.width;
    const h = r.height === 0 && !img.hasAttribute("height") && !img.style.height;
    return (w || h) && (w || r.width >= PLACEHOLDER_MIN_PX) && (h || r.height >= PLACEHOLDER_MIN_PX)
      ? { w, h } : null;
  }

  function reserve(img, state, axes) {
    const s = img.style;
    state.hadStyle = img.hasAttribute("style");
    state.reserved =["min-width", "min-height"].map((p) =>
      [p, s.getPropertyValue(p), s.getPropertyPriority(p)]);
    // Width is capped at the container's; a percentage height would resolve
    // to nothing in an auto-height container.
    if (axes.w) s.setProperty("min-width", `min(${RESERVE_PX}px, 100%)`, "important");
    if (axes.h) s.setProperty("min-height", RESERVE_PX + "px", "important");
  }

  // Every frame: measure everything first, then move, so a page full of
  // cards costs one layout. A card is moved by however far it sits from its
  // image, which works whatever its containing block is.
  function placeImages() {
    const now = performance.now();
    const moves = [];
    const reserves = [];
    for (const img of inView) {
      const state = loading.get(img);
      if (!img.isConnected || img.complete) {
        untrackImage(img);
        continue;
      }
      if (now - state.since < PLACEHOLDER_MS) continue;
      let r = img.getBoundingClientRect();
      if (!r.width || !r.height) {
        const box = wrapperRect(img);
        const axes = !box && !state.reserved && reservable(img, r);
        if (box) r = box;
        else if (axes) reserves.push([img, state, axes]);  // a card next frame, once it has room
      }
      const attached = state.shown && state.host.previousSibling === img;
      if (!attached) state.clips = clippers(img);  // looked up again whenever it's (re)placed
      const clip = clipPath(r, state.clips);
      if (r.width < PLACEHOLDER_MIN_PX || r.height < PLACEHOLDER_MIN_PX || !clip) {
        hidePlaceholder(state);
        continue;
      }
      moves.push([img, state, r, clip, attached ? state.host.getBoundingClientRect() : null]);
    }
    for (const [img, state, axes] of reserves) reserve(img, state, axes);
    for (const [img, state, r, clip, h] of moves) {
      const host = state.host = state.host || makeImageHost();
      const s = host.style;
      if (clip !== state.clip) s.setProperty("clip-path", state.clip = clip, "important");
      if (!h) {
        // New, or the page moved things around: place it at 0,0 and measure
        // once to find the offset.
        if (host.previousSibling !== img) img.after(host);
        const cs = getComputedStyle(img);
        s.setProperty("border-radius", cs.borderRadius, "important");
        s.setProperty("z-index", cs.zIndex, "important");
        s.setProperty("display", "block", "important");
        s.setProperty("left", "0px", "important");
        s.setProperty("top", "0px", "important");
        const origin = host.getBoundingClientRect();
        state.left = r.left - origin.left;
        state.top = r.top - origin.top;
        state.shown = true;
      } else if (Math.abs(h.left - r.left) > 0.5 || Math.abs(h.top - r.top) > 0.5) {
        state.left += r.left - h.left;
        state.top += r.top - h.top;
      } else if (Math.abs(h.width - r.width) <= 0.5 && Math.abs(h.height - r.height) <= 0.5) {
        continue;  // already in place
      }
      s.setProperty("left", state.left + "px", "important");
      s.setProperty("top", state.top + "px", "important");
      s.setProperty("width", r.width + "px", "important");
      s.setProperty("height", r.height + "px", "important");
    }
    if (inView.size) requestAnimationFrame(placeImages);
    else placing = false;
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

  // Manifest URLs written out in text (inline scripts, JSON), often
  // JSON-escaped.
  const INLINE_MANIFEST = /https?:\\?\/\\?\/[^"'\s<>]+?\.(?:m3u8|mpd)(?:\?[^"'\s<>]*)?(?=["'\s<>]|$)/gi;
  function inlineManifests(text) {
    const found = [];
    for (const m of text.matchAll(INLINE_MANIFEST)) {
      const u = httpUrl(m[0].replace(/\\\//g, "/").replace(/\\u0026/gi, "&"));
      if (u) found.push(u);
    }
    return found;
  }

  // Of manifests listed once per quality, the tallest rendition the proxy
  // won't scale down (heights read from names like 720P_4000K), else the
  // smallest.
  function bestRendition(found) {
    const height = (u) => +((/(?:^|[^0-9])(\d{3,4})p(?![a-z])/i.exec(new URL(u).pathname) || [])[1] || 0);
    const urls = [...found].sort((a, b) => height(a) - height(b));
    const fits = urls.filter((u) => height(u) <= MAX_HEIGHT);
    return fits.length ? fits[fits.length - 1] : urls[0] || null;
  }

  // Players that fetch their manifest only once playback starts (Pornhub's)
  // never show it to note(); the page's inline config usually lists it.
  function pageManifest() {
    const found = new Set();
    for (const s of document.querySelectorAll("script:not([src])")) {
      for (const u of inlineManifests(s.textContent)) found.add(u);
    }
    return bestRendition(found);
  }

  // An ad playing in the page's own player isn't the video (video.ad_sources,
  // and media files named in VAST ads the page fetched).
  const notAd = (u) => u && !adFiles.has(u) && !AD_SOURCES.some((re) => re.test(u)) ? u : null;
  const lastNotAd = (urls) => urls.filter(notAd).pop() || null;

  function findSource(video) {
    // Raw attributes: the .src properties turn src="" into the page URL.
    const source = video.querySelector("source[src]");
    const direct = notAd(httpUrl(video.currentSrc)) || notAd(httpUrl(video.getAttribute("src"))) ||
      notAd(httpUrl(source && source.getAttribute("src")));
    return notAd(attributeUrl(video, MANIFEST)) ||   // a player's manifest beats any single file
      siteHint(direct) ||
      direct ||
      notAd(attributeUrl(video, FILE)) ||
      lastNotAd(manifests) ||
      bestRendition([...listed].filter(notAd)) ||
      pageManifest();
  }

  // ---- Censoring -------------------------------------------------------
  async function start(video, state) {
    const src = findSource(video);
    report("start", { ...describe(video), found: src, manifests: manifests.slice(-3), listed: listed.size });
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
        await new Promise((r) => setTimeout(r, 1000));
        job = await (await origFetch.call(window, statusUrl(state))).json();
      }
      play(video, state, job);
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

  function play(video, state, job) {
    const player = document.createElement("video");
    let stopped = false;
    const stop = (why) => {
      if (stopped) return;
      stopped = true;
      player.pause();
      player.remove();
      state.player = null;
      show(state, "failed", why);
    };
    const timer = setInterval(() => {
      if (!player.isConnected) return clearInterval(timer);
      beat(state);
    }, HEARTBEAT_MS);
    player.addEventListener("pause", () => beat(state));
    // Resuming after the proxy stopped the job (paused while another video
    // waited) would just run out where the job stopped; say so instead.
    player.addEventListener("play", async () => {
      const now = await beat(state);
      if (now && now.state === "error") stop(now.error.replace(/^\w+: /, ""));
    });
    ours.add(player);
    player.controls = true;
    player.playsInline = true;
    player.muted = START_MUTED || video.muted;
    player.addEventListener("error", () => {
      report("player-error", { code: player.error && player.error.code, id: job.id });
      stop();
    });
    if (!stream(player, job, stop)) return stop("This browser can't play the censored copy.");
    state.card.appendChild(player);
    state.player = player;
    show(state, "playing");
    player.play().catch(() => { /* autoplay refused; controls let the user start it */ });
  }

  // ---- Feeding the player ------------------------------------------------
  // The proxy cuts the censored copy into segments (video.segment_seconds)
  // and the player plays them through Media Source Extensions, fetched a
  // little ahead of the playhead. Seeking past what is buffered fetches
  // from there; the proxy restarts the job at that point unless it is
  // about to get there anyway, and the player waits meanwhile.
  const AHEAD_S = 30;   // buffer this far ahead of the playhead
  const BEHIND_S = 30;  // and let go of what is further behind
  const GAP_S = 0.5;    // step over gaps this small (where two runs of the job meet)

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function stream(player, job, stop) {
    const type = 'video/mp4; codecs="' + job.codecs + '"';
    if (!window.MediaSource || !job.codecs || !MediaSource.isTypeSupported(type)) return false;
    const base = ORIGIN + "/video/" + job.id;
    const seg = job.segment_seconds;
    const ms = new MediaSource();
    let sb = null;
    let init = null;        // the run whose init segment the buffer has
    let next = 0;           // the segment to fetch next
    let end = job.segments; // segment count, once known
    let gen = 0;            // bumped by seeks; work for an older one is dropped
    let ctrl = null;        // aborts the fetch in flight

    // Run one SourceBuffer operation and wait for it to finish.
    const update = (op) => new Promise((resolve, reject) => {
      const done = (e) => {
        sb.removeEventListener("updateend", done);
        sb.removeEventListener("error", done);
        e.type === "error" ? reject(new Error("the player refused a segment")) : resolve();
      };
      sb.addEventListener("updateend", done);
      sb.addEventListener("error", done);
      try { op(); } catch (e) {
        sb.removeEventListener("updateend", done);
        sb.removeEventListener("error", done);
        reject(e);
      }
    });

    // Where the buffered stretch around t ends (small gaps don't count), or
    // null when t isn't buffered.
    function bufferedEnd(t) {
      const b = sb.buffered;
      let e = null;
      for (let i = 0; i < b.length; i++) {
        if (e === null ? b.start(i) - GAP_S <= t && t < b.end(i) : b.start(i) - e <= GAP_S) e = b.end(i);
        else if (e !== null) break;
      }
      return e;
    }

    function buffered(n) {
      const lo = n * seg, hi = Math.min((n + 1) * seg, ms.duration || Infinity);
      const b = sb.buffered;
      for (let i = 0; i < b.length; i++) {
        if (b.start(i) <= lo + 0.25 && b.end(i) >= hi - 0.25) return true;
      }
      return false;
    }

    function onSeek() {
      const t = player.currentTime;
      const e = bufferedEnd(t);
      const n = e === null ? Math.floor(t / seg) : Math.round(e / seg);
      if (n === next) return;
      next = n;
      gen++;
      if (ctrl) ctrl.abort();
    }

    // Stalled just before the next buffered stretch: step over the gap.
    function jumpGap() {
      if (!sb || player.seeking || player.paused || player.readyState >= 3) return;
      const t = player.currentTime, b = sb.buffered;
      for (let i = 0; i < b.length; i++) {
        if (b.start(i) <= t && t < b.end(i) - 0.1) return;
        if (b.start(i) > t && b.start(i) - t <= GAP_S) {
          player.currentTime = b.start(i) + 0.01;
          return;
        }
      }
    }

    async function append(data) {
      for (;;) {
        try {
          return await update(() => sb.appendBuffer(data));
        } catch (e) {
          if (e.name !== "QuotaExceededError") throw e;
          // Full: let go of more of what has been played, then wait for the playhead.
          const t = player.currentTime;
          if (t > 5 && sb.buffered.length && sb.buffered.start(0) < t - 5) {
            await update(() => sb.remove(0, t - 5));
          }
          await sleep(1000);
        }
      }
    }

    async function fetchSegment(n, g) {
      ctrl = new AbortController();
      const res = await origFetch.call(window, base + "/segment/" + n, { signal: ctrl.signal });
      if (res.status === 202) return null;  // not done yet; ask again
      if (res.status === 204) {             // past the end
        end = n;
        return null;
      }
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || "HTTP " + res.status);
      const run = res.headers.get("X-Init"), offset = Number(res.headers.get("X-Offset"));
      const data = await res.arrayBuffer();
      if (g !== gen) return null;
      if (run !== init) {
        const r = await origFetch.call(window, base + "/init/" + run, { signal: ctrl.signal });
        if (!r.ok) throw new Error("HTTP " + r.status);
        const bytes = await r.arrayBuffer();
        if (g !== gen) return null;
        await update(() => sb.appendBuffer(bytes));
        init = run;
      }
      return { data, offset };
    }

    async function loop() {
      let failures = 0;
      while (player.isConnected) {
        jumpGap();
        const t = player.currentTime;
        if (end !== null && next >= end) {
          if (ms.readyState === "open" && !sb.updating) ms.endOfStream();
          await sleep(250);
          continue;
        }
        if (buffered(next)) {
          next++;
          continue;
        }
        if (next * seg > t + AHEAD_S) {
          await sleep(250);
          continue;
        }
        const n = next, g = gen;
        try {
          const got = await fetchSegment(n, g);
          failures = 0;
          if (!got) continue;
          const b = sb.buffered;
          if (b.length && b.start(0) < t - BEHIND_S - seg) await update(() => sb.remove(0, t - BEHIND_S));
          sb.timestampOffset = got.offset;  // each run's timestamps start at 0
          await append(got.data);
          if (g === gen) next = n + 1;
        } catch (e) {
          if (e.name === "AbortError") continue;  // a seek; start over from there
          if (e instanceof TypeError && ++failures < 5) {  // network trouble
            await sleep(1000);
            continue;
          }
          throw e;
        }
      }
    }

    ms.addEventListener("sourceopen", () => {
      URL.revokeObjectURL(player.src);
      sb = ms.addSourceBuffer(type);
      if (job.duration) ms.duration = job.duration;
      player.addEventListener("seeking", onSeek);
      player.addEventListener("waiting", jumpGap);
      loop().catch((e) => {
        report("stream-error", { error: String(e && e.message || e), id: job.id });
        stop(String(e && e.message || e).slice(0, 80));
      });
    }, { once: true });
    player.src = URL.createObjectURL(ms);
    return true;
  }

  watch(document);
})();
