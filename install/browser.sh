#!/usr/bin/env bash
# Sissy Censor System: dedicated browser installer for Linux and macOS.
#
# Installs Docker (Docker Engine on Linux, Docker Desktop on macOS), pulls
# the proxy image from Docker Hub and starts it from ~/scs, adds a job that
# checks for updates at each startup (scs-update, from the image), installs Chromium
# and adds a "Sissy Browser" launcher: a separate Chromium profile that
# always goes through the proxy and trusts its CA in the profile's own
# certificate store. Nothing system-wide changes: no system proxy, no CA in
# the system store, and your usual browsers are untouched. Windows:
# install/browser.ps1.
#
# Run as your normal user (not root); sudo is asked for where it is needed:
#
#   curl -fsSL https://raw.githubusercontent.com/that-trans-fae/sissy-censor-system/main/install/browser.sh | bash
#
# Uninstall: removes the launcher, its profile, the update job and the
# proxy's container and image, asks before removing Docker and Chromium,
# and deletes ~/scs (config, logs, CA). --uninstall, or SCS_UNINSTALL=1 in
# the environment:
#
#   curl -fsSL https://raw.githubusercontent.com/that-trans-fae/sissy-censor-system/main/install/browser.sh | bash -s -- --uninstall
#
# Environment overrides: SCS_IMAGE_REPO, SCS_CHANNEL, SCS_DIR, and SCS_GPU=0
# to leave the GPU out of the container.
set -euo pipefail

# ---------------------------------------------------------------- settings
SCS_IMAGE_REPO="${SCS_IMAGE_REPO:-thattransfae/scs}"
# The tag installs follow; scs-update moves to whatever it points at.
SCS_CHANNEL="${SCS_CHANNEL:-$SCS_IMAGE_REPO:latest}"
SCS_DIR="${SCS_DIR:-$HOME/scs}"
SCS_UNINSTALL="${SCS_UNINSTALL:-0}"
SCS_GPU="${SCS_GPU:-1}"
for arg in "$@"; do
  case $arg in
    --uninstall) SCS_UNINSTALL=1 ;;
    *) echo "unknown option: $arg (the only option is --uninstall)" >&2; exit 2 ;;
  esac
done

BROWSER_NAME="Sissy Browser"
BROWSER_ID=sissy-browser
PROXY_HOST=127.0.0.1
PROXY_PORT=8080
CA_PEM="$SCS_DIR/mitmproxy/mitmproxy-ca-cert.pem"
ICON_SVG="$SCS_DIR/browser-icon.svg"
UPDATE_UNIT=scs-update
UPDATE_AGENT=local.scs.update
ADMIN_URL="http://localhost:8082/admin"
ADMIN_BOOKMARK="SCS admin panel"
OS=$(uname -s)
# Docker as this user, or through sudo until a new login picks up the
# docker group (Linux).
DOCKER=(docker)

# ----------------------------------------------------------------- helpers
log()  { printf '\033[1;35m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

[ "$(id -u)" -ne 0 ] || die "run this as your normal user, not root; sudo is used where needed"
case "$OS" in Linux|Darwin) ;; *) die "this installer is for Linux and macOS" ;; esac

PKG=
if [ "$OS" = Linux ]; then
  have sudo || die "sudo is required"
  if have apt-get; then PKG=apt
  elif have dnf; then PKG=dnf
  elif have pacman; then PKG=pacman
  else die "unsupported distribution: need apt, dnf or pacman"
  fi
fi

pkg_install() {
  case "$PKG" in
    apt)    sudo apt-get install -y "$@" ;;
    dnf)    sudo dnf install -y "$@" ;;
    pacman) sudo pacman -S --needed --noconfirm "$@" ;;
  esac
}

brew_bin() {
  if [ -x /opt/homebrew/bin/brew ]; then echo /opt/homebrew/bin/brew
  elif [ -x /usr/local/bin/brew ]; then echo /usr/local/bin/brew
  else command -v brew 2>/dev/null || true
  fi
}

