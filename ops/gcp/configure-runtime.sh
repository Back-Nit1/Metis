#!/usr/bin/env bash
# Runs on the VM as root after vm-setup.sh has written the base units.
# One warm decoder, one jobs worker, one small timer worker; 2 GiB disk overflow.
set -euo pipefail
APP="${APP:-/opt/metis}"
SWAP_FILE=/var/swap/metis.swap
SWAP_MIB=2048

if ! swapon --show=NAME --noheadings | grep -Fxq "$SWAP_FILE"; then
  if [ ! -e "$SWAP_FILE" ]; then
    # Leave at least 1 GiB free; swap must not fill the data/SQLite disk.
    free_kib="$(df --output=avail /var | tail -n 1 | tr -d ' ')"
    if (( free_kib < (SWAP_MIB + 1024) * 1024 )); then
      echo 'not enough free space for 2 GiB swap plus 1 GiB disk headroom' >&2
      exit 1
    fi
    install -d -m 0700 /var/swap
    umask 077
    dd if=/dev/zero of="$SWAP_FILE" bs=1M count="$SWAP_MIB" status=none
    chmod 0600 "$SWAP_FILE"
    mkswap "$SWAP_FILE" >/dev/null
  elif [ "$(blkid -p -s TYPE -o value "$SWAP_FILE")" != swap ]; then
    echo 'existing metis swap path is not swap; refusing to overwrite it' >&2
    exit 1
  fi
  chmod 0600 "$SWAP_FILE"
  swapon "$SWAP_FILE"
fi
if ! awk -v path="$SWAP_FILE" '$1 == path && $3 == "swap" {found=1} END {exit !found}' /etc/fstab; then
  printf '%s none swap sw 0 0\n' "$SWAP_FILE" >>/etc/fstab
fi
printf 'vm.swappiness=60\n' >/etc/sysctl.d/60-metis-swap.conf
sysctl -q -p /etc/sysctl.d/60-metis-swap.conf

for service in api worker speech timers web; do
  install -D -m 0644 "$APP/ops/gcp/systemd/metis-runtime-$service.conf" \
    "/etc/systemd/system/metis-$service.service.d/runtime.conf"
done
install -D -m 0644 "$APP/ops/gcp/systemd/metis-runtime-caddy.conf" \
  /etc/systemd/system/caddy.service.d/metis-runtime.conf
# vm-setup owns creation of the timers base unit, same sandbox/env as worker.
systemctl daemon-reload
