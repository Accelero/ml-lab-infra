#!/bin/bash
set -e

# Set environment variables
TAILSCALE_AUTH_KEY="${TAILSCALE_AUTH_KEY}"

# Wait for any automatic system updates to finish
echo "Waiting for apt locks..."
while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; do
   sleep 5
done

# Install base dependencies
apt update -y
apt install -y curl ca-certificates

# Install Tailscale
curl -fsSL https://tailscale.com/install.sh | sh
echo 'net.ipv4.ip_forward = 1' | tee -a /etc/sysctl.d/99-tailscale.conf
echo 'net.ipv6.conf.all.forwarding = 1' | tee -a /etc/sysctl.d/99-tailscale.conf
sysctl -p /etc/sysctl.d/99-tailscale.conf

# Wait for tailscaled to be active before proceeding
systemctl enable --now tailscaled
while ! systemctl is-active --quiet tailscaled; do
  echo "Waiting for tailscaled..."
  sleep 2
done

# Bring up Tailscale
tailscale up \
  --authkey=$TAILSCALE_AUTH_KEY \
  --advertise-tags=tag:hub-server \
  --accept-routes \
  --force-reauth
