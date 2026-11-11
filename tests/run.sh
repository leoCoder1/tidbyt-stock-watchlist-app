#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
# Keep both the offline and optional real-Pixlet suites independent of user caches.
export PYTHONDONTWRITEBYTECODE=1
unset PIXLET_REDIS_URL
python3 tests/test_logic.py
bash tests/test_push_state.sh
exec python3 tests/test_watchlist.py
