#!/usr/bin/env bash
# Install Lean toolchains with elan before anything uses them, retrying failed downloads (SCI-1717).
#
#   install_toolchain.sh TOOLCHAIN [TOOLCHAIN ...]
#
# Without this, the first `lake` or `lean` call downloads the toolchain on first use, and a failed
# download surfaces later as an unrelated error (elan's "could not canonicalize path" warning in
# `shfv doctor`). Here each toolchain gets SHFV_INSTALL_ATTEMPTS tries (default 3), waiting
# SHFV_INSTALL_DELAY seconds (default 15) after the first failure and doubling after each next one.
# elan's output goes to the log as it is; the script exits non-zero if any toolchain is still missing.
set -u

attempts="${SHFV_INSTALL_ATTEMPTS:-3}"
delay0="${SHFV_INSTALL_DELAY:-15}"
[ "$#" -gt 0 ] || { echo "usage: $0 TOOLCHAIN [TOOLCHAIN ...]" >&2; exit 2; }

status=0
for tc in "$@"; do
  delay="$delay0"
  for ((i = 1; i <= attempts; i++)); do
    echo "Installing $tc (attempt $i of $attempts)"
    if elan toolchain install "$tc"; then
      continue 2
    fi
    if [ "$i" -lt "$attempts" ]; then
      echo "elan toolchain install $tc failed; retrying in ${delay}s"
      sleep "$delay"
      delay=$((delay * 2))
    fi
  done
  echo "::error::could not install the Lean toolchain $tc after $attempts attempts (elan's output is above)"
  status=1
done
exit "$status"
