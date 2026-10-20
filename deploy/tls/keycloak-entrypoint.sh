#!/bin/bash
set -euo pipefail
export KC_DB_PASSWORD="$(</run/secrets/db-password)"
export KC_BOOTSTRAP_ADMIN_PASSWORD="$(</run/secrets/admin-password)"
exec /opt/keycloak/bin/kc.sh start --optimized --import-realm "$@"