# ------------------------------------------------------------------ docker
# Docker Engine from Docker's own repositories (Arch: the distribution's).
# The script and the launcher talk to it on its socket, so a Docker Desktop
# context, if one is set, is left alone.
install_docker_linux() {
  if have docker && systemctl cat docker.service >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    log "Docker is already installed"
    return
  fi
  log "Installing Docker"
  case "$PKG" in
    apt)
      pkg_install ca-certificates curl
      . /etc/os-release
      sudo install -m 0755 -d /etc/apt/keyrings
      sudo curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
      sudo chmod a+r /etc/apt/keyrings/docker.asc
      echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$ID $VERSION_CODENAME stable" \
        | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
      sudo apt-get update
      pkg_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
      ;;
    dnf)
      if dnf --version 2>/dev/null | head -1 | grep -q '^dnf5'; then
        sudo dnf config-manager addrepo --overwrite --from-repofile=https://download.docker.com/linux/fedora/docker-ce.repo
      else
        sudo dnf -y install dnf-plugins-core
        sudo dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo
      fi
      pkg_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
      ;;
    pacman)
      pkg_install docker docker-compose docker-buildx
      ;;
  esac
}

install_docker_desktop_macos() {
  if [ -z "$(brew_bin)" ]; then
    log "Installing Homebrew"
    NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  fi
  if [ -d /Applications/Docker.app ]; then
    log "Docker Desktop is already installed"
    return
  fi
  log "Installing Docker Desktop"
  local brew; brew=$(brew_bin)
  "$brew" install --cask docker-desktop || "$brew" install --cask docker
}

GROUP_ADDED=0
start_docker() {
  if [ "$OS" = Linux ]; then
    log "Starting Docker"
    export DOCKER_HOST=unix:///var/run/docker.sock
    sudo systemctl enable --now docker.service
    if ! id -nG "$USER" | grep -qw docker; then
      log "Adding $USER to the docker group (takes effect at your next login)"
      sudo usermod -aG docker "$USER"
      GROUP_ADDED=1
    fi
    docker info >/dev/null 2>&1 || DOCKER=(sudo docker)
    "${DOCKER[@]}" info >/dev/null || die "Docker did not start; check 'systemctl status docker'"
    return
  fi
  log "Starting Docker Desktop"
  open -ga Docker
  local waited=0
  until docker info >/dev/null 2>&1; do
    if [ "$waited" -eq 0 ]; then
      echo "    Waiting for Docker Desktop. On first start, accept the service agreement"
      echo "    in its window and leave it running."
    fi
    [ "$waited" -lt 900 ] || die "Docker Desktop did not become ready within 15 minutes"
    sleep 5; waited=$((waited + 5))
  done
}

# --------------------------------------------------------------- the proxy
# KEY=value in ~/scs/.env, which compose reads; other keys are kept.
env_set() {
  local file="$SCS_DIR/.env"
  [ -f "$file" ] || printf '# Compose settings for this install (written by the installer and scs-update).\n' >"$file"
  awk -v k="$1" -v v="$2" '
    index($0, k "=") == 1 { if (!done) print k "=" v; done = 1; next }
    { print }
    END { if (!done) print k "=" v }' "$file" >"$file.tmp" && mv -f "$file.tmp" "$file"
}

# Earlier installers cloned the repository and built the image in
# $SCS_DIR/src. Stop that container and keep its CA (which the profile
# already trusts) and settings.
migrate_checkout() {
  [ -f "$SCS_DIR/src/compose.yaml" ] || return 0
  log "Moving the CA and settings out of the old checkout layout"
  (cd "$SCS_DIR/src" && "${DOCKER[@]}" compose down) || true
  local d
  for d in mitmproxy config; do
    if [ -d "$SCS_DIR/src/$d" ] && [ -z "$(ls -A "$SCS_DIR/$d" 2>/dev/null)" ]; then
      mkdir -p "$SCS_DIR/$d"
      cp -Rp "$SCS_DIR/src/$d/." "$SCS_DIR/$d/"
    fi
  done
}

get_proxy() {
  log "Downloading $SCS_CHANNEL (over a gigabyte the first time)"
  "${DOCKER[@]}" pull "$SCS_CHANNEL"
  local repo=${SCS_CHANNEL%:*} image
  image=$("${DOCKER[@]}" image inspect -f '{{range .RepoDigests}}{{println .}}{{end}}' "$SCS_CHANNEL" \
          | awk -v p="$repo@sha256:" 'index($0, p) == 1 { print; exit }')
  [ -n "$image" ] || die "pulled $SCS_CHANNEL but found no digest for it"
  # A local tag named after the digest keeps repo@sha256:... resolvable once
  # the channel tag moves on (scs-update does the same).
  "${DOCKER[@]}" tag "$image" "$repo:sha256-${image##*@sha256:}"
  OLD_IMAGE=
  [ ! -f "$SCS_DIR/.env" ] || OLD_IMAGE=$(sed -n 's/^SCS_IMAGE=//p' "$SCS_DIR/.env" | tail -n 1)
  migrate_checkout
  # The container runs unprivileged and writes to these mounts; Docker Engine
  # would create missing ones as root, where it can't.
  mkdir -p "$SCS_DIR/mitmproxy" "$SCS_DIR/config" "$SCS_DIR/logs"
  # compose.yaml, the updater and the icon come out of the image, through
  # tar on stdout so the files are ours even while Docker needs sudo.
  "${DOCKER[@]}" run --rm --network none --entrypoint tar "$image" -C /app/dist -cf - . | tar -xf - -C "$SCS_DIR"
  chmod +x "$SCS_DIR/scs-update"
  env_set SCS_CHANNEL "$SCS_CHANNEL"
  env_set SCS_IMAGE "$image"
  # Run the container as this user (user: in compose.yaml).
  env_set SCS_UID "$(id -u)"
  env_set SCS_GID "$(id -g)"
}

