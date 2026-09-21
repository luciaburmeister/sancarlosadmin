#!/bin/bash
set -e

# A 2 GB swap file as headroom on the 4 GB t3.medium. The web app, two
# celery workers, beat and Redis share that RAM; a memory spike (several
# WeasyPrint renders, a large export) then degrades into slowness for a
# few seconds instead of the kernel killing a gunicorn or celery process.
# swappiness is kept low so swap is only touched under real pressure, not
# as a matter of course: it lives on the EBS root volume and is slow.
#
# Idempotent: runs on every deploy, does the work only the first time on a
# fresh instance. Runs before 01_setup_celery.sh (alphabetical order).

SWAPFILE=/swapfile
SWAP_MB=2048

if swapon --show=NAME --noheadings | grep -qx "$SWAPFILE"; then
    echo "Swap already active on $SWAPFILE."
else
    if [ ! -f "$SWAPFILE" ]; then
        echo "Creating ${SWAP_MB} MB swap file..."
        # dd rather than fallocate: a fallocated file can have holes that
        # swapon rejects on some filesystems.
        dd if=/dev/zero of="$SWAPFILE" bs=1M count="$SWAP_MB" status=none
        chmod 600 "$SWAPFILE"
        mkswap "$SWAPFILE"
    fi
    swapon "$SWAPFILE"
    echo "Swap enabled on $SWAPFILE."
fi

if ! grep -q "^$SWAPFILE " /etc/fstab; then
    echo "$SWAPFILE none swap sw 0 0" >> /etc/fstab
fi

cat <<EOF > /etc/sysctl.d/99-swappiness.conf
vm.swappiness = 10
EOF
sysctl -q -p /etc/sysctl.d/99-swappiness.conf

echo "Swap setup complete."
