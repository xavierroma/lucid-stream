#!/usr/bin/env bash
set -euo pipefail

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  echo "Use: source infra/livekit/use-local-env.sh"
  exit 1
fi

export LIVEKIT_URL="ws://localhost:7880"
export LIVEKIT_API_URL="http://localhost:7880"
export LIVEKIT_API_KEY="devkey"
export LIVEKIT_API_SECRET="devsecretdevsecretdevsecretdevsecret"

echo "LIVEKIT_URL=${LIVEKIT_URL}"
echo "LIVEKIT_API_URL=${LIVEKIT_API_URL}"
echo "LIVEKIT_API_KEY=${LIVEKIT_API_KEY}"
echo "LIVEKIT_API_SECRET=***"
