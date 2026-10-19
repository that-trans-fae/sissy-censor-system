#!/usr/bin/env bash
# Builds the image for amd64 and arm64 (Apple silicon, ARM Linux) and pushes
# it to Docker Hub as VERSION and latest. Installs follow latest
# (SCS_CHANNEL in their .env), so pushing is releasing: every installed
# copy picks it up within a day (see dist/scs-update).
#
#   tools/release.sh 1.2.0
#
# Run from a clean checkout of main, logged in to Docker Hub (docker login).
# Set SCS_IMAGE_REPO to push elsewhere, such as a local registry for a trial
# run (SCS_IMAGE_REPO=localhost:5000/scs), and SCS_PLATFORMS to build fewer
# platforms. The git tag v<VERSION> is created once the push succeeds.
set -euo pipefail

REPO="${SCS_IMAGE_REPO:-thattransfae/scs}"
PLATFORMS="${SCS_PLATFORMS:-linux/amd64,linux/arm64}"
VERSION="${1:-}"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[[ $VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "usage: tools/release.sh MAJOR.MINOR.PATCH"
cd "$(dirname "$0")/.."
[ -z "$(git status --porcelain)" ] || die "the checkout has uncommitted changes; commit them first"
! git rev-parse -q --verify "refs/tags/v$VERSION" >/dev/null || die "v$VERSION is already tagged"

# A multi-platform build needs a builder other than the default one.
docker buildx inspect scs-release >/dev/null 2>&1 \
  || docker buildx create --name scs-release --driver docker-container --driver-opt network=host >/dev/null

docker buildx build --builder scs-release --platform "$PLATFORMS" \
  --build-context brand=../docs/brand \
  --build-arg SCS_VERSION="$VERSION" \
  --label org.opencontainers.image.revision="$(git rev-parse HEAD)" \
  -t "$REPO:$VERSION" -t "$REPO:latest" \
  --push .

git tag -a "v$VERSION" -m "Release $VERSION"
echo "Pushed $REPO:$VERSION and $REPO:latest; tagged v$VERSION."