# A GPU for video decoding and encoding (video.hwaccel) on Linux: compose
# passes one in through an override file that COMPOSE_FILE in .env adds.
# An override is kept only if the container's own test encode works with
# it, since a device compose can't set up stops the proxy from starting.
gpu_works() {
  (cd "$SCS_DIR" && "${DOCKER[@]}" compose -f compose.yaml -f "$1" run --rm --no-deps -T \
     censor-proxy python -m censor_proxy.hwaccel) >/dev/null 2>&1
}

setup_gpu() {
  local files=compose.yaml node
  if [ "$OS" = Linux ] && [ "$SCS_GPU" != 0 ]; then
    log "Looking for a GPU for video"
    if have nvidia-smi && { have nvidia-container-runtime-hook || have nvidia-ctk; } \
       && gpu_works compose.nvidia.yaml; then
      files="$files:compose.nvidia.yaml"
    else
      for node in /dev/dri/renderD*; do
        [ -c "$node" ] || continue
        env_set SCS_RENDER_NODE "$node"
        env_set SCS_RENDER_GID "$(stat -c %g "$node")"
        if gpu_works compose.vaapi.yaml; then
          files="$files:compose.vaapi.yaml"
          break
        fi
      done
    fi
  fi
  case $files in
    *nvidia*) log "Video will use the NVIDIA GPU" ;;
    *vaapi*) log "Video will use the GPU at $node" ;;
    *) [ "$OS" != Linux ] || log "No usable GPU found; video uses the CPU" ;;
  esac
  [ "$OS" != Linux ] || env_set COMPOSE_FILE "$files"
}

start_proxy() {
  local other
  other=$("${DOCKER[@]}" ps --filter "publish=$PROXY_PORT" --format '{{.Names}} {{.Label "com.docker.compose.project"}}' \
          | awk '$2 != "scs" { print $1; exit }')
  [ -z "$other" ] || die "another container ($other) holds port $PROXY_PORT; stop it (docker stop $other) and rerun"
  log "Starting the proxy"
  (cd "$SCS_DIR" && "${DOCKER[@]}" compose up -d)
  # The release a reinstall replaced.
  if [ -n "${OLD_IMAGE:-}" ] && [ "$OLD_IMAGE" != "$(sed -n 's/^SCS_IMAGE=//p' "$SCS_DIR/.env" | tail -n 1)" ]; then
    "${DOCKER[@]}" image rm "${OLD_IMAGE%@sha256:*}:sha256-${OLD_IMAGE##*@sha256:}" >/dev/null 2>&1 || true
  fi
  local waited=0
  until [ -s "$CA_PEM" ]; do
    [ "$waited" -lt 120 ] || die "the proxy did not write its CA to $CA_PEM"
    sleep 2; waited=$((waited + 2))
  done
  # The container takes a few seconds to load the models after (re)creation.
  waited=0
  until curl -fsS -x "http://$PROXY_HOST:$PROXY_PORT" http://censor.local/health >/dev/null 2>&1; do
    if [ "$waited" -ge 60 ]; then
      warn "health check through the proxy failed; check 'docker compose logs' in $SCS_DIR"
      break
    fi
    sleep 3; waited=$((waited + 3))
  done
}

