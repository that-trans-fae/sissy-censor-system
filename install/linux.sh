#!/usr/bin/env bash
# Sissy Censor System: system-level installer for Linux.
#
# Installs Docker Desktop, clones the repository, builds and starts the
# proxy, trusts its CA in the system store and in the browser policy
# directories, and points the desktop's proxy settings at 127.0.0.1:8080.
#
# Run as your normal user (not root); sudo is asked for where it is needed:
#
#   curl -fsSL https://raw.githubusercontent.com/OWNER/scs/main/install/linux.sh | bash
#
# Undo the trust and proxy changes (keeps Docker Desktop and the checkout):
#
#   curl -fsSL https://raw.githubusercontent.com/OWNER/scs/main/install/linux.sh | SCS_UNINSTALL=1 bash
#
# Environment overrides: SCS_REPO_URL, SCS_DIR, SCS_BRANCH.
set -euo pipefail

# ---------------------------------------------------------------- settings
# TODO: replace the placeholder with the real repository URL.
SCS_REPO_URL="${SCS_REPO_URL:-https://github.com/OWNER/scs.git}"
SCS_DIR="${SCS_DIR:-$HOME/scs}"
SCS_BRANCH="${SCS_BRANCH:-main}"
SCS_UNINSTALL="${SCS_UNINSTALL:-0}"

PROXY_HOST=127.0.0.1
PROXY_PORT=8080
CA_PEM="$SCS_DIR/src/mitmproxy/mitmproxy-ca-cert.pem"
DESKTOP_DEB_URL=https://desktop.docker.com/linux/main/amd64/docker-desktop-amd64.deb
DESKTOP_RPM_URL=https://desktop.docker.com/linux/main/amd64/docker-desktop-x86_64.rpm
DESKTOP_ARCH_URL=https://desktop.docker.com/linux/main/amd64/docker-desktop-x86_64.pkg.tar.zst
MARKER="scs-proxy"

# Chromium-family policy directories (snap Chromium, Chromium, Chrome, Edge, Brave).
POLICY_DIRS=(
  /var/snap/chromium/current/policies/managed
  /etc/chromium/policies/managed
  /etc/opt/chrome/policies/managed
  /etc/opt/edge/policies/managed
  /etc/brave/policies/managed
)
FIREFOX_POLICY=/etc/firefox/policies/policies.json

# ----------------------------------------------------------------- helpers
log()  { printf '\033[1;35m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

[ "$(id -u)" -ne 0 ] || die "run this as your normal user, not root; sudo is used where needed"
[ "$(uname -m)" = x86_64 ] || die "Docker Desktop for Linux only ships for x86_64 (this is $(uname -m))"
have sudo || die "sudo is required"

PKG=
if have apt-get; then PKG=apt
elif have dnf; then PKG=dnf
elif have pacman; then PKG=pacman
else die "unsupported distribution: need apt, dnf or pacman"
fi

# CA trust location per distribution family.
case "$PKG" in
  apt)    CA_DST=/usr/local/share/ca-certificates/$MARKER.crt; CA_UPDATE="update-ca-certificates" ;;
  dnf)    CA_DST=/etc/pki/ca-trust/source/anchors/$MARKER.pem; CA_UPDATE="update-ca-trust" ;;
  pacman) CA_DST=/etc/ca-certificates/trust-source/anchors/$MARKER.crt; CA_UPDATE="trust extract-compat" ;;
esac

pkg_install() {
  case "$PKG" in
    apt)    sudo apt-get install -y "$@" ;;
    dnf)    sudo dnf install -y "$@" ;;
    pacman) sudo pacman -S --needed --noconfirm "$@" ;;
  esac
}

# ---------------------------------------------------------- docker desktop
docker_desktop_installed() {
  [ -f /opt/docker-desktop/bin/com.docker.backend ] || systemctl --user list-unit-files docker-desktop.service >/dev/null 2>&1
}

install_docker_desktop() {
  if docker_desktop_installed; then
    log "Docker Desktop is already installed"
    return
  fi
  log "Installing Docker Desktop"
  [ -e /dev/kvm ] || warn "/dev/kvm is missing: Docker Desktop needs KVM (enable virtualisation in the BIOS)"
  local tmp; tmp=$(mktemp -d)
  case "$PKG" in
    apt)
      # Docker's apt repository provides the CLI and compose plugin the .deb depends on.
      pkg_install ca-certificates curl gnupg
      . /etc/os-release
      sudo install -m 0755 -d /etc/apt/keyrings
      sudo curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
      sudo chmod a+r /etc/apt/keyrings/docker.asc
      echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$ID $VERSION_CODENAME stable" \
        | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
      sudo apt-get update
      curl -fL "$DESKTOP_DEB_URL" -o "$tmp/docker-desktop.deb"
      sudo apt-get install -y "$tmp/docker-desktop.deb"
      ;;
    dnf)
      if dnf --version 2>/dev/null | head -1 | grep -q '^dnf5'; then
        sudo dnf config-manager addrepo --overwrite --from-repofile=https://download.docker.com/linux/fedora/docker-ce.repo
      else
        sudo dnf -y install dnf-plugins-core
        sudo dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo
      fi
      curl -fL "$DESKTOP_RPM_URL" -o "$tmp/docker-desktop.rpm"
      sudo dnf install -y "$tmp/docker-desktop.rpm"
      ;;
    pacman)
      pkg_install docker docker-compose
      curl -fL "$DESKTOP_ARCH_URL" -o "$tmp/docker-desktop.pkg.tar.zst"
      sudo pacman -U --noconfirm "$tmp/docker-desktop.pkg.tar.zst"
      ;;
  esac
  rm -rf "$tmp"
}

