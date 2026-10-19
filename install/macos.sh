#!/usr/bin/env bash
# Sissy Censor System: system-level installer for macOS.
#
# Installs Homebrew (if missing) and Docker Desktop, clones the repository,
# builds and starts the proxy, trusts its CA in the System keychain and
# points every network service's HTTP and HTTPS proxy at 127.0.0.1:8080.
#
# Run as your normal (admin) user; sudo is asked for where it is needed:
#
#   curl -fsSL https://raw.githubusercontent.com/OWNER/scs/main/install/macos.sh | bash
#
# Undo the trust and proxy changes (keeps Docker Desktop and the checkout):
#
#   curl -fsSL https://raw.githubusercontent.com/OWNER/scs/main/install/macos.sh | SCS_UNINSTALL=1 bash
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
KEYCHAIN=/Library/Keychains/System.keychain
FIREFOX_POLICY="/Applications/Firefox.app/Contents/Resources/distribution/policies.json"
MARKER="scs-proxy"

# ----------------------------------------------------------------- helpers
log()  { printf '\033[1;35m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

[ "$(uname -s)" = Darwin ] || die "this installer is for macOS"
[ "$(id -u)" -ne 0 ] || die "run this as your normal user, not root; sudo is used where needed"

brew_bin() {
  if [ -x /opt/homebrew/bin/brew ]; then echo /opt/homebrew/bin/brew
  elif [ -x /usr/local/bin/brew ]; then echo /usr/local/bin/brew
  else command -v brew 2>/dev/null || true
  fi
}

# Network services to configure: every enabled one (disabled ones are marked '*').
network_services() {
  networksetup -listallnetworkservices | tail -n +2 | grep -v '^\*'
}

# ---------------------------------------------------------- docker desktop
install_homebrew() {
  [ -n "$(brew_bin)" ] && return
  log "Installing Homebrew"
  NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
}

install_docker_desktop() {
  if [ -d /Applications/Docker.app ]; then
    log "Docker Desktop is already installed"
    return
  fi
  log "Installing Docker Desktop"
  local brew; brew=$(brew_bin)
  # The cask was renamed from "docker" to "docker-desktop" in 2025.
  "$brew" install --cask docker-desktop || "$brew" install --cask docker
}

start_docker_desktop() {
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
    log "Installing the Xcode command line tools for git"
    xcode-select --install 2>/dev/null || true
    until have git; do sleep 5; done
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
trust_ca() {
  log "Trusting the proxy CA in the System keychain (Safari, Chrome, Edge, curl)"
  sudo security add-trusted-cert -d -r trustRoot -k "$KEYCHAIN" "$CA_PEM"
  if [ -d /Applications/Firefox.app ]; then
    if [ -e "$FIREFOX_POLICY" ] && ! grep -q "$MARKER" "$FIREFOX_POLICY"; then
      warn "$FIREFOX_POLICY exists; add \"ImportEnterpriseRoots\": true to its policies by hand"
    else
      log "Telling Firefox to use the system trust store"
      sudo mkdir -p "$(dirname "$FIREFOX_POLICY")"
      printf '{\n  "policies": {\n    "Certificates": { "ImportEnterpriseRoots": true }\n  },\n  "_": "%s"\n}\n' "$MARKER" \
        | sudo tee "$FIREFOX_POLICY" >/dev/null
    fi
  fi
}

untrust_ca() {
  log "Removing the proxy CA from the System keychain"
  if [ -s "$CA_PEM" ]; then
    sudo security remove-trusted-cert -d "$CA_PEM" 2>/dev/null || true
    local sha1; sha1=$(openssl x509 -in "$CA_PEM" -noout -fingerprint -sha1 | sed 's/.*=//; s/://g')
    sudo security delete-certificate -Z "$sha1" "$KEYCHAIN" 2>/dev/null || true
  else
    sudo security delete-certificate -c mitmproxy "$KEYCHAIN" 2>/dev/null || true
  fi
  if [ -e "$FIREFOX_POLICY" ] && grep -q "$MARKER" "$FIREFOX_POLICY"; then
    sudo rm -f "$FIREFOX_POLICY"
  fi
}

# ------------------------------------------------------------------- proxy
set_proxy() {
  log "Pointing every network service at $PROXY_HOST:$PROXY_PORT (fails closed while the proxy is stopped)"
  local svc
  while IFS= read -r svc; do
    [ -n "$svc" ] || continue
    sudo networksetup -setwebproxy "$svc" "$PROXY_HOST" "$PROXY_PORT"
    sudo networksetup -setsecurewebproxy "$svc" "$PROXY_HOST" "$PROXY_PORT"
    # The default bypass list has "*.local", which would skip censor.local and scs.local.
    sudo networksetup -setproxybypassdomains "$svc" localhost 127.0.0.1 "::1"
  done < <(network_services)
}

unset_proxy() {
  log "Turning the system proxy off"
  local svc
  while IFS= read -r svc; do
    [ -n "$svc" ] || continue
    sudo networksetup -setwebproxystate "$svc" off
    sudo networksetup -setsecurewebproxystate "$svc" off
    sudo networksetup -setproxybypassdomains "$svc" "*.local" "169.254/16"
  done < <(network_services)
}

# -------------------------------------------------------------------- main
if [ "$SCS_UNINSTALL" = 1 ]; then
  unset_proxy
  untrust_ca
  log "Done. Docker Desktop and $SCS_DIR were left in place; stop the proxy with"
  echo "    (cd $SCS_DIR/src && docker compose down)"
  exit 0
fi

install_homebrew
install_docker_desktop
start_docker_desktop
clone_repo
start_proxy
trust_ca
set_proxy

log "Installed"
cat <<EOF
    Proxy:        $PROXY_HOST:$PROXY_PORT (Docker, restarts with the machine)
    Admin panel:  http://localhost:8082/admin
    Checkout:     $SCS_DIR
    Restart any open browser so it picks up the CA and the proxy.
    Docker Desktop follows the system proxy by default: in Settings > Resources
    > Proxies choose manual with empty fields so image pulls do not go through
    the censor.
EOF