# One scs-update run each time you log in after the computer starts: a
# systemd user timer on Linux (a minute after the user session starts), a
# launchd agent on macOS (at login). The run may last while it waits for the
# browser to close, so it has no time limit.
install_updater() {
  log "Adding the update check at startup"
  if [ "$OS" = Linux ]; then
    if ! systemctl --user show-environment >/dev/null 2>&1; then
      warn "no systemd user session, so updates won't run on their own; run $SCS_DIR/scs-update now and then"
      return
    fi
    local units="$HOME/.config/systemd/user"
    mkdir -p "$units"
    cat >"$units/$UPDATE_UNIT.service" <<UNIT
[Unit]
Description=Sissy Censor System update check

[Service]
Type=oneshot
TimeoutStartSec=infinity
ExecStart="$SCS_DIR/scs-update"
UNIT
    cat >"$units/$UPDATE_UNIT.timer" <<UNIT
[Unit]
Description=Sissy Censor System update check at startup

[Timer]
OnStartupSec=1min

[Install]
WantedBy=timers.target
UNIT
    systemctl --user daemon-reload
    systemctl --user enable --now "$UPDATE_UNIT.timer" >/dev/null 2>&1
    return
  fi
  local plist="$HOME/Library/LaunchAgents/$UPDATE_AGENT.plist"
  mkdir -p "$(dirname "$plist")"
  cat >"$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$UPDATE_AGENT</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$SCS_DIR/scs-update</string></array>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/dev/null</string>
  <key>StandardErrorPath</key><string>/dev/null</string>
</dict>
</plist>
PLIST
  launchctl bootout "gui/$(id -u)/$UPDATE_AGENT" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
}

uninstall_updater() {
  log "Removing the update check"
  if [ "$OS" = Linux ]; then
    systemctl --user disable --now "$UPDATE_UNIT.timer" >/dev/null 2>&1 || true
    rm -f "$HOME/.config/systemd/user/$UPDATE_UNIT.service" "$HOME/.config/systemd/user/$UPDATE_UNIT.timer"
    systemctl --user daemon-reload 2>/dev/null || true
  else
    launchctl bootout "gui/$(id -u)/$UPDATE_AGENT" 2>/dev/null || true
    rm -f "$HOME/Library/LaunchAgents/$UPDATE_AGENT.plist"
  fi
}

# ---------------------------------------------------------------- chromium
# Prints the Chromium command; installs it first if missing.
CHROMIUM=
install_chromium() {
  if [ "$OS" = Darwin ]; then
    if [ ! -d /Applications/Chromium.app ]; then
      log "Installing Chromium"
      "$(brew_bin)" install --cask --no-quarantine chromium
    fi
    CHROMIUM=/Applications/Chromium.app/Contents/MacOS/Chromium
    return
  fi
  for c in chromium chromium-browser; do have "$c" && CHROMIUM=$(command -v "$c") && break; done
  if [ -z "$CHROMIUM" ]; then
    log "Installing Chromium"
    case "$PKG" in
      apt)
        if have snap && grep -qi ubuntu /etc/os-release; then sudo snap install chromium
        else pkg_install chromium
        fi
        ;;
      dnf|pacman) pkg_install chromium ;;
    esac
    hash -r
    for c in chromium chromium-browser; do have "$c" && CHROMIUM=$(command -v "$c") && break; done
  fi
  [ -n "$CHROMIUM" ] || die "could not find a chromium command after installing it"
}

# Chromium's SingletonLock is a dangling symlink (to host-pid), so -e misses it.
profile_in_use() {
  local base; base=$(profile_dir)
  [ -L "$base/SingletonLock" ] || [ -e "$base/SingletonLock" ] || [ -S "$base/SingletonSocket" ]
}

# The profile directory. Snap Chromium can only reach its own home area.
profile_dir() {
  if [ "$OS" = Linux ] && [ -d "$HOME/snap/chromium" ]; then echo "$HOME/snap/chromium/common/$BROWSER_ID"
  elif [ "$OS" = Darwin ]; then echo "$HOME/Library/Application Support/$BROWSER_ID"
  else echo "$HOME/.config/$BROWSER_ID"
  fi
}