start_docker_desktop() {
  log "Starting Docker Desktop"
  systemctl --user enable --now docker-desktop >/dev/null 2>&1 || systemctl --user start docker-desktop
  docker context use desktop-linux >/dev/null 2>&1 || true
  local waited=0
  until docker info >/dev/null 2>&1; do
    if [ "$waited" -eq 0 ]; then
      echo "    Waiting for Docker Desktop. On first start, accept the service agreement"
      echo "    in its window (the whale icon in the tray) and leave it running."
    fi
    [ "$waited" -lt 900 ] || die "Docker Desktop did not become ready within 15 minutes"
    sleep 5; waited=$((waited + 5))
  done
}

# ---------------------------------------------------------------- the repo
clone_repo() {
  have git || pkg_install git
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

# ------------------------------------------------------------------- trust
trust_ca_system() {
  log "Trusting the proxy CA in the system store ($CA_DST)"
  sudo install -m 0644 "$CA_PEM" "$CA_DST"
  sudo $CA_UPDATE >/dev/null
}

trust_ca_browsers() {
  log "Writing browser policies for the CA"
  local der; der=$(openssl x509 -in "$CA_PEM" -outform DER | base64 -w0)
  local policy; policy=$(printf '{\n  "CACertificates": ["%s"]\n}\n' "$der")
  local dir
  for dir in "${POLICY_DIRS[@]}"; do
    # Only the snap directory needs the snap to exist; the others are plain /etc paths.
    case "$dir" in /var/snap/*) [ -d /var/snap/chromium ] || continue ;; esac
    sudo mkdir -p "$dir"
    printf '%s' "$policy" | sudo tee "$dir/$MARKER.json" >/dev/null
  done
  if [ -e "$FIREFOX_POLICY" ] && ! grep -q "$MARKER" "$FIREFOX_POLICY"; then
    warn "$FIREFOX_POLICY exists; add {\"Certificates\": {\"Install\": [\"$CA_DST\"]}} to its policies by hand"
  else
    sudo mkdir -p "$(dirname "$FIREFOX_POLICY")"
    printf '{\n  "policies": {\n    "Certificates": { "Install": ["%s"] }\n  }\n}\n' "$CA_DST" \
      | sudo tee "$FIREFOX_POLICY" >/dev/null
    echo "    Firefox reads $FIREFOX_POLICY when installed as a deb/rpm; the snap and"
    echo "    Flatpak may not (check about:policies)."
  fi
}

untrust_ca() {
  log "Removing the proxy CA from the system store and browser policies"
  sudo rm -f "$CA_DST"
  sudo $CA_UPDATE >/dev/null 2>&1 || true
  local dir
  for dir in "${POLICY_DIRS[@]}"; do sudo rm -f "$dir/$MARKER.json"; done
  if [ -e "$FIREFOX_POLICY" ] && grep -q "$MARKER" "$FIREFOX_POLICY"; then
    sudo rm -f "$FIREFOX_POLICY"
  fi
}

# ------------------------------------------------------------------- proxy
set_proxy() {
  if ! have gsettings; then
    warn "gsettings not found: set the HTTP and HTTPS proxy to $PROXY_HOST:$PROXY_PORT in your desktop's network settings"
    return
  fi
  log "Pointing the desktop proxy settings at $PROXY_HOST:$PROXY_PORT (fails closed while the proxy is stopped)"
  gsettings set org.gnome.system.proxy.http host "$PROXY_HOST"
  gsettings set org.gnome.system.proxy.http port "$PROXY_PORT"
  gsettings set org.gnome.system.proxy.https host "$PROXY_HOST"
  gsettings set org.gnome.system.proxy.https port "$PROXY_PORT"
  gsettings set org.gnome.system.proxy ignore-hosts "['localhost', '127.0.0.0/8', '::1']"
  gsettings set org.gnome.system.proxy mode manual
}

unset_proxy() {
  have gsettings || return 0
  log "Turning the desktop proxy off"
  gsettings set org.gnome.system.proxy mode none
}

# -------------------------------------------------------------------- main
if [ "$SCS_UNINSTALL" = 1 ]; then
  unset_proxy
  untrust_ca
  log "Done. Docker Desktop and $SCS_DIR were left in place; stop the proxy with"
  echo "    (cd $SCS_DIR/src && docker compose down)"
  exit 0
fi

install_docker_desktop
start_docker_desktop
clone_repo
start_proxy
trust_ca_system
trust_ca_browsers
set_proxy

log "Installed"
cat <<EOF
    Proxy:        $PROXY_HOST:$PROXY_PORT (Docker, restarts with the machine)
    Admin panel:  http://localhost:8082/admin
    Checkout:     $SCS_DIR
    Restart any open browser so it picks up the CA policy and the proxy.
    If Docker Desktop shows a proxy under Settings > Resources > Proxies, set it to
    manual with empty fields so image pulls do not go through the censor.
EOF
