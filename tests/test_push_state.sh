#!/bin/bash
# Bash driver for the production zsh script. All config/state is synthetic and local.
set -euo pipefail
cd "$(dirname "$0")/.."
test_root=$PWD
test_tmp=$(mktemp -d "$test_root/tests/.run-push-XXXXXX")
trap 'rm -rf "$test_tmp"' EXIT
mkdir -p "$test_tmp/bin" "$test_tmp/home" "$test_tmp/tmp"
export HOME="$test_tmp/home" TMPDIR="$test_tmp/tmp" STATE_DIR="$test_tmp/state"
export STOCKS_CONFIG="$test_tmp/stocks.json" STOCKS_APP="$test_root/stock_watchlist.star"
export TIDBYT_DEVICE=synthetic-device TIDBYT_AUTH="$test_tmp/auth"
export TEST_CALLS="$test_tmp/pixlet.calls" TEST_PUSHES="$test_tmp/curl.calls"
export PIXLET="$test_tmp/bin/pixlet"
export PATH="$test_tmp/bin:$PATH"
printf '%s\n' '{"symbols":"TAA,TAB,TAC","extended":"false"}' > "$STOCKS_CONFIG"
printf '%s\n' 'Authorization: Bearer synthetic' > "$TIDBYT_AUTH"
cat > "$test_tmp/bin/date" <<'FAKE'
#!/bin/sh
printf '%s\n' "${TEST_NOW:-11015}"
FAKE
cat > "$PIXLET" <<'FAKE'
#!/bin/bash
set -euo pipefail
printf '%s\n' "$*" >> "$TEST_CALLS"
page=missing limit=missing out=
while (( $# )); do
  case $1 in
    -o) out=$2; shift ;;
    page_offset=*) page=${1#*=} ;;
    plan_limit=*) limit=${1#*=} ;;
    --silent) echo 'STATE output must not be suppressed' >&2; exit 1 ;;
  esac
  shift
done
[[ $page =~ ^[0-9]+$ && $limit =~ ^[0-9]+$ ]]
if [[ ${TEST_RENDER_FAIL:-0} == 1 ]]; then echo 'synthetic render failure' >&2; exit 1; fi
if [[ ${TEST_RENDER_EMPTY:-0} == 1 ]]; then
  : > "$out"
else
  printf 'synthetic webp' > "$out"
fi
case ${TEST_STATE:-valid} in
  missing) exit 0 ;;
  malformed) echo 'STATE page_offset=oops plan_limit=100000'; exit 0 ;;
  duplicate) echo 'STATE page_offset=9 plan_limit=100000' ;;
  mixed) echo 'STATE page_offset=broken plan_limit=5' ;;
  inline_duplicate) echo 'STATE page_offset=9 plan_limit=100000 STATE page_offset=13 plan_limit=100000'; exit 0 ;;
  oversized) echo 'STATE page_offset=9999999999999999999 plan_limit=100000'; exit 0 ;;
esac
printf 'STATE page_offset=%s plan_limit=100000\n' "$((page + 4))"
FAKE
cat > "$test_tmp/bin/curl" <<'FAKE'
#!/bin/bash
set -euo pipefail
printf '%s\n' "$*" >> "$TEST_PUSHES"
# The old state must still be present when curl is invoked, even on success.
if [[ -n ${TEST_OLD_STATE:-} ]]; then
  cmp "$TEST_OLD_STATE" "$STATE_DIR/render.state"