# The launcher. Before starting Chromium it adds the proxy CA to the
# profile's own certificate store (the "Installed by you" list in
# chrome://certificate-manager, a SQLite file Chromium creates on first
# start), so no command-line flag and no system-wide trust is needed, and a
# regenerated CA (after deleting ~/scs/mitmproxy/) needs no reinstall.
LAUNCHER=
write_launcher() {
  local path=$1
  mkdir -p "$(dirname "$path")"
  cat >"$path" <<EOF
#!/usr/bin/env bash
# $BROWSER_NAME: Chromium pinned to the Sissy Censor System proxy.
SCS_DIR="$SCS_DIR"
PROFILE="$(profile_dir)"
CHROMIUM="$CHROMIUM"
CA="\$SCS_DIR/mitmproxy/mitmproxy-ca-cert.pem"
DB="\$PROFILE/Default/ServerCertificate"

# Start the proxy if Docker is up and it is not running yet.
$( [ "$OS" = Linux ] && echo 'export DOCKER_HOST=unix:///var/run/docker.sock' )
if command -v timeout >/dev/null 2>&1; then t="timeout 30"; else t=""; fi
(cd "\$SCS_DIR" && \$t docker compose up -d >/dev/null 2>&1) || true

# Trust the proxy CA in this profile's certificate store. Chromium creates
# the store on first start, so a new profile is initialised headless first.
# Chromium 134-136 only keep the store with this feature on (on by default
# from 137; later builds ignore the name), so it goes on every start.
FEATURES=--enable-features=EnableCertManagementUIV2Write
if [ ! -s "\$DB" ]; then
  "\$CHROMIUM" --user-data-dir="\$PROFILE" "\$FEATURES" --headless=new --disable-gpu --no-first-run --dump-dom about:blank >/dev/null 2>&1
fi
if [ -s "\$DB" ] && [ -s "\$CA" ]; then
  python3 - "\$DB" "\$CA" <<'PY' 2>/dev/null
import base64, hashlib, sqlite3, sys
db, ca = sys.argv[1:]
pem = open(ca).read()
der = base64.b64decode("".join(l for l in pem.splitlines() if not l.startswith("-----")))
digest = hashlib.sha256(der).hexdigest().upper()
# trust_settings is a CertificateMetadata protobuf: trust { trust_type: TRUSTED }
c = sqlite3.connect(db)
c.execute("insert or ignore into certificates values (?, ?, ?)", (digest, der, bytes.fromhex("0a020803")))
c.commit()
PY
fi

exec "\$CHROMIUM" \\
  --user-data-dir="\$PROFILE" "\$FEATURES" \\
  --proxy-server="$PROXY_HOST:$PROXY_PORT" \\
  --class=SissyBrowser \\
  --no-first-run --no-default-browser-check \\
  "\$@"
EOF
  chmod +x "$path"
  LAUNCHER=$path
}

# Runs the launcher once headless so the profile exists with the CA trusted
# before the first real launch.
init_profile() {
  local base; base=$(profile_dir)
  if profile_in_use; then
    warn "$BROWSER_NAME is running; the launcher trusts the proxy CA when it is next started"
    return
  fi
  log "Initialising the profile and trusting the proxy CA in it"
  "$LAUNCHER" --headless=new --disable-gpu --dump-dom about:blank >/dev/null 2>&1 || true
  if ! python3 -c 'import sqlite3,sys; sys.exit(0 if sqlite3.connect(sys.argv[1]).execute("select count(*) from certificates").fetchone()[0] else 1)' "$base/Default/ServerCertificate" 2>/dev/null; then
    warn "the CA was not written to the profile store; the browser will warn on HTTPS pages (Chromium 134 or newer is needed)"
  fi
}

