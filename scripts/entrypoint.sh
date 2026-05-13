#!/bin/sh
# Container entrypoint:
#   - if GOOGLE_APPLICATION_CREDENTIALS_JSON is set (preferred for cloud
#     hosts), decode it into a temp file and point ADC at it
#   - then exec whatever command was passed
set -eu

if [ -n "${GOOGLE_APPLICATION_CREDENTIALS_JSON:-}" ]; then
  CREDS_FILE="/tmp/gcp-service-account.json"
  printf '%s' "$GOOGLE_APPLICATION_CREDENTIALS_JSON" > "$CREDS_FILE"
  chmod 600 "$CREDS_FILE"
  export GOOGLE_APPLICATION_CREDENTIALS="$CREDS_FILE"
  echo "[entrypoint] Wrote service account to $CREDS_FILE ($(wc -c <"$CREDS_FILE") bytes)"
fi

exec "$@"
