#!/bin/bash
# Setup script for a Claude Code cloud environment (claude.ai/code).
#
# Runs as root on Ubuntu 24.04 before the session starts. Paste this file's contents
# into the environment's setup script. It installs what the tests and Mellon's own
# specification work need beyond the image:
#   - the Qt runtime libraries for the offscreen GUI tests (as CI installs them),
#   - the PDF program the installer bundles, pinned and checksummed (as CI does),
#   - Python 3.12,
#   - the released Mellon, at /opt/mellon-release/bin, for editing Mellon's own documents
#     (docs/specification.md: never with the development build; tools/spec/released.py
#     finds it there).
# The development environment itself is `uv sync --all-groups` in mellon/, which the
# session runs (CLAUDE.md). Safe to run again.
set -euo pipefail

MELLON_REPO="https://github.com/tobias-hallberg/mellon-req-tool"
MELLON_RELEASE="v0.3.0"
TECTONIC_VERSION="0.17.0"
TECTONIC_SHA256="8533d07f9ccbd7a65824b9e0459041bca34af1eb33daba48f59215593753a3b7"

echo "==> Qt runtime libraries"
apt-get update -q
apt-get install -y -q --no-install-recommends \
  libegl1 libgl1 libxkbcommon0 libxkbcommon-x11-0 libxcb-cursor0 libfontconfig1 libdbus-1-3

echo "==> PDF program (tectonic ${TECTONIC_VERSION})"
if ! /usr/local/bin/tectonic --version 2>/dev/null | grep -q "${TECTONIC_VERSION}"; then
  tmp="$(mktemp -d)"
  curl -fsSL -o "${tmp}/tectonic.tar.gz" \
    "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40${TECTONIC_VERSION}/tectonic-${TECTONIC_VERSION}-x86_64-unknown-linux-musl.tar.gz"
  echo "${TECTONIC_SHA256}  ${tmp}/tectonic.tar.gz" | sha256sum -c -
  tar xzf "${tmp}/tectonic.tar.gz" -C /usr/local/bin tectonic
  rm -rf "${tmp}"
fi

echo "==> Python 3.12"
uv python install 3.12

echo "==> Released Mellon (${MELLON_RELEASE})"
if [ ! -x /opt/mellon-release/bin/mellon-mcp ]; then
  UV_TOOL_DIR=/opt/mellon-release/tools UV_TOOL_BIN_DIR=/opt/mellon-release/bin \
    uv tool install --python 3.12 \
    "mellon @ git+${MELLON_REPO}@${MELLON_RELEASE}#subdirectory=mellon"
fi
/opt/mellon-release/bin/mellon-mcp --version