install_launcher_linux() {
  log "Adding the $BROWSER_NAME launcher"
  local bin="$HOME/.local/bin/$BROWSER_ID"
  write_launcher "$bin"
  local icons="$HOME/.local/share/icons/hicolor"
  local icon="$icons/scalable/apps/$BROWSER_ID.svg"
  mkdir -p "$icons/scalable/apps" "$HOME/.local/share/applications"
  cp "$ICON_SVG" "$icon"
  local size
  for size in 48 128 256; do
    mkdir -p "$icons/${size}x${size}/apps"
    if have rsvg-convert; then
      rsvg-convert -w "$size" -h "$size" "$ICON_SVG" -o "$icons/${size}x${size}/apps/$BROWSER_ID.png"
    elif have convert; then
      convert -background none -density 384 "$ICON_SVG" -resize "${size}x${size}" "$icons/${size}x${size}/apps/$BROWSER_ID.png" 2>/dev/null || true
    fi
  done
  # Ubuntu's GNOME Shell only honours StartupWMClass for a Chromium snap
  # window when the entry runs /snap/bin/chromium itself (its
  # chromium-snap-pwa patch); through the launcher script the window is
  # grouped with Chromium in the dock. The entry then skips the launcher's
  # proxy start and CA re-trust: the container restarts with Docker
  # (unless-stopped) and init_profile has trusted the CA.
  local exec="$bin"
  case $CHROMIUM in
    /snap/*) exec="$CHROMIUM \"--user-data-dir=$(profile_dir)\" --proxy-server=$PROXY_HOST:$PROXY_PORT --class=SissyBrowser --no-first-run --no-default-browser-check" ;;
  esac
  # The icon is given by path: a stale icon-theme.cache in ~/.local/share/icons
  # otherwise hides a freshly added icon name from GNOME.
  cat >"$HOME/.local/share/applications/$BROWSER_ID.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$BROWSER_NAME
Comment=Chromium through the Sissy Censor System proxy
Exec=$exec %U
Icon=$icon
Terminal=false
Categories=Network;WebBrowser;
MimeType=text/html;x-scheme-handler/http;x-scheme-handler/https;
StartupWMClass=SissyBrowser
EOF
  have update-desktop-database && update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
  # -t: no index.theme in the user directory; without it the refresh fails silently.
  have gtk-update-icon-cache && gtk-update-icon-cache -f -t -q "$icons" 2>/dev/null || true
}

install_launcher_macos() {
  log "Adding $BROWSER_NAME to ~/Applications"
  local app="$HOME/Applications/$BROWSER_NAME.app"
  rm -rf "$app"
  mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
  write_launcher "$app/Contents/MacOS/$BROWSER_ID"
  cat >"$app/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$BROWSER_NAME</string>
  <key>CFBundleDisplayName</key><string>$BROWSER_NAME</string>
  <key>CFBundleIdentifier</key><string>local.scs.$BROWSER_ID</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>$BROWSER_ID</string>
  <key>CFBundleIconFile</key><string>icon</string>
</dict>
</plist>
EOF
  # Rasterise the SVG with Quick Look, then build the .icns with iconutil.
  local tmp; tmp=$(mktemp -d)
  local png=""
  if qlmanage -t -s 1024 -o "$tmp" "$ICON_SVG" >/dev/null 2>&1 && [ -f "$tmp/browser-icon.svg.png" ]; then
    png="$tmp/browser-icon.svg.png"
  elif have rsvg-convert; then
    rsvg-convert -w 1024 -h 1024 "$ICON_SVG" -o "$tmp/icon.png" && png="$tmp/icon.png"
  fi
  if [ -n "$png" ]; then
    mkdir -p "$tmp/icon.iconset"
    local size
    for size in 16 32 128 256 512; do
      sips -z "$size" "$size" "$png" --out "$tmp/icon.iconset/icon_${size}x${size}.png" >/dev/null
      sips -z $((size * 2)) $((size * 2)) "$png" --out "$tmp/icon.iconset/icon_${size}x${size}@2x.png" >/dev/null
    done
    iconutil -c icns "$tmp/icon.iconset" -o "$app/Contents/Resources/icon.icns" \
      || warn "could not build the icon; the app will use a generic one"
  else
    warn "could not rasterise $ICON_SVG; the app will use a generic icon"
  fi
  rm -rf "$tmp"
  touch "$app"
}

# Seeds the profile so the bookmark bar is shown with the admin panel on it.
# Chromium reads these files when the profile first opens and fills in the
# ids and checksum itself; an existing profile gets the bookmark inserted.
seed_profile() {
  local base; base=$(profile_dir)
  local dir="$base/Default"
  local bookmarks="$dir/Bookmarks" prefs="$dir/Preferences"
  if profile_in_use; then
    warn "$BROWSER_NAME is running; close it and rerun to add the admin panel bookmark"
    return
  fi
  log "Putting the admin panel on the bookmark bar"
  mkdir -p "$dir"
  if [ ! -s "$bookmarks" ]; then
    cat >"$bookmarks" <<EOF
{
  "roots": {
    "bookmark_bar": {
      "children": [ { "name": "$ADMIN_BOOKMARK", "type": "url", "url": "$ADMIN_URL" } ],
      "name": "Bookmarks bar", "type": "folder"
    },
    "other": { "children": [], "name": "Other bookmarks", "type": "folder" },
    "synced": { "children": [], "name": "Mobile bookmarks", "type": "folder" }
  },
  "version": 1
}
EOF
  elif ! grep -q "$ADMIN_URL" "$bookmarks"; then
    have python3 || { warn "python3 is needed to edit $bookmarks; add $ADMIN_URL by hand"; return; }
    python3 - "$bookmarks" "$ADMIN_BOOKMARK" "$ADMIN_URL" <<'EOF'
import json, sys
path, name, url = sys.argv[1:]
with open(path) as f:
    data = json.load(f)
bar = data.setdefault("roots", {}).setdefault("bookmark_bar", {"name": "Bookmarks bar", "type": "folder"})
bar.setdefault("children", []).insert(0, {"name": name, "type": "url", "url": url})
data.pop("checksum", None)   # Chromium recomputes it
with open(path, "w") as f:
    json.dump(data, f, indent=3)
EOF
  fi
  if [ ! -s "$prefs" ]; then
    printf '{ "bookmark_bar": { "show_on_all_tabs": true } }\n' >"$prefs"
  elif ! grep -q '"show_on_all_tabs": true' "$prefs" && have python3; then
    python3 - "$prefs" <<'EOF'
import json, sys
path = sys.argv[1]
with open(path) as f:
    data = json.load(f)
data.setdefault("bookmark_bar", {})["show_on_all_tabs"] = True
with open(path, "w") as f:
    json.dump(data, f, indent=3)
EOF
  fi
}

uninstall_launcher() {
  log "Removing the $BROWSER_NAME launcher and its profile"
  if [ "$OS" = Linux ]; then
    rm -f "$HOME/.local/bin/$BROWSER_ID" "$HOME/.local/share/applications/$BROWSER_ID.desktop"
    rm -f "$HOME/.local/share/icons/hicolor"/*/apps/"$BROWSER_ID".*
  else
    rm -rf "$HOME/Applications/$BROWSER_NAME.app"
  fi
  rm -rf "$(profile_dir)"
}

