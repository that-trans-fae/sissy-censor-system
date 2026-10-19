# Video censoring: design proposal

Status: approved Oct 18, 2026; being built. Plain files (MP4/WebM) and
streaming (HLS/DASH) are both in scope; videos start paused and covered
until "play censored" is clicked. The control is injected on every site
(changed from an allowlist on Oct 18, 2026, because much of the content is
embedded from other hosts); `video.hosts` can still narrow it. Every video
gets the control, including ones the proxy can't censor (see "Videos we
can't censor").

## Approach: replace the player with a censored stream

Censoring segments in flight (rewriting each HLS/DASH chunk as the site's
player fetches it) is fragile. Re-encoded segments no longer match the
stream's init segment or byte-range index, so every site's player is a new
fight. Instead, the injected control swaps the site's player for a plain
`<video>` that plays a censored stream the proxy produces. One pipeline then
covers plain files, HLS and DASH.

```
page (allowlisted host)
  └─ injected script ── finds <video>, pauses + covers it, records the
       │                source URL (src, or the HLS/DASH manifest the
       │                player fetched)
       │  click "play censored"
       ▼
  <video src="https://censor.local/video/<job>">  ◄── fragmented MP4, streamed
                                                        as it is produced
proxy
  └─ job: ffmpeg reads the source (file or manifest) ──► raw frames
          detect every Nth frame (320n), boxes held across the gap,
          blur ──► ffmpeg encodes H.264 + copies audio ──► fMP4
```

## Pieces

1. **HTML injection** on every site by default (config `video.hosts`), into
   full documents and iframes only (`Sec-Fetch-Dest`), never into HTML
   fragments a page fetches itself: add
   `<script src="https://censor.local/inject.js">` at the top of `<head>`.
   Content-Security-Policy is extended, not removed: `https://censor.local`
   is added to `script-src`, `media-src` and `connect-src` where those
   directives (or `default-src`) exist.
2. **Injected script**
   - Covers and pauses every `<video>`, including ones added later
     (MutationObserver), and re-pauses on `play` until censored.
   - Finds the source: `video.src` or `<source>` for plain files. For
     players that load through Media Source Extensions (`src` is a `blob:`
     URL), the script wraps `fetch`/`XMLHttpRequest` before the player loads
     and records `.m3u8`/`.mpd` manifest URLs, matching them to the video.
     Site-specific hints are allowed where they're cheap (Reddit's player
     element carries its HLS URL in an attribute). Raw `src` attributes are
     read, not the `.src` property, which turns an empty `src=""` into the
     page's own URL; the page URL is never taken as a source.
   - Last resort, for players that fetch their manifest only once playback
     starts (Pornhub's): manifest URLs written into the page's inline
     scripts, JSON-escaped or not. When there is one per quality, the
     tallest at or under `video.max_height` (read from names like
     `720P_4000K`) wins.
   - On click, asks the proxy to start a job, then swaps in a plain
     `<video controls>` pointing at the job's stream, and shows progress
     until playback can start.
3. **Proxy endpoints** on `https://censor.local` (mitmproxy issues its cert;
   needs `connection_strategy=lazy` so it doesn't try to reach a real host):
   `POST /video` starts a job, `GET /video/<job>` streams the output,
   `GET /video/<job>/status` reports progress.
4. **Video pipeline** (`censor_proxy/video.py`)
   - ffmpeg decodes the source (it reads HLS, DASH and plain files
     natively) at a capped resolution (default 720p) into raw frames.
   - Detection runs on a sample of frames (default 4 per second). Each box
     is applied to every frame between its sample and the next, as with
     GIFs now, plus extra padding for motion.
   - The same `decide()` rules and censor styles as images.
   - ffmpeg re-encodes H.264 (veryfast) into fragmented MP4 so playback
     starts before the job finishes; audio is copied.
   - Finished outputs are cached by source URL and page origin so replays
     and seeks are instant; the cache is size-capped. Job ids are random,
     so a page can't derive another site's job from a video URL.
   - Jobs run `video.max_concurrent_jobs` at a time; the rest queue, and
     the control says so. The script checks in every 2 s, saying whether
     the video is wanted now (being prepared or playing in a visible tab)
     or idle (paused, ended, background tab), and immediately on pause and
     tab switches.
     - Closing or leaving the page, or the site removing the video, sends a
       cancel beacon (`POST /video/<id>/cancel`): the job stops at once.
     - A running job idle for `video.idle_seconds` gives its slot to a
       queued job someone is actively waiting on. With nothing queued it
       keeps working ahead. Resuming a stopped video offers "Try again".
     - A job nobody has asked about for `video.abandon_seconds` (crashed
       tab) is cancelled. Cancelled jobs' ffmpeg processes are killed and
       their partial output deleted; each attempt writes its own file.
   - Requests look like a browser's: `User-Agent` goes after `Host` (sent
     through `-headers`, not `-user_agent`), and HLS sources are read
     without `Range: bytes=0-` (`-seekable 0 -http_seekable 0`). Pornhub's
     CDN answers 410 to either. Plain files keep Range, since an MP4 with
     its index at the end needs to seek.
5. **Fail closed**: if a job errors, the video stays covered and the
   control shows the error. Nothing plays uncensored.

## Videos we can't censor

Any video still gets the injected control, even when
the script can't find a usable source (DRM, an unknown player, a source the
proxy can't fetch). It stays covered and paused, and the control says the
video can't be censored instead of offering "play censored". Nothing plays
uncensored by default.

The control's look (name, colours, icons, strings) lives in one place: the
`BRAND` block at the top of `inject.js`, with the product name from config
(`video.brand_name`). It follows the brand in [`brand/`](brand/README.md).

## Costs and limits

- **Speed**: with 320n at 4 samples/s, about 0.5 s of work per second of
  video on this CPU, so playback can start within a few seconds and keep
  up. 640m with tiling is about 10 s per second of video, so video always
  uses 320n regardless of the image model (configurable).
- **Image size**: ffmpeg adds about 100–150 MB to the Docker image.
- **Seeking** past the part already processed waits for the job to reach
  it.
- **Tokenised URLs**: sites that sign source URLs per page load (Pornhub)
  get a new job, and no cache hit, each time the page is reloaded.
- **Cookies**: the proxy fetches the source itself, so videos that need the
  site's login cookies may fail. Reddit's are public. The script can pass
  along non-HttpOnly cookies if needed.
- **Not covered**: DRM-protected video (Widevine).
- **Security**: with every site in scope, every page's Content-Security-
  Policy is extended to allow our script, and any page can ask the proxy to
  fetch a public video URL. Private and loopback addresses stay refused,
  starts are capped and rate-limited per origin, and ffmpeg is held to a
  protocol whitelist (`docs/decisions.md`, "Page-facing surface"). The user
  will be warned about this trade-off (not built yet).

## Defaults chosen

Approved without changes to these, so the proposed defaults stand (all
configurable): output is capped at 720p and the original audio is kept.
