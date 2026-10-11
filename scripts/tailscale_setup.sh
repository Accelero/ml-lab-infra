#!/usr/bin/env bash
# Requires TAILSCALE_AUTH_KEY from the SkyPilot admin policy.
set -euo pipefail

# The proxy is unavailable until tailscaled starts.
unset ALL_PROXY HTTP_PROXY http_proxy HTTPS_PROXY https_proxy

if ! command -v tailscale &>/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi

# Containers without systemd use userspace networking.
if [ ! -S /var/run/tailscale/tailscaled.sock ]; then
  setsid tailscaled \
    --tun=userspace-networking \
    --socks5-server=localhost:1055 \
    --outbound-http-proxy-listen=localhost:1055 \
    >/tmp/tailscaled.log 2>&1 &
  disown

  # Cover login, interactive, and non-interactive shells.
  cat >/etc/profile.d/tailscale-proxy.sh <<'EOF'
export ALL_PROXY=socks5h://localhost:1055/
export HTTP_PROXY=http://localhost:1055/
export http_proxy=http://localhost:1055/
export HTTPS_PROXY=http://localhost:1055/
export https_proxy=http://localhost:1055/
EOF
  chmod 0644 /etc/profile.d/tailscale-proxy.sh
  cat >>/etc/environment <<'EOF'
ALL_PROXY=socks5h://localhost:1055/
HTTP_PROXY=http://localhost:1055/
http_proxy=http://localhost:1055/
HTTPS_PROXY=http://localhost:1055/
https_proxy=http://localhost:1055/
BASH_ENV=/etc/profile.d/tailscale-proxy.sh
EOF
  grep -qxF 'source /etc/profile.d/tailscale-proxy.sh' /etc/bash.bashrc \
    || echo 'source /etc/profile.d/tailscale-proxy.sh' >>/etc/bash.bashrc
  export BASH_ENV=/etc/profile.d/tailscale-proxy.sh
  export ALL_PROXY=socks5h://localhost:1055/
  export HTTP_PROXY=http://localhost:1055/
  export http_proxy=http://localhost:1055/
  export HTTPS_PROXY=http://localhost:1055/
  export https_proxy=http://localhost:1055/
fi

_ts_deadline=$(( SECONDS + 30 ))
until [ -S /var/run/tailscale/tailscaled.sock ]; do
  sleep 1
  if (( SECONDS > _ts_deadline )); then
    cat /tmp/tailscaled.log >&2
    exit 1
  fi
done

tailscale up \
  --authkey="${TAILSCALE_AUTH_KEY}" \
  --hostname="skypilot-$(hostname | cut -c1-12)" \
  --accept-routes

tailscale status >&2