# --------------------------------------------------------------- uninstall
# A yes/no question on the terminal (stdin is the script when piped). No
# terminal, or no answer, means no.
ask() {
  local ans=
  if [ -r /dev/tty ] && [ -w /dev/tty ]; then
    printf '\033[1;35m==>\033[0m %s [y/N] ' "$1" >/dev/tty
    read -r ans </dev/tty || true
  fi
  case "$ans" in y|Y|yes|Yes|YES) return 0 ;; *) return 1 ;; esac
}

docker_installed() {
  if [ "$OS" = Linux ]; then have docker && systemctl cat docker.service >/dev/null 2>&1
  else [ -d /Applications/Docker.app ]
  fi
}
chromium_cmd() {
  if [ "$OS" = Darwin ]; then [ -d /Applications/Chromium.app ] && echo /Applications/Chromium.app; return; fi
  for c in chromium chromium-browser; do have "$c" && command -v "$c" && return; done
  return 1
}

# Docker for the uninstall: Engine on Linux (sudo while this user is not in
# the docker group), Docker Desktop on macOS, started if it is not running.
docker_ready() {
  if [ "$OS" = Linux ]; then
    export DOCKER_HOST=unix:///var/run/docker.sock
    docker info >/dev/null 2>&1 && return 0
    DOCKER=(sudo docker)
    sudo docker info >/dev/null 2>&1
    return
  fi
  docker info >/dev/null 2>&1 && return 0
  log "Starting Docker Desktop to remove the proxy"
  open -ga Docker
  local waited=0
  until docker info >/dev/null 2>&1; do
    [ "$waited" -lt 120 ] || return 1
    sleep 5; waited=$((waited + 5))
  done
}

# The channel this install follows, for the image tags to remove.
channel_repo() {
  local channel=$SCS_CHANNEL
  [ ! -f "$SCS_DIR/.env" ] || channel=$(sed -n 's/^SCS_CHANNEL=//p' "$SCS_DIR/.env" | tail -n 1 | tr -d '"')
  echo "${channel:-$SCS_CHANNEL}" | sed 's/:[^:/]*$//'
}

remove_proxy() {
  log "Removing the proxy container and image"
  if ! docker_installed || ! docker_ready; then
    warn "Docker is not available; the proxy container and image were left. With Docker running: docker rm -f \$(docker ps -aq --filter label=com.docker.compose.project=scs); docker image rm -f \$(docker images -q $(channel_repo))"
    return
  fi
  if [ -f "$SCS_DIR/compose.yaml" ]; then
    (cd "$SCS_DIR" && "${DOCKER[@]}" compose down --remove-orphans) || warn "docker compose down failed"
  fi
  local ids
  ids=$("${DOCKER[@]}" images --filter "reference=$(channel_repo)" -q | sort -u)
  # shellcheck disable=SC2086
  [ -z "$ids" ] || "${DOCKER[@]}" image rm -f $ids >/dev/null || warn "some images could not be removed"
}

