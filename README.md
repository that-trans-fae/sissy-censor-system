<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/brand/banner-dark.svg">
  <img src="docs/brand/banner-light.svg" width="100%" alt="Sissy Censor System: a local proxy that blurs explicit regions before your browser renders them">
</picture>

# Sissy Censor System

Sissy Censor System (SCS) is a small program that sits between a web
browser and the internet and blurs female explicit regions in pictures and
videos before the browser shows them. Male regions are left alone. Nothing
is blocked and nothing is swapped for a placeholder: each image is blurred
where it needs to be and passed on otherwise unchanged, so pages keep their
layout and the rest of every picture stays intact.

It runs entirely on your own computer, in one Docker container. No image,
video or address you visit is sent anywhere for analysis. There is no
account, no cloud service and no telemetry. The only thing the install ever
fetches on its own is a newer version of itself from Docker Hub.

**Contents**

- [What it does](#what-it-does)
- [How it works](#how-it-works)
- [Installing the Sissy Browser](#installing-the-sissy-browser)
- [What gets installed](#what-gets-installed)
- [Using it day to day](#using-it-day-to-day)
- [Security: what a proxy like this can see](#security-what-a-proxy-like-this-can-see)
- [Why you should not make it your system-wide proxy yet](#why-you-should-not-make-it-your-system-wide-proxy-yet)
- [Running it from source](#running-it-from-source)
- [Project status](#project-status)
- [Documentation and layout](#documentation-and-layout)

## What it does

- **Still images**: JPEG, PNG, WebP and AVIF are decoded, scanned and
  blurred in place. Animated GIFs and WebPs are handled too: frames are
  sampled, and the blur is carried across the frames in between.
- **Images hidden inside pages**: pictures embedded directly in HTML, CSS,
  JavaScript and JSON (base64 "data URIs") go through the same scan, as do
  images served with a wrong or missing content type.
- **Video**: every video on a page is paused and covered by a small
  control. Pressing "Play censored" has the proxy download the video,
  blur it frame by frame with ffmpeg and stream the censored copy back
  while it works. You can seek anywhere; the proxy picks up from the new
  spot.
- **Loading placeholders**: an image that is still being scanned when it
  scrolls into view shows a "Censoring" card until it is ready, so you are
  not looking at an empty gap wondering if the page broke.

Detection errs on the side of caution. An unnecessary blur is preferred to
a miss, and if detection fails or times out the whole image is blurred.
The blur style (Gaussian, pixelate or solid fill), what counts as explicit
and almost every other behaviour can be changed from the admin panel.

## How it works

The container runs [mitmproxy](https://mitmproxy.org/), a general-purpose
intercepting proxy, with a custom add-on that does the censoring. In plain
terms:

1. **The browser sends every request through the proxy.** The dedicated
   browser described below is configured to use `127.0.0.1:8080` for all
   traffic.
2. **The proxy opens the encrypted connection.** HTTPS pages are encrypted
   between your browser and the website, so to see the pictures the proxy
   has to stand in the middle. It does this with its own certificate
   authority (CA), created on first start and stored in `~/scs/mitmproxy`.
   The browser is told to trust that CA, so pages still show the padlock.
   Sites on the [whitelist](#security-what-a-proxy-like-this-can-see) are
   not opened at all and pass straight through.
3. **Responses are sorted by content type.** Anything that is not an image,
   a video, or a text body that might contain an embedded image is streamed
   through untouched. Images are buffered until they have been checked.
4. **Each image is run through a nudity detector.** The detector is
   [NudeNet](https://github.com/notAI-tech/NudeNet), an open-source model
   that draws labelled boxes around body parts. Two versions ship in the
   image: a fast one (320n, about a tenth of a second per image on a CPU)
   and a more accurate one (640m, about six tenths of a second). Tall and
   wide images are scanned in tiles so small regions are not missed.
5. **One rule decides what to blur.** Female labels are blurred above
   context-dependent confidence thresholds. Labels that are not
   sex-specific, such as buttocks, are blurred unless they sit near male
   context only. The reasoning behind each threshold, and the sample
   images that motivated it, is in [`docs/decisions.md`](docs/decisions.md).
6. **The chosen boxes are padded, blurred and re-encoded** in the image's
   original format, and the result goes to the browser. Identical images
   are remembered by content hash so they are not scanned twice.
7. **Video uses the same detector on sampled frames.** The proxy injects
   one small script into each page. It covers every `<video>`, and when
   you press play it asks the proxy to fetch that video, blur it and serve
   it back in short segments that the page plays as they are produced.
   [`docs/video-design.md`](docs/video-design.md) has the details.

Every scan is written to a local log (`~/scs/logs/detections.jsonl`) with
the hostname, the labels found and their scores. No images are kept.

An **admin panel** at <http://localhost:8082/admin> shows what has been
censored, lets you switch models and censoring levels, manage the
whitelist, watch video jobs, turn automatic updates on or off, and lock
all of that behind a PIN.

## Installing the Sissy Browser

The supported way to use SCS is a **dedicated browser**: a separate copy
of Chromium, with its own profile and its own launcher, that always goes
through the proxy. Your normal browsers, your operating system's network
settings and your system certificate store are not touched. You can keep
using your usual browser for everything else, and the two never mix.

One command does the whole install.

Linux (Ubuntu, Debian, Fedora or Arch) and macOS, in a terminal as your
normal user:

```bash
curl -fsSL https://raw.githubusercontent.com/that-trans-fae/sissy-censor-system/main/install/browser.sh | bash
```

Windows 10 or 11, in a normal PowerShell window:

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/that-trans-fae/sissy-censor-system/main/install/browser.ps1 | iex"
```

The script asks for your password (sudo) or an elevation prompt (winget)
only for the steps that need it, such as installing Docker. The first run
downloads a little over a gigabyte: the proxy image contains Python,
ffmpeg and both detection models. Later updates fetch only what changed.

When it finishes you have a **Sissy Browser** entry in your app launcher,
Start menu or `~/Applications`, with its own icon. Open it and browse. The
admin panel is already on the bookmark bar.

A few platform notes:

- **Linux** installs Docker Engine from Docker's own package repository
  (Arch uses the distribution's package) and adds you to the `docker`
  group. Until you log out and back in, the script and the launcher use
  sudo to talk to Docker.
- **macOS and Windows** install Docker Desktop. It shows a service
  agreement on first start; the installer waits up to fifteen minutes for
  you to accept it and for the engine to come up.
- **Windows** needs WSL 2 for Docker Desktop. If it is not enabled, the
  installer prints the command to run.
- Rerunning the installer is safe. It picks up the current release and
  reapplies the settings. On Windows, rerun it if the proxy ever makes a
  new CA, since the certificate's fingerprint is baked into the shortcut.

To remove the browser, its profile and the update job (Docker, Chromium and
the `~/scs` folder are kept):

```bash
curl -fsSL https://raw.githubusercontent.com/that-trans-fae/sissy-censor-system/main/install/browser.sh | SCS_UNINSTALL=1 bash
```

```powershell
powershell -ExecutionPolicy Bypass -c "$env:SCS_UNINSTALL='1'; irm https://raw.githubusercontent.com/that-trans-fae/sissy-censor-system/main/install/browser.ps1 | iex"
```

[`install/README.md`](install/README.md) covers the environment variables
the scripts read (image repository, release channel, install folder) and
more first-run detail.

## What gets installed

The installer puts five things on your machine. Nothing is added to the
system proxy settings, the system certificate store or any browser you
already had.

**1. Docker.** Docker Engine on Linux, Docker Desktop on macOS and Windows.
If you already have Docker, it is reused. On Linux the Engine is set to
start with the machine.

**2. The proxy, in `~/scs`** (`%USERPROFILE%\scs` on Windows). This is the
only folder SCS owns, and everything the container can write lives here:

| Path | What it is |
| --- | --- |
| `compose.yaml` | How the container is run. Comes out of the image and is replaced at each update, so don't edit it. |
| `.env` | Which release is running (pinned by digest), which channel to follow, your user id, and optionally different ports. |
| `config/config.yaml` | Your settings. The admin panel writes this file; you can also edit it by hand. |
| `mitmproxy/` | The proxy's certificate authority. Created on first start, kept across updates. **This folder contains a private key.** |
| `logs/` | The detection log, the admin PIN attempt log, and `update.json` with the last update result. |
| `scs-update` / `scs-update.ps1` | The updater script, from the image. |
| `update.log` | A line for every update run. |

The container itself runs locked down: as your own user rather than root,
with a read-only filesystem apart from the three folders above, with every
Linux capability dropped, and with both of its ports bound to `127.0.0.1`
so nothing else on your network can reach it. It has no access to the
Docker socket and never contacts Docker Hub itself.

**3. An update check at startup.** A systemd user timer on Linux, a
launchd agent on macOS, a scheduled task on Windows. Once each time you log
in after the computer starts, it runs `scs-update`, which asks Docker Hub
once whether there is a newer release. If there is, it waits until the
Sissy Browser is closed, switches to the new image, waits for its health
check to pass, and rolls back to the old one if it does not. The result shows on the admin panel's Overview tab. You can turn
automatic updates off there, or pin a version by setting the channel in
`.env` to a tag such as `thattransfae/scs:1.2.0`.

**4. Chromium.** The snap or distribution package on Linux, the Homebrew
cask on macOS, the winget package on Windows (plus the sqlite3 tool, which
the Windows installer uses to write the certificate store). If Chromium is
already installed it is reused.

**5. The Sissy Browser launcher and profile.** A launcher that starts
Chromium with a separate profile and `--proxy-server=127.0.0.1:8080`, and
trusts the proxy's CA inside that profile's own certificate store (the
"Installed by you" list in `chrome://certificate-manager`). On Linux and
macOS the launcher also starts the proxy container if it is not running.

| Platform | Launcher | Profile |
| --- | --- | --- |
| Linux | `~/.local/bin/sissy-browser`, a `.desktop` entry and icons under `~/.local/share` | `~/.config/sissy-browser` (snap Chromium: `~/snap/chromium/common/sissy-browser`) |
| macOS | `~/Applications/Sissy Browser.app` | `~/Library/Application Support/sissy-browser` |
| Windows | Start menu and desktop shortcuts | `%LOCALAPPDATA%\SissyBrowser` |

## Using it day to day

- Browse in the Sissy Browser as normal. Images arrive a moment later than
  usual while they are scanned; animated GIFs are the slowest.
- Videos show a cover with a "Play censored" button. Playback starts after
  a few seconds of video are ready. Censored videos start muted; there is a
  switch for that on the panel.
- Open the **admin panel** from the bookmark bar (or
  <http://localhost:8082/admin>, or <https://scs.local/admin> from inside
  the Sissy Browser). The Settings tab has three plain switches: the image
  model (fast or accurate), how much CPU to use, and the censoring level
  (Full, Cocks only, Only Women, Permissive, or your own preset). Custom
  mode exposes every option with its documentation.
- If a site you need breaks, or is one the proxy should never look inside
  (your bank, a health portal, work sign-in), add it on the **Whitelist**
  tab. Whitelisted sites are not intercepted at all.
- If you forget whether something was blurred, the **Detections** tab lists
  recent scans by host, label and score. No images are shown.
- Set a **PIN** on the panel if you don't want settings changed casually.
  Wrong attempts are logged and shown after the next unlock, and a wrong
  PIN never locks anyone out.

## Security: what a proxy like this can see

Be clear-eyed about what you are installing. To blur images on HTTPS
pages, the proxy must decrypt those pages. That means:

- **Inside the Sissy Browser, the proxy sees everything.** Every page,
  every form you submit, every cookie, every password you type into a site
  that is not whitelisted. It does not record any of this (the detection
  log keeps hostnames, labels and scores, and at the `debug` log level full
  URLs), but it could. You are trusting this code, and this container, with
  all of that browser's traffic.
- **The CA private key in `~/scs/mitmproxy` is the key to that trust.**
  Anyone who gets a copy of it, and can get between your browser and the
  network, can impersonate any website to the Sissy Browser without a
  warning. Never share that folder, never commit it, and if you suspect it
  has leaked, delete it: the proxy makes a new CA on next start and the
  launcher trusts it automatically (on Windows, rerun the installer).
- **The whitelist exists to keep sensitive sites out of the proxy's reach.**
  The built-in list is not intercepted at all: browser and operating system
  updates, Docker, sign-in providers, password managers, payment
  processors, banks, brokerages and credit bureaus, health portals,
  insurers and pharmacies, major streaming services and YouTube, and every
  `.gov`, `.mil`, `.edu`, `gov.uk` and `edu.au` site. Add your own on the
  panel. The trade-off is that nothing those sites serve gets censored.
- **The proxy injects a script into pages for video.** It also loosens
  each page's Content Security Policy enough to allow that one script, and
  lets pages ask the proxy to fetch public video URLs on their behalf. Job
  requests are only accepted from pages on `video.hosts` (every site by
  default), only for public addresses, and are capped per origin. Narrow
  `video.hosts` to the sites you actually watch video on if that bothers
  you.
- **The container is a target.** A compromise of the proxy is a compromise
  of every page in the browser behind it, which is why it runs unprivileged,
  read-only, without capabilities, on loopback only, and without access to
  Docker.
- **The admin panel changes all of this.** Any page you visit can try to
  send requests to `localhost:8082`. The panel refuses requests with the
  wrong `Host` header, refuses writes that do not come from its own origin
  as JSON, and sends no CORS headers. The PIN adds a second layer.

The dedicated-browser design keeps all of these risks inside one browser
profile you use on purpose. Your other browsers, your mail client, your
operating system's updater and every other program on the machine keep
their own direct, end-to-end encrypted connections.

## Why you should not make it your system-wide proxy yet

It is technically possible to point the whole operating system at the
proxy and trust its CA in the system certificate store (the commands are
in [`src/README.md`](src/README.md)). **Don't do that at this stage.**

- **Everything on the machine would go through it.** Not only your
  browser: the OS updater, your password manager's sync, mail, chat apps,
  games, anything that uses the system proxy settings. Every one of those
  would be decrypted by a project that is still being tuned. The whitelist
  mitigates this for the hosts it knows, but it cannot know about every
  program you run.
- **Many programs will simply break.** Anything that pins its own
  certificates (most mobile-style apps, some updaters, some chat clients)
  refuses a connection signed by an unknown CA and fails, sometimes
  silently.
- **The fallback is untested.** When the container is stopped or mid-update
  (it goes away for a few seconds at each update), a fixed system proxy
  leaves you with no internet at all. The project's plan for this is a PAC
  file that falls back to a direct connection, and that plan has not been
  tried: it is listed under "not done yet" in
  [`docs/status.md`](docs/status.md). Chromium may not even accept the
  file-based PAC URL from system settings.
- **It widens the blast radius of every item in the section above.** A
  leaked CA key, a bug in the proxy or a compromise of the container would
  then affect every program instead of one browser profile.
- **Firefox is not supported at all** yet, and the system-wide path has
  only ever been exercised with Chromium.

If you want more of your browsing covered, use the Sissy Browser for more
of your browsing. A system-wide mode is on the roadmap, but it needs the
fallback behaviour, per-application exceptions and far more real-world
testing before it is something to recommend.

## Running it from source

This is for people working on the project; users should use the installer
above, which never downloads source. From the `src/` folder:

```bash
docker compose up -d --build
```

The proxy listens on `127.0.0.1:8080` and the admin panel on
`127.0.0.1:8082`. Check it is up:

```bash
curl -x http://127.0.0.1:8080 http://censor.local/health
```

A source checkout keeps its CA in `src/mitmproxy/`, its settings in
`src/config/config.yaml` and its logs in `src/logs/`. To try it in a
browser without touching anything else, start Chromium with a throwaway
profile:

```bash
chromium --user-data-dir=$HOME/censor-test --proxy-server=127.0.0.1:8080
```

[`src/README.md`](src/README.md) has the full developer guide: trusting the
CA, the admin panel, reviewing detections against the sample set, tuning
video attribution, running the tests and cutting a release.

## Project status

Image and embedded-image censoring are complete and verified in Chromium.
Video censoring works end to end, including seeking, and plays on real
sites, but still has rough edges. The dedicated-browser installer is
verified on Ubuntu; the macOS and Windows scripts are written and
syntax-checked but not yet run on those systems. Automatic updates are
verified against a local registry; nothing has been pushed to the Docker
Hub repository ([`thattransfae/scs`](https://hub.docker.com/r/thattransfae/scs))
yet.

Not yet: a system-wide proxy mode, Firefox support, and tuning the
detection thresholds against more real browsing. The full list, with
dates, is in [`docs/status.md`](docs/status.md).

## Documentation and layout

- [`install/README.md`](install/README.md): the installer, update job,
  options and undo
- [`src/README.md`](src/README.md): run from source, browser setup, the
  admin panel, configuration, detection review, tests, releases
- [`docs/decisions.md`](docs/decisions.md): design decisions and
  detection tuning, with the reasoning behind each threshold
- [`docs/video-design.md`](docs/video-design.md): how video censoring works
- [`docs/status.md`](docs/status.md): what's done and what's next
- [`docs/brand/`](docs/brand/): the mark, README banners and palette
- [`docs/Image Censoring Proxy — Project Handoff.md`](<docs/Image Censoring Proxy — Project Handoff.md>):
  the original plan (historical)

```
README.md               this file
install/                one-line Sissy Browser installers (browser.sh, browser.ps1)
docs/                   design docs, status, decisions, brand
src/censor.py           mitmproxy add-on (entry point)
src/censor_proxy/       detection, imaging, data URIs, video, admin panel, config
src/dist/               files copied onto users' machines: compose.yaml, scs-update
src/config/config.yaml  your overrides (source checkouts)
src/tests/              pytest suite and per-model fixtures
src/tools/              detection review and release tooling
```
