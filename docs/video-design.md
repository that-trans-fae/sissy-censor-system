# Video censoring: design proposal

Status: approved Oct 18, 2026; being built. Plain files (MP4/WebM) and
streaming (HLS/DASH) are both in scope; videos start paused and covered
until "play censored" is clicked; the control is injected only on
allowlisted sites. Every video on those sites gets the control, including
ones the proxy can't censor (see "Videos we can't censor").

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

1. **HTML injection** on allowlisted hosts (config `video.hosts`): add
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
     element carries its HLS URL in an attribute).
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
   - Finished outputs are cached by source URL so replays and seeks are
     instant; the cache is size-capped.
5. **Fail closed**: if a job errors, the video stays covered and the
   control shows the error. Nothing plays uncensored.

## Videos we can't censor

Any video on an allowlisted page still gets the injected control, even when
the script can't find a usable source (DRM, an unknown player, a source the
proxy can't fetch). It stays covered and paused, and the control says the
video can't be censored instead of offering "play censored". Nothing plays
uncensored by default.

The control's look (name, colours, icon) lives in one place so it can be
branded later: CSS custom properties and text strings at the top of
`inject.js`, with the product name from config.

## Costs and limits

- **Speed**: with 320n at 4 samples/s, about 0.5 s of work per second of
  video on this CPU, so playback can start within a few seconds and keep
  up. 640m with tiling is about 10 s per second of video, so video always
  uses 320n regardless of the image model (configurable).
- **Image size**: ffmpeg adds about 100–150 MB to the Docker image.
- **Seeking** past the part already processed waits for the job to reach
  it.
- **Cookies**: the proxy fetches the source itself, so videos that need the
  site's login cookies may fail. Reddit's are public. The script can pass
  along non-HttpOnly cookies if needed.
- **Not covered**: DRM-protected video (Widevine), and any site not on the
  allowlist.

## Defaults chosen

Approved without changes to these, so the proposed defaults stand (all
configurable): Reddit is the first allowlisted site, output is capped at
720p, and the original audio is kept.
