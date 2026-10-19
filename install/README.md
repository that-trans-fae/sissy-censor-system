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

Every step the scripts take is also written out under
[Manual install](#manual-install), for doing it by hand.

## Dedicated browser

Installs Chromium (snap or distribution package on Linux, Homebrew's `ungoogled-chromium` cask on
macOS, since its `chromium` cask is disabled, Hibbiki's build from its GitHub release on Windows, verified against
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

## Manual install

The same install, step by step, without running a script from the
internet: for a distribution the script doesn't know, a locked-down
machine, or to see what each piece is. It ends in the same layout as the
one-line install, so `scs-update` and the uninstaller work on it, and
rerunning the one-liner later is safe. The commands are for Linux and
macOS; the Windows differences are noted where they exist. Paths such as
`~/scs` are the defaults, not requirements.

### 1. Docker

Linux: install Docker Engine with the compose plugin from Docker's own
repository ([docs.docker.com/engine/install](https://docs.docker.com/engine/install/);
on Arch, the distribution's `docker`, `docker-compose` and `docker-buildx`
packages), start it with the machine, and let your user talk to it:

```bash
sudo systemctl enable --now docker.service
```

```bash
sudo usermod -aG docker "$USER"
```

The group takes effect at your next login; until then put `sudo` in front
of every `docker` command below.

macOS and Windows: install Docker Desktop (`brew install --cask
docker-desktop`, or `winget install Docker.DockerDesktop`; Windows needs
WSL 2 first, `wsl --install --no-distribution` from an elevated PowerShell
and a reboot). Start it, accept the service agreement in its window, and
wait until `docker info` works.

### 2. The proxy

Make the install folder with the three folders the container writes to.
Docker Engine would otherwise create them as root, where the unprivileged
container can't write:

```bash
mkdir -p ~/scs/mitmproxy ~/scs/config ~/scs/logs && cd ~/scs
```

Pull the release and note its digest (the `thattransfae/scs@sha256:...`
line):

```bash
docker pull thattransfae/scs:latest
```

```bash
docker image inspect -f '{{range .RepoDigests}}{{println .}}{{end}}' thattransfae/scs:latest
```

Give the image a local tag named after that digest. Docker's image store
forgets `repo@sha256:...` once the `latest` tag moves on to a newer image,
and compose would then fail to start the pinned one:

```bash
docker tag thattransfae/scs@sha256:HEX thattransfae/scs:sha256-HEX
```

Copy the install files out of the image: `compose.yaml`, the GPU
overrides, `scs-update`, `scs-update.ps1` and the browser icon. Nothing
else comes out of it:

```bash
docker run --rm --network none --entrypoint tar thattransfae/scs:latest -C /app/dist -cf - . | tar -xf - -C ~/scs
```

```powershell
$cid = docker create thattransfae/scs:latest; docker cp "${cid}:/app/dist/." $env:USERPROFILE\scs; docker rm $cid
```

Write `~/scs/.env`, which `compose.yaml` and `scs-update` read. `SCS_UID`
and `SCS_GID` are your `id -u` and `id -g` (leave them out on Windows, where
Docker Desktop maps the files); `SCS_PROFILE` is the browser profile folder
from step 4, quoted if it has a space; `SCS_PROXY_PORT` and `SCS_ADMIN_PORT`
are optional and move the two ports:

```
SCS_CHANNEL=thattransfae/scs:latest
SCS_IMAGE=thattransfae/scs@sha256:HEX
SCS_UID=1000
SCS_GID=1000
COMPOSE_FILE=compose.yaml
SCS_PROFILE="/home/you/.config/sissy-browser"
```

Optionally, a GPU for video (Linux, and NVIDIA on Windows). Check the
container's own test encode first, since compose won't start the proxy
with a device that isn't there. NVIDIA, with the NVIDIA Container Toolkit
installed on Linux:

```bash
docker compose -f compose.yaml -f compose.nvidia.yaml run --rm --no-deps -T censor-proxy python -m censor_proxy.hwaccel
```

Intel or AMD on Linux, after adding `SCS_RENDER_NODE=/dev/dri/renderD128`
(your render node) and `SCS_RENDER_GID=` with that node's group id
(`stat -c %g /dev/dri/renderD128`) to `.env`:

```bash
docker compose -f compose.yaml -f compose.vaapi.yaml run --rm --no-deps -T censor-proxy python -m censor_proxy.hwaccel
```

If the command exits 0, append the override to `COMPOSE_FILE` in `.env`:
`compose.yaml:compose.nvidia.yaml` or `compose.yaml:compose.vaapi.yaml`
(Windows separates with `;`). `scs-update` carries the overrides across
updates.

Start the proxy. It restarts with Docker from now on:

```bash
docker compose up -d
```

Within a minute `~/scs/mitmproxy/mitmproxy-ca-cert.pem` appears (the CA the
browser will trust) and, once the models are loaded, the health check
answers through the proxy:

```bash
curl -x http://127.0.0.1:8080 http://censor.local/health
```

The admin panel is at <http://localhost:8082/admin>.

### 3. Chromium

Chromium 134 or newer, for the per-profile certificate store the next step
uses: `sudo snap install chromium` on Ubuntu, otherwise the `chromium`
package (apt, dnf, pacman); on macOS `brew install --cask
ungoogled-chromium` (Homebrew's `chromium` cask is disabled) followed by
`xattr -dr com.apple.quarantine /Applications/Chromium.app`, since the
build isn't notarized; on Windows the `mini_installer.exe` from
[Hibbiki's latest release](https://github.com/Hibbiki/chromium-win64/releases/latest),
checked against the SHA-256 on the release page.

### 4. The Sissy Browser profile

The Sissy Browser is Chromium started with a separate profile folder and
the proxy flags. This is the whole launcher:

```bash
chromium --user-data-dir="$HOME/.config/sissy-browser" --proxy-server=127.0.0.1:8080 --proxy-bypass-list=www.google.com --enable-features=EnableCertManagementUIV2Write --no-first-run --no-default-browser-check
```

- `--user-data-dir` keeps the profile apart from your usual browsing. Snap
  Chromium can only reach its own area, so use
  `~/snap/chromium/common/sissy-browser` there; on macOS the command is
  `/Applications/Chromium.app/Contents/MacOS/Chromium` and the profile goes
  in `~/Library/Application Support/sissy-browser`; on Windows,
  `%LOCALAPPDATA%\SissyBrowser\profile`. Whatever you choose is what
  `SCS_PROFILE` in `.env` names.
- `--proxy-server` sends everything through the proxy and
  `--proxy-bypass-list` takes Google search around it (Google challenges a
  Chromium that can't use HTTP/3 with a CAPTCHA on every search). Step 5
  locks SafeSearch on instead.
- `--enable-features=EnableCertManagementUIV2Write` makes Chromium 134-136
  keep the profile certificate store; later versions ignore it.

Start it once. Pages show certificate warnings until the CA is trusted:
open `chrome://certificate-manager`, and under **Custom**, **Installed by
you**, import `~/scs/mitmproxy/mitmproxy-ca-cert.pem` into the trusted
certificates. Trust lives in this profile only; nothing is added to the
system store or your other browsers. The scripted launcher writes the same
entry into the profile's store at every start (`Default/ServerCertificate`,
a SQLite file), so with a manual install, redo this import if you ever
delete `~/scs/mitmproxy` and the proxy makes a new CA.

Then put the admin panel on the bookmark bar (Ctrl+Shift+B shows the bar):
<http://localhost:8082/admin>.

Save the command somewhere you can start it from. On Linux, a `.desktop`
entry in `~/.local/share/applications/sissy-browser.desktop` with the
command as `Exec=` and `~/scs/browser-icon.svg` as `Icon=`; add
`--class=SissyBrowser` to the command and `StartupWMClass=SissyBrowser` to
the entry so the window gets its own dock icon (with snap Chromium, `Exec`
must run `/snap/bin/chromium` directly, not a wrapper script, or GNOME
groups it with Chromium). On macOS, an Automator "Run Shell Script"
application or a wrapper `.app` in `~/Applications`. On Windows, a shortcut
whose target is `chrome.exe` with the flags as its arguments. The scripted
launchers also run `docker compose up -d` in `~/scs` before starting
Chromium; a manual install can rely on the container's
`restart: unless-stopped` instead, which brings it back whenever Docker
starts.

### 5. SafeSearch

Since Google search goes around the proxy, lock SafeSearch on with
Chromium's `ForceGoogleSafeSearch` policy. It applies to every profile of
that Chromium, and the setting can't be turned off from Google's page.

Linux, as root, in `/etc/chromium/policies/managed/sissy-browser.json`
(snap: `/var/snap/chromium/current/policies/managed/sissy-browser.json`):

```json
{ "ForceGoogleSafeSearch": true }
```

macOS:

```bash
defaults write org.chromium.Chromium ForceGoogleSafeSearch -bool true
```

Windows, in an elevated PowerShell:

```powershell
reg add HKLM\Software\Policies\Chromium /v ForceGoogleSafeSearch /t REG_DWORD /d 1 /f
```

Restart the browser and check that `chrome://policy` lists the policy as
OK.

### 6. Updates

Nothing checks for updates on its own until you add the job. To check and
update by hand at any time:

```bash
~/scs/scs-update --now
```

```powershell
powershell -ExecutionPolicy Bypass -File $env:USERPROFILE\scs\scs-update.ps1 -Now
```

To get the same startup check the one-line install adds, which waits for
the Sissy Browser to close before switching releases (that is what
`SCS_PROFILE` in `.env` is for):

Linux, two files in `~/.config/systemd/user/`, then
`systemctl --user daemon-reload` and `systemctl --user enable --now scs-update.timer`:

```ini
# scs-update.service
[Unit]
Description=Sissy Censor System update check

[Service]
Type=oneshot
TimeoutStartSec=infinity
ExecStart=%h/scs/scs-update
```

```ini
# scs-update.timer
[Unit]
Description=Sissy Censor System update check at startup

[Timer]
OnStartupSec=1min

[Install]
WantedBy=timers.target
```

macOS, `~/Library/LaunchAgents/local.scs.update.plist`, then
`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.scs.update.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.scs.update</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>/Users/you/scs/scs-update</string></array>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
```

Windows, a scheduled task of your user that runs at sign-in with a minute's
delay and no time limit:

```powershell
$a = New-ScheduledTaskAction -Execute conhost.exe -Argument "--headless powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$env:USERPROFILE\scs\scs-update.ps1`""
$t = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"; $t.Delay = 'PT1M'
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName 'Sissy Censor System update' -Action $a -Trigger $t -Settings $s -Force
```

### Undoing a manual install

In reverse: remove the launcher and the profile folder, the SafeSearch
policy file, key or default, and the update job; then, in `~/scs`,
`docker compose down`, `docker image rm` of every `thattransfae/scs` image
(`docker images thattransfae/scs -q`), and delete `~/scs`, which takes the
CA, settings and logs with it. The one-line uninstaller does the same
steps, and works on a manual install that used the default paths above.
