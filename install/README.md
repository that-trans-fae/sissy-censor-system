# Installers

One-line installers that run straight from GitHub. Every one installs Docker
Desktop, clones this repository, builds and starts the proxy, and then either
points the whole system at it or sets up a dedicated browser for it.

Replace `OWNER` in the URLs below with the GitHub account that hosts the
repository. Each script carries the same placeholder in `SCS_REPO_URL`
(marked `TODO` near the top); set it there before publishing, or pass the
real URL in the environment when running.

| Script | Platform | What it changes system-wide |
| --- | --- | --- |
| `linux.sh` | Linux (apt, dnf, pacman; x86_64) | CA in the system store and in Chromium/Chrome/Edge/Brave/Firefox policies; GNOME proxy settings |
| `macos.sh` | macOS | CA in the System keychain (and a Firefox policy); HTTP/HTTPS proxy on every network service |
| `windows.ps1` | Windows 10/11 | CA in the machine Trusted Root store (and a Firefox policy); the Windows proxy |
| `browser.sh`, `browser.ps1` | Linux and macOS, Windows | Nothing: a separate "Sissy Browser" Chromium profile pinned to the proxy |

## System level

Linux (run as your normal user; sudo is asked for where needed):

```bash
curl -fsSL https://raw.githubusercontent.com/OWNER/scs/main/install/linux.sh | bash
```

macOS:

```bash
curl -fsSL https://raw.githubusercontent.com/OWNER/scs/main/install/macos.sh | bash
```

Windows, from an elevated PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/OWNER/scs/main/install/windows.ps1 | iex"
```

The system proxy is set to a fixed `127.0.0.1:8080`, so browsing fails
closed while the container is stopped (`docker compose down` in `src/`).
Docker Desktop restarts the container with the machine.

Docker Desktop follows the system proxy on macOS and Windows, which would
send its own image pulls through the censor. After installing, open Docker
Desktop's Settings > Resources > Proxies and choose manual configuration
with empty fields.

## Dedicated browser

Installs Chromium (snap or distribution package on Linux, Homebrew cask on
macOS, winget on Windows) and adds a **Sissy Browser** launcher with its
own icon (`docs/brand/browser-icon.svg`). It opens a separate profile with
`--proxy-server=127.0.0.1:8080` and trusts the proxy's CA in that profile's
own certificate store (the "Installed by you" list in
`chrome://certificate-manager`, which needs Chromium 134 or newer), so
nothing is added to the system trust store, no "unsupported command-line
flag" bar appears, and your other browsers are untouched. On Linux and
macOS the launcher writes the CA into the store at every start, so a
regenerated CA needs no reinstall; on Windows the installer writes it with
the sqlite3 tool, so rerun the installer after a CA change. The profile starts with
the bookmark bar shown and the admin panel (`http://localhost:8082/admin`)
on it; rerunning the installer adds the bookmark to an existing profile if
it is missing (close the browser first). On Linux and macOS the launcher
also starts the proxy container if Docker is up.

Linux and macOS:

```bash
curl -fsSL https://raw.githubusercontent.com/OWNER/scs/main/install/browser.sh | bash
```

Windows, from a normal PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/OWNER/scs/main/install/browser.ps1 | iex"
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

## Options and undo

Every script reads these environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `SCS_REPO_URL` | placeholder | Repository to clone |
| `SCS_DIR` | `~/scs` (`%USERPROFILE%\scs`) | Where to clone it |
| `SCS_BRANCH` | `main` | Branch to check out |
| `SCS_UNINSTALL` | unset | `1` reverses the script's trust, proxy or launcher changes |

Undo keeps Docker Desktop, Chromium and the checkout:

```bash
curl -fsSL https://raw.githubusercontent.com/OWNER/scs/main/install/linux.sh | SCS_UNINSTALL=1 bash
```

```powershell
powershell -ExecutionPolicy Bypass -c "$env:SCS_UNINSTALL='1'; irm https://raw.githubusercontent.com/OWNER/scs/main/install/windows.ps1 | iex"
```

Rerunning an installer is safe: it updates the checkout, rebuilds the
image and reapplies the settings.

## First run notes

- Docker Desktop shows its service agreement on first start; the scripts
  wait (up to 15 minutes) until it is accepted and the engine is up.
- On Windows the Docker Desktop install needs WSL 2. The system installer
  enables it and asks for a reboot; the browser installer tells you the
  command to run.
- Docker Desktop for Linux needs KVM and only ships for x86_64.
- Restart any browser that was open during a system-level install so it
  picks up the CA policy and the proxy.
