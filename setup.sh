#!/usr/bin/env bash
set -euo pipefail

git-crypt unlock /mnt/files/server/homelab-crypt.key
chmod 600 ssh/id_ed25519_*
