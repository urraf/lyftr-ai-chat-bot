#!/bin/sh
# Write the runtime config so the same image works against any backend URL.
set -e
cat > /usr/share/nginx/html/config.js <<CFG
window.__APP_CONFIG__ = { API_BASE_URL: "${API_BASE_URL}" };
CFG
echo "runtime config: API_BASE_URL=${API_BASE_URL}"
