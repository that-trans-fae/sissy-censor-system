#!/usr/bin/env bash
# Sissy Censor System: dedicated browser installer for Linux and macOS.
#
# Installs Docker (Docker Engine on Linux, Docker Desktop on macOS), clones
# the repository, builds and starts the proxy, installs Chromium and adds a "Sissy Browser" launcher: a separate
# Chromium profile that always goes through the proxy and trusts its CA in
# the profile's own certificate store. Nothing system-wide changes: no
# system proxy, no CA in the system store, and your usual browsers are
# untouched. Windows: install/browser.ps1.
#
# Run as your normal user (not root); sudo is asked for where it is needed:
#
#   curl -fsSL https://raw.githubusercontent.com/OWNER/scs/main/install/browser.sh | bash
#
# Remove the launcher and its profile (keeps Docker, Chromium and the checkout):
#
#   curl -fsSL https://raw.githubusercontent.com/OWNER/scs/main/install/browser.sh | SCS_UNINSTALL=1 bash
#
# Environment overrides: SCS_REPO_URL, SCS_DIR, SCS_BRANCH.
set -euo pipefail

# ---------------------------------------------------------------- settings
# TODO: replace the placeholder with the real repository URL.
SCS_REPO_URL="${SCS_REPO_URL:-https://github.com/OWNER/scs.git}"
SCS_DIR="${SCS_DIR:-$HOME/scs}"
SCS_BRANCH="${SCS_BRANCH:-main}"
SCS_UNINSTALL="${SCS_UNINSTALL:-0}"

BROWSER_NAME="Sissy Browser"
BROWSER_ID=sissy-browser
PROXY_HOST=127.0.0.1
PROXY_PORT=8080
CA_PEM="$SCS_DIR/src/mitmproxy/mitmproxy-ca-cert.pem"
ICON_SVG="$SCS_DIR/docs/brand/browser-icon.svg"
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

# ---------------------------------------------------------------- the repo
clone_repo() {
  if ! have git; then
    if [ "$OS" = Linux ]; then pkg_install git
    else
      log "Installing the Xcode command line tools for git"
      xcode-select --install 2>/dev/null || true
      until have git; do sleep 5; done
    fi
  fi
  if [ -d "$SCS_DIR/.git" ]; then
    log "Updating $SCS_DIR"
    git -C "$SCS_DIR" pull --ff-only || warn "could not fast-forward $SCS_DIR; continuing with what is there"
  else
    log "Cloning $SCS_REPO_URL into $SCS_DIR"
    case "$SCS_REPO_URL" in *OWNER/*) die "SCS_REPO_URL is still the placeholder; set it to the real repository URL" ;; esac
    git clone --branch "$SCS_BRANCH" "$SCS_REPO_URL" "$SCS_DIR"
  fi
}

start_proxy() {
  log "Building and starting the proxy (the first build downloads a few hundred MB)"
  # The container runs unprivileged and writes to these mounts; Docker Engine
  # would create missing ones as root, where it can't.
  mkdir -p "$SCS_DIR/src/mitmproxy" "$SCS_DIR/src/config" "$SCS_DIR/src/logs"
  # Run the container as this user (user: in src/compose.yaml reads .env).
  printf 'SCS_UID=%s\nSCS_GID=%s\n' "$(id -u)" "$(id -g)" >"$SCS_DIR/src/.env"
  (cd "$SCS_DIR/src" && "${DOCKER[@]}" compose up -d --build)
  local waited=0
  until [ -s "$CA_PEM" ]; do
    [ "$waited" -lt 120 ] || die "the proxy did not write its CA to $CA_PEM"
    sleep 2; waited=$((waited + 2))
  done
  # The container takes a few seconds to load the models after (re)creation.
  waited=0
  until curl -fsS -x "http://$PROXY_HOST:$PROXY_PORT" http://censor.local/health >/dev/null 2>&1; do
    if [ "$waited" -ge 60 ]; then
      warn "health check through the proxy failed; check 'docker compose logs' in $SCS_DIR/src"
      break
    fi
    sleep 3; waited=$((waited + 3))
  done
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
# regenerated CA (after deleting src/mitmproxy/) needs no reinstall.
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
CA="\$SCS_DIR/src/mitmproxy/mitmproxy-ca-cert.pem"
DB="\$PROFILE/Default/ServerCertificate"

# Start the proxy if Docker is up and it is not running yet.
$( [ "$OS" = Linux ] && echo 'export DOCKER_HOST=unix:///var/run/docker.sock' )
if command -v timeout >/dev/null 2>&1; then t="timeout 30"; else t=""; fi
(cd "\$SCS_DIR/src" && \$t docker compose up -d >/dev/null 2>&1) || true

# Trust the proxy CA in this profile's certificate store. Chromium creates
# the store on first start, so a new profile is initialised headless first.
if [ ! -s "\$DB" ]; then
  "\$CHROMIUM" --user-data-dir="\$PROFILE" --headless=new --disable-gpu --no-first-run --dump-dom about:blank >/dev/null 2>&1
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
  --user-data-dir="\$PROFILE" \\
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

# -------------------------------------------------------------------- main
if [ "$SCS_UNINSTALL" = 1 ]; then
  uninstall_launcher
  log "Done. Docker, Chromium and $SCS_DIR were left in place; stop the proxy with"
  echo "    (cd $SCS_DIR/src && docker compose down)"
  exit 0
fi

if [ "$OS" = Linux ]; then install_docker_linux; else install_docker_desktop_macos; fi
start_docker
clone_repo
start_proxy
install_chromium
[ -f "$ICON_SVG" ] || die "icon missing from the checkout: $ICON_SVG"
if [ "$OS" = Linux ]; then install_launcher_linux; else install_launcher_macos; fi
seed_profile
init_profile

log "Installed"
cat <<EOF
    Launcher:     $BROWSER_NAME (in your app menu$( [ "$OS" = Darwin ] && echo " and ~/Applications"))
    Profile:      $(profile_dir) (bookmark bar: $ADMIN_BOOKMARK)
    Proxy:        $PROXY_HOST:$PROXY_PORT (Docker, restarts with the machine)
    Admin panel:  http://localhost:8082/admin
    Checkout:     $SCS_DIR
    Only $BROWSER_NAME goes through the proxy; your other browsers are unchanged.
EOF
[ "$GROUP_ADDED" = 0 ] || echo "    Log out and back in so you can run docker without sudo."