fi
python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["deviceID"] == "synthetic-device"; assert d["image"]; assert d["background"] is True'
if [[ ${TEST_PUSH_FAIL:-0} == 1 ]]; then exit 22; fi
FAKE
chmod +x "$test_tmp/bin/"*
run_push() { zsh "$test_root/push_tidbyt.sh"; }
assert_state() {
  python3 - "$STATE_DIR/render.state" "$1" "$2" "$STOCKS_CONFIG" <<'PY'
import hashlib, json, os, pathlib, stat, sys
p = pathlib.Path(sys.argv[1])
s = dict(line.split('=', 1) for line in p.read_text().splitlines())
assert s['page_offset'] == sys.argv[2], s
assert s['plan_limit'] == sys.argv[3], s
assert s['symbols_hash'] == hashlib.sha256(json.load(open(sys.argv[4]))['symbols'].encode()).hexdigest(), s
assert s['key_hash'] == hashlib.sha256(json.load(open(sys.argv[4])).get('rapidapi_key', '').strip().encode()).hexdigest(), s
assert 'fetch_cursor' not in s, s
assert stat.S_IMODE(p.stat().st_mode) == 0o600, oct(p.stat().st_mode)
PY
}
run_push
assert_state 4 100000
run_push
assert_state 8 100000
# The second fresh Pixlet process receives the first process's next positions.
[[ $(tail -n 1 "$TEST_CALLS") == *'page_offset=4 plan_limit=100000'* ]]
# Reorder/change symbols: both positions reset before rendering.
printf '%s\n' '{"symbols":"TAC,TAA","extended":"false"}' > "$STOCKS_CONFIG"
run_push
assert_state 4 100000
[[ $(tail -n 1 "$TEST_CALLS") == *'page_offset=0 plan_limit=0'* ]]
# A key change cannot reuse a paid-plan hint from the previous key.
printf '%s\n' '{"symbols":"TAC,TAA","extended":"false","rapidapi_key":"changed-key"}' > "$STOCKS_CONFIG"
run_push
assert_state 4 100000
[[ $(tail -n 1 "$TEST_CALLS") == *'page_offset=0 plan_limit=0'* ]]
# Legacy files retain the single row offset and ignore the old chunk cursor.
python3 - "$STATE_DIR/render.state" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
lines = [s for s in p.read_text().splitlines() if not s.startswith(('key_hash=', 'plan_limit='))]
p.write_text('\n'.join(lines).replace('page_offset=4', 'page_offset=17') + '\nfetch_cursor=12\n')
PY
run_push
assert_state 21 100000
[[ $(tail -n 1 "$TEST_CALLS") == *'page_offset=17 plan_limit=0'* ]]
# State files are parsed as data, never sourced. Malformed input safely resets.
printf '%s\n' 'page_offset=$(touch should-never-exist)' 'fetch_cursor=oops' > "$STATE_DIR/render.state"
run_push
assert_state 4 100000
[[ ! -e should-never-exist ]]
# Malformed integer fields with a matching hash also reset.
python3 - "$STATE_DIR/render.state" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
s = p.read_text().replace('page_offset=4', 'page_offset=-9').replace('plan_limit=100000', 'plan_limit=bogus')
p.write_text(s)
PY
run_push
assert_state 4 100000
# Failed renders preserve state and do not push; bad metadata still pushes successfully.
cp "$STATE_DIR/render.state" "$test_tmp/saved.state"
export TEST_OLD_STATE="$test_tmp/saved.state"
pushes=$(wc -l < "$TEST_PUSHES")
if TEST_RENDER_FAIL=1 run_push > "$test_tmp/failure.log" 2>&1; then exit 1; fi
cmp "$STATE_DIR/render.state" "$test_tmp/saved.state"
[[ $(wc -l < "$TEST_PUSHES") == "$pushes" ]]
# A successful render with STATE but an empty image skips pushing and preserves state.
if ! TEST_RENDER_EMPTY=1 run_push > "$test_tmp/empty-render.log" 2>&1; then
  echo 'FAIL: empty render must exit 0' >&2
  exit 1
fi
cmp "$STATE_DIR/render.state" "$test_tmp/saved.state"
[[ $(wc -l < "$TEST_PUSHES") == "$pushes" ]]
rg -q '^Skip: Pixlet render output is missing or empty; keeping previous state$' "$test_tmp/empty-render.log"
for bad_state in missing malformed duplicate mixed inline_duplicate oversized; do
  TEST_STATE=$bad_state run_push > "$test_tmp/$bad_state.stdout" 2> "$test_tmp/$bad_state.stderr"
  cmp "$STATE_DIR/render.state" "$test_tmp/saved.state"
  pushes=$((pushes + 1))
  [[ $(wc -l < "$TEST_PUSHES") == "$pushes" ]]
  rg -q 'Warning:.*STATE.*keeping previous state' "$test_tmp/$bad_state.stderr"
done
# Failed pushes leave the previous offset so the same page is retried.
if TEST_PUSH_FAIL=1 run_push > "$test_tmp/push-failure.log" 2>&1; then exit 1; fi
cmp "$STATE_DIR/render.state" "$test_tmp/saved.state"
run_push
assert_state 8 100000
unset TEST_OLD_STATE
# Original regular/extended market-window checks remain effective.
calls=$(wc -l < "$TEST_CALLS")
TEST_NOW=61015 run_push  # Saturday
TEST_NOW=10949 run_push  # before regular window
TEST_NOW=11630 run_push  # at regular close
[[ $(wc -l < "$TEST_CALLS") == "$calls" ]]
printf '%s\n' '{"symbols":"TAC,TAA","extended":"true","rapidapi_key":"changed-key"}' > "$STOCKS_CONFIG"
TEST_NOW=10415 run_push
assert_state 12 100000
TEST_NOW=12015 run_push
assert_state 12 100000
[[ ! -e "$HOME/.stockwatchlist" ]]
printf '%s\n' 'PASS: push state continuity, legacy/key/list resets, mode 600, empty render skip, bad STATE pushes, failed push preservation, and market windows'
