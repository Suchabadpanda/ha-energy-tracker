#!/usr/bin/env bash
# init-env.sh — creates .env with your Home Assistant details.
# Usage (from the project folder):  bash scripts/init-env.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -e .env ]]; then
  echo "ERROR: .env already exists. Delete it first if you want to start again."
  exit 1
fi

echo "Home Assistant address — use the IP, e.g. http://192.168.1.50:8123"
read -rp "  HA URL: " HA_URL
HA_URL="${HA_URL%/}"

echo "Long-lived access token (Home Assistant > your profile > Security). Input is hidden."
read -rsp "  HA token: " HA_TOKEN
echo

[[ -n "$HA_URL" && -n "$HA_TOKEN" ]] || { echo "ERROR: both values are required"; exit 1; }

# Check the details work before saving them.
status="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
  -H "Authorization: Bearer ${HA_TOKEN}" "${HA_URL}/api/" || true)"
case "$status" in
  200) echo "  Home Assistant connection OK" ;;
  401) echo "ERROR: Home Assistant rejected the token (HTTP 401)"; exit 1 ;;
  000) echo "ERROR: could not reach ${HA_URL} from this VM"; exit 1 ;;
  *)   echo "ERROR: unexpected response from Home Assistant (HTTP ${status})"; exit 1 ;;
esac

umask 077   # .env is readable only by you
cat > .env <<EOF
HA_URL=${HA_URL}
HA_TOKEN=${HA_TOKEN}
POLL_SECONDS=60
EOF

echo "Created .env. Next: docker compose up -d --build"
