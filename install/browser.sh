#!/usr/bin/env bash
# Sissy Censor System: dedicated browser installer for Linux and macOS.
#
# Installs Docker Desktop, clones the repository, builds and starts the
# proxy, installs Chromium and adds a "Sissy Browser" launcher: a separate
# Chromium profile that always goes through the proxy and trusts only its
# CA. Nothing system-wide changes: no system proxy, no CA in the system
# store, and your usual browsers are untouched. Windows: install/browser.ps1.
#
# Run as your normal user (not root); sudo is asked for where it is needed:
#
#   curl -fsSL https://raw.githubusercontent.com/OWNER/scs/main/install/browser.sh | bash
#
# Remove the launcher and its profile (keeps Docker Desktop, Chromium and the checkout):
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

# ----------------------------------------------------------------- helpers
log()  { printf '\033[1;35m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

[ "$(id -u)" -ne 0 ] || die "run this as your normal user, not root; sudo is used where needed"
case "$OS" in Linux|Darwin) ;; *) die "this installer is for Linux and macOS" ;; esac

PKG=
if [ "$OS" = Linux ]; then
  [ "$(uname -m)" = x86_64 ] || die "Docker Desktop for Linux only ships for x86_64 (this is $(uname -m))"
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

# ---------------------------------------------------------- docker desktop
install_docker_desktop_linux() {
  if [ -f /opt/docker-desktop/bin/com.docker.backend ]; then
    log "Docker Desktop is already installed"
    return
  fi
  log "Installing Docker Desktop"
  [ -e /dev/kvm ] || warn "/dev/kvm is missing: Docker Desktop needs KVM (enable virtualisation in the BIOS)"
  local tmp; tmp=$(mktemp -d)
  case "$PKG" in
    apt)
      pkg_install ca-certificates curl gnupg
      . /etc/os-release
      sudo install -m 0755 -d /etc/apt/keyrings
      sudo curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
      sudo chmod a+r /etc/apt/keyrings/docker.asc
      echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$ID $VERSION_CODENAME stable" \
        | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
      sudo apt-get update
      curl -fL https://desktop.docker.com/linux/main/amd64/docker-desktop-amd64.deb -o "$tmp/docker-desktop.deb"
      sudo apt-get install -y "$tmp/docker-desktop.deb"
      ;;
    dnf)
      if dnf --version 2>/dev/null | head -1 | grep -q '^dnf5'; then
        sudo dnf config-manager addrepo --overwrite --from-repofile=https://download.docker.com/linux/fedora/docker-ce.repo
      else
        sudo dnf -y install dnf-plugins-core
        sudo dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo
      fi
      curl -fL https://desktop.docker.com/linux/main/amd64/docker-desktop-x86_64.rpm -o "$tmp/docker-desktop.rpm"
      sudo dnf install -y "$tmp/docker-desktop.rpm"
      ;;
    pacman)
      pkg_install docker docker-compose
      curl -fL https://desktop.docker.com/linux/main/amd64/docker-desktop-x86_64.pkg.tar.zst -o "$tmp/docker-desktop.pkg.tar.zst"
      sudo pacman -U --noconfirm "$tmp/docker-desktop.pkg.tar.zst"
      ;;
  esac
  rm -rf "$tmp"
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

