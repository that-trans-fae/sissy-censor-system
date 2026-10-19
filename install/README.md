# Installer

A one-line installer that runs straight from GitHub. It installs Docker
(Docker Engine on Linux, Docker Desktop on macOS and Windows), pulls the
proxy image from Docker Hub and starts it, adds a job that keeps it
updated, and sets up a dedicated "Sissy Browser" for it. Nothing
system-wide changes: no system proxy, no CA in the system trust store, and
your other browsers are untouched. No source code is downloaded: everything
the install needs comes out of the image.

The scripts are served from
[`that-trans-fae/sissy-censor-system`](https://github.com/that-trans-fae/sissy-censor-system)
on GitHub. The image comes from
[`thattransfae/scs`](https://hub.docker.com/r/thattransfae/scs) on Docker
Hub (`SCS_IMAGE_REPO` points elsewhere).

| Script | Platform |
| --- | --- |
| `browser.sh` | Linux and macOS |
| `browser.ps1` | Windows 10/11 |

## Dedicated browser

Installs Chromium (snap or distribution package on Linux, Homebrew cask on
macOS, Hibbiki's build from its GitHub release on Windows, verified against
the release's SHA-256) and adds a **Sissy Browser** launcher with its
own icon (`docs/brand/browser-icon.svg`, shipped in the image). It opens a separate profile with
`--proxy-server=127.0.0.1:8080` and trusts the proxy's CA in that profile's
own certificate store (the "Installed by you" list in
`chrome://certificate-manager`, which needs Chromium 134 or newer), so
nothing is added to the system trust store and no "unsupported command-line
flag" bar appears. On Linux and
macOS the launcher writes the CA into the store at every start, so a
regenerated CA needs no reinstall; on Windows the installer writes it with
the sqlite3 tool, so rerun the installer after a CA change. The profile starts with
the bookmark bar shown and the admin panel (`http://localhost:8082/admin`)
on it; rerunning the installer adds the bookmark to an existing profile if
it is missing (close the browser first). On Linux and macOS the launcher
also starts the proxy container if Docker is up.

Google search is the one site the launcher takes around the proxy
(`--proxy-bypass-list=www.google.com`): Google answers a Chromium that
reaches it over TCP instead of HTTP/3 with a CAPTCHA on every search, and
an HTTP proxy can't carry HTTP/3. The installer locks SafeSearch on instead,
with Chromium's `ForceGoogleSafeSearch` policy (a file in the managed-policy
directory on Linux, written with sudo; the app's user defaults on macOS; the
machine's policy key on Windows, through a UAC prompt), so results are filtered at Google's end
and the setting can't be turned off from the page. The policy covers every
profile of that Chromium, not only the Sissy Browser. Image Search
thumbnails still load through the proxy and are censored.

Linux and macOS:

```bash
curl -fsSL https://raw.githubusercontent.com/that-trans-fae/sissy-censor-system/main/install/browser.sh | bash
```

Windows, pasted into a normal PowerShell window. Don't wrap it in
`powershell -ExecutionPolicy Bypass -c "..."`: Defender blocks that command
line as `Trojan:Win32/Commando.A!ml` before the script is even fetched.
Execution policy doesn't apply to `iex`, so the flag isn't needed.

```powershell
irm https://raw.githubusercontent.com/that-trans-fae/sissy-censor-system/main/install/browser.ps1 | iex
```

Where it lands:

- Linux: `~/.local/bin/sissy-browser`, a `.desktop` entry and icons under
  `~/.local/share`; profile in `~/.config/sissy-browser` (snap Chromium:
  `~/snap/chromium/common/sissy-browser`)
- macOS: `~/Applications/Sissy Browser.app` (the Dock shows Chromium's icon
  while it runs, since the process is Chromium's); profile in
  `~/Library/Application Support/sissy-browser`
- Windows: Start menu and desktop shortcuts; profile and icon in
  `%LOCALAPPDATA%\SissyBrowser`. The CA's key hash is baked into the
  shortcut, so rerun the installer if the proxy ever makes a new CA.

The proxy itself runs from `~/scs` (`%USERPROFILE%\scs`):

| Path | What |
| --- | --- |
| `compose.yaml` | The service, from the image; replaced at each update |
| `compose.vaapi.yaml`, `compose.nvidia.yaml` | GPU overrides, from the image; in use when `COMPOSE_FILE` in `.env` names one |
| `.env` | `SCS_IMAGE` (the running release, by digest), `SCS_CHANNEL`, your uid:gid, the browser profile, `COMPOSE_FILE` (with a GPU override if one is used, and `SCS_RENDER_NODE`/`SCS_RENDER_GID` for VAAPI); add `SCS_PROXY_PORT` or `SCS_ADMIN_PORT` to move the ports |
| `config/config.yaml` | Your settings, written by the admin panel |
| `mitmproxy/` | The CA, kept across updates |
| `logs/` | Detection log; `update.json` is the last update result |
| `scs-update`, `scs-update.ps1` | The updater, from the image |
| `update.log` | Every update run |

## Updates

The installer adds a job that checks for an update once each time you log
in after the computer starts: a systemd user timer `scs-update.timer` on
Linux (a minute after your session starts), a launchd agent
`local.scs.update` on macOS (at login), and a scheduled task "Sissy Censor
System update" on Windows (a minute after sign-in). Nothing runs on a
schedule after that. The run of `scs-update`:

1. waits for Docker and the proxy to come up (Docker Desktop starts at
   login too), for up to 15 minutes.
2. reads `updates.*` from your settings. With automatic updates off (the
   switch on the admin panel's Overview) it stops here.
3. pulls `SCS_CHANNEL` (`thattransfae/scs:latest`) from Docker Hub, trying
   for up to ten minutes while the network comes up. This is the only thing
   the install sends to the internet on its own.
4. if that is a new release, waits until the Sissy Browser is closed
   (`updates.wait_for_browser`), then switches the proxy to it: the new
   `compose.yaml` goes in place, `SCS_IMAGE` in `.env` moves to the new
   digest and the container is recreated, which takes the proxy away for a
   few seconds.
5. keeps the new release once its health check passes, usually within 15
   seconds. If it doesn't pass within five minutes, the old `compose.yaml`
   and image go back, and that release is skipped until a newer one is out.

The result shows on the admin panel's Overview (Updates) and is logged in
`update.log`. To check and update at once, even with the browser open:

```bash
~/scs/scs-update --now
```

```powershell
powershell -ExecutionPolicy Bypass -File $env:USERPROFILE\scs\scs-update.ps1 -Now
```

To stay on a release, set `SCS_CHANNEL` in `.env` to a version tag such as
`thattransfae/scs:1.2.0`, or turn automatic updates off on the panel.

## Options and undo

Both scripts read these environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `SCS_IMAGE_REPO` | `thattransfae/scs` | Docker Hub repository |
| `SCS_CHANNEL` | `$SCS_IMAGE_REPO:latest` | Tag to install and follow |
| `SCS_DIR` | `~/scs` (`%USERPROFILE%\scs`) | Where the proxy runs from |
| `SCS_UNINSTALL` | unset | `1` uninstalls (same as `--uninstall` / `-Uninstall`) |
| `SCS_GPU` | `1` | `0` leaves the GPU out of the container (see below) |

Uninstalling removes the browser launcher, its profile, the update job and
the proxy's container and image, then asks before removing Docker (with
everything in it), Chromium and, on Windows, WSL, and finally deletes
`~/scs` (config, logs and the CA).

```bash
curl -fsSL https://raw.githubusercontent.com/that-trans-fae/sissy-censor-system/main/install/browser.sh | bash -s -- --uninstall
```

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/that-trans-fae/sissy-censor-system/main/install/browser.ps1))) -Uninstall
```

On Linux and Windows the installer looks for a GPU to decode and encode
video with: NVIDIA (on Linux, with the NVIDIA Container Toolkit
installed), or on Linux an Intel or AMD render node. It is passed into the
container only if a test encode works there, since compose won't start the
proxy with a device that isn't there; rerun the installer after adding or
removing a GPU or its driver. Without one, video uses the CPU, as on macOS.

Rerunning the installer is safe: it installs the channel's current
release straight away (without the health check that `scs-update` does)
and reapplies the settings. An install made by an earlier version of the
installer, which cloned the repository into `~/scs`, is moved over: its
container is stopped and its CA and settings are copied out of `~/scs/src`.

## First run notes

- Linux: Docker Engine comes from Docker's apt or dnf repository (Arch:
  the distribution's packages) and starts with the machine. The installer
  adds you to the `docker` group and uses sudo for Docker until you log out
  and back in.
- The container runs as your uid:gid, so it can write its CA into
  `~/scs/mitmproxy`: the installer writes them to `.env`, which `user:` in
  `compose.yaml` reads.
- The first download is over a gigabyte (Python, ffmpeg and both models).
  Updates download only the layers that changed.
- If another container already holds port 8080 (a development copy run from
  `src/`, say), the installer stops and names it.
- The Windows task runs through `conhost --headless` so no console window
  flashes up each hour.
- macOS and Windows: Docker Desktop shows its service agreement on first
  start; the scripts wait (up to 15 minutes) until it is accepted and the
  engine is up.
- On Windows the Docker Desktop install needs WSL 2. If it isn't enabled,
  the installer tells you the command to run.
