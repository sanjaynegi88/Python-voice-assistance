#!/bin/sh
# Wait for Postgres, apply migrations, seed the demo data, then hand over to the
# process named in CMD. Safe to re-run: migrations and the seed are idempotent.
set -e

if [ -n "$DATABASE_URL" ]; then
  echo "==> waiting for the database"
  python - <<'PY'
import os
import sys
import time
from urllib.parse import urlparse
import socket

url = urlparse(os.environ["DATABASE_URL"])
host = url.hostname or "localhost"
port = url.port or 5432

for attempt in range(60):
    try:
        with socket.create_connection((host, port), timeout=2):
            print(f"    database reachable at {host}:{port}")
            sys.exit(0)
    except OSError:
        time.sleep(1)

print(f"    gave up waiting for {host}:{port}", file=sys.stderr)
sys.exit(1)
PY
fi

echo "==> applying migrations"
python manage.py migrate --noinput

if [ "${SEED_DEMO_DATA:-true}" = "true" ]; then
  echo "==> seeding demo data"
  python manage.py seed_demo
fi

echo "==> starting: $*"
exec "$@"