start_docker_desktop() {
  log "Starting Docker Desktop"
  if [ "$OS" = Linux ]; then
    systemctl --user enable --now docker-desktop >/dev/null 2>&1 || systemctl --user start docker-desktop
    docker context use desktop-linux >/dev/null 2>&1 || true
  else
    open -ga Docker
  fi
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
  (cd "$SCS_DIR/src" && docker compose up -d --build)
  local waited=0
  until [ -s "$CA_PEM" ]; do
    [ "$waited" -lt 120 ] || die "the proxy did not write its CA to $CA_PEM"
    sleep 2; waited=$((waited + 2))
  done
  curl -fsS -x "http://$PROXY_HOST:$PROXY_PORT" http://censor.local/health >/dev/null \
    || warn "health check through the proxy failed; check 'docker compose logs' in $SCS_DIR/src"
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

# The profile directory. Snap Chromium can only reach its own home area.
profile_dir() {
  if [ "$OS" = Linux ] && [ -d "$HOME/snap/chromium" ]; then echo "$HOME/snap/chromium/common/$BROWSER_ID"
  elif [ "$OS" = Darwin ]; then echo "$HOME/Library/Application Support/$BROWSER_ID"
  else echo "$HOME/.config/$BROWSER_ID"
  fi
}

# The launcher. The CA's SPKI hash is computed at every launch, so a
# regenerated CA (after deleting src/mitmproxy/) needs no reinstall.
# mitmproxy signs each site certificate with the CA's own key pair, which is
# why the CA's SPKI matches the certificates Chromium sees.
write_launcher() {
  local path=$1
  mkdir -p "$(dirname "$path")"
  cat >"$path" <<EOF
#!/usr/bin/env bash
# $BROWSER_NAME: Chromium pinned to the Sissy Censor System proxy.
SCS_DIR="$SCS_DIR"
PROFILE="$(profile_dir)"
CA="\$SCS_DIR/src/mitmproxy/mitmproxy-ca-cert.pem"
# Start the proxy if Docker is up and it is not running yet.
(cd "\$SCS_DIR/src" && timeout 30 docker compose up -d >/dev/null 2>&1) || true
spki=\$(openssl x509 -in "\$CA" -pubkey -noout 2>/dev/null | openssl pkey -pubin -outform der 2>/dev/null | openssl dgst -sha256 -binary | base64)
exec "$CHROMIUM" \\
  --user-data-dir="\$PROFILE" \\
  --proxy-server="$PROXY_HOST:$PROXY_PORT" \\
  --ignore-certificate-errors-spki-list="\$spki" \\
  --class=SissyBrowser \\
  --no-first-run --no-default-browser-check \\
  "\$@"
EOF
  chmod +x "$path"
}

install_launcher_linux() {
  log "Adding the $BROWSER_NAME launcher"
  local bin="$HOME/.local/bin/$BROWSER_ID"
  write_launcher "$bin"
  local icons="$HOME/.local/share/icons/hicolor"
  mkdir -p "$icons/scalable/apps" "$HOME/.local/share/applications"
  cp "$ICON_SVG" "$icons/scalable/apps/$BROWSER_ID.svg"
  if have rsvg-convert; then
    local size
    for size in 48 128 256; do
      mkdir -p "$icons/${size}x${size}/apps"
      rsvg-convert -w "$size" -h "$size" "$ICON_SVG" -o "$icons/${size}x${size}/apps/$BROWSER_ID.png"
    done
  fi
  cat >"$HOME/.local/share/applications/$BROWSER_ID.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$BROWSER_NAME
Comment=Chromium through the Sissy Censor System proxy
Exec=$bin %U
Icon=$BROWSER_ID
Terminal=false
Categories=Network;WebBrowser;
MimeType=text/html;x-scheme-handler/http;x-scheme-handler/https;
StartupWMClass=SissyBrowser
EOF
  have update-desktop-database && update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
  have gtk-update-icon-cache && gtk-update-icon-cache -q "$icons" 2>/dev/null || true
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
  if [ -e "$base/SingletonLock" ] || [ -e "$base/SingletonSocket" ]; then
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
  log "Done. Docker Desktop, Chromium and $SCS_DIR were left in place; stop the proxy with"
  echo "    (cd $SCS_DIR/src && docker compose down)"
  exit 0
fi

if [ "$OS" = Linux ]; then install_docker_desktop_linux; else install_docker_desktop_macos; fi
start_docker_desktop
clone_repo
start_proxy
install_chromium
[ -f "$ICON_SVG" ] || die "icon missing from the checkout: $ICON_SVG"
if [ "$OS" = Linux ]; then install_launcher_linux; else install_launcher_macos; fi
seed_profile

log "Installed"
cat <<EOF
    Launcher:     $BROWSER_NAME (in your app menu$( [ "$OS" = Darwin ] && echo " and ~/Applications"))
    Profile:      $(profile_dir) (bookmark bar: $ADMIN_BOOKMARK)
    Proxy:        $PROXY_HOST:$PROXY_PORT (Docker, restarts with the machine)
    Admin panel:  http://localhost:8082/admin
    Checkout:     $SCS_DIR
    Only $BROWSER_NAME goes through the proxy; your other browsers are unchanged.
EOF