remove_docker() {
  log "Removing Docker"
  if [ "$OS" = Linux ]; then
    sudo systemctl disable --now docker.service docker.socket containerd.service 2>/dev/null || true
    case "$PKG" in
      apt)
        local pkgs=() p
        for p in docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin docker-ce-rootless-extras docker.io docker-compose-v2; do
          dpkg -s "$p" >/dev/null 2>&1 && pkgs+=("$p")
        done
        [ ${#pkgs[@]} -eq 0 ] || sudo apt-get purge -y "${pkgs[@]}"
        sudo rm -f /etc/apt/sources.list.d/docker.list /etc/apt/keyrings/docker.asc
        ;;
      dnf)
        sudo dnf remove -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
        sudo rm -f /etc/yum.repos.d/docker-ce.repo
        ;;
      pacman) sudo pacman -Rns --noconfirm docker docker-compose docker-buildx ;;
    esac
    sudo rm -rf /var/lib/docker /var/lib/containerd
    sudo gpasswd -d "$USER" docker >/dev/null 2>&1 || true
    return
  fi
  osascript -e 'quit app "Docker"' >/dev/null 2>&1 || true
  local brew; brew=$(brew_bin)
  if [ -n "$brew" ] && "$brew" list --cask docker-desktop >/dev/null 2>&1; then "$brew" uninstall --zap --cask docker-desktop
  elif [ -n "$brew" ] && "$brew" list --cask docker >/dev/null 2>&1; then "$brew" uninstall --zap --cask docker
  else warn "Docker Desktop was not installed by Homebrew; uninstall it from its Troubleshoot menu"
  fi
}

remove_chromium() {
  local c=$1
  log "Removing Chromium"
  if [ "$OS" = Darwin ]; then
    local brew; brew=$(brew_bin)
    if [ -n "$brew" ] && "$brew" list --cask chromium >/dev/null 2>&1; then "$brew" uninstall --cask chromium
    else warn "Chromium was not installed by Homebrew; drag $c to the Trash"
    fi
    return
  fi
  if [ "$c" = /snap/bin/chromium ] || [ "$c" = /snap/bin/chromium-browser ]; then
    sudo snap remove chromium
    return
  fi
  case "$PKG" in
    apt)
      local pkgs=() p
      for p in chromium chromium-browser; do dpkg -s "$p" >/dev/null 2>&1 && pkgs+=("$p"); done
      [ ${#pkgs[@]} -eq 0 ] || sudo apt-get purge -y "${pkgs[@]}"
      ;;
    dnf)    sudo dnf remove -y chromium ;;
    pacman) sudo pacman -Rns --noconfirm chromium ;;
  esac
}

# Only a folder that looks like ours: SCS_DIR can be pointed anywhere.
remove_scs_dir() {
  [ -d "$SCS_DIR" ] || return 0
  if [ ! -f "$SCS_DIR/compose.yaml" ] && [ ! -f "$SCS_DIR/.env" ]; then
    warn "$SCS_DIR has no compose.yaml or .env, so it was left alone"
    return
  fi
  log "Removing $SCS_DIR"
  rm -rf "$SCS_DIR" 2>/dev/null || sudo rm -rf "$SCS_DIR"
}

uninstall() {
  # The questions first, so nothing is started only to be removed.
  local remove_docker=0 remove_chromium=0 chromium
  if docker_installed; then
    ask "Remove Docker too, with every container, image and volume on this machine?" && remove_docker=1
  fi
  if chromium=$(chromium_cmd); then
    ask "Remove Chromium too ($chromium)?" && remove_chromium=1
  fi
  uninstall_launcher
  uninstall_updater
  if [ "$remove_docker" = 1 ]; then remove_docker; else remove_proxy; fi
  [ "$remove_chromium" = 0 ] || remove_chromium "$chromium"
  remove_scs_dir
  log "Done"
}

# -------------------------------------------------------------------- main
if [ "$SCS_UNINSTALL" = 1 ]; then
  uninstall
  exit 0
fi

if [ "$OS" = Linux ]; then install_docker_linux; else install_docker_desktop_macos; fi
start_docker
get_proxy
setup_gpu
start_proxy
install_chromium
[ -f "$ICON_SVG" ] || die "icon missing from the image: $ICON_SVG"
if [ "$OS" = Linux ]; then install_launcher_linux; else install_launcher_macos; fi
seed_profile
init_profile
# scs-update applies an update only while no browser runs on this profile.
env_set SCS_PROFILE "\"$(profile_dir)\""
install_updater

log "Installed"
cat <<EOF
    Launcher:     $BROWSER_NAME (in your app menu$( [ "$OS" = Darwin ] && echo " and ~/Applications"))
    Profile:      $(profile_dir) (bookmark bar: $ADMIN_BOOKMARK)
    Proxy:        $PROXY_HOST:$PROXY_PORT (Docker, restarts with the machine)
    Admin panel:  http://localhost:8082/admin
    Install:      $SCS_DIR (checks $SCS_CHANNEL for updates at each startup; $SCS_DIR/scs-update --now checks at once)
    Only $BROWSER_NAME goes through the proxy; your other browsers are unchanged.
EOF
[ "$GROUP_ADDED" = 0 ] || echo "    Log out and back in so you can run docker without sudo."
