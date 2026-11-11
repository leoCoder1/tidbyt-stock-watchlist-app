#!/bin/zsh
# Stock-firmware Tidbyt (not flashed to Tronbyt): render with tronbyt pixlet and push through the Tidbyt cloud API.
# Whether api.tidbyt.com still accepts pushes is UNVERIFIED. Run it from launchd: at the BASIC slot times
# (09:50 11:50 13:50 15:50 16:25 ET) on a free key, or every 10 minutes on a paid one. The CLI has no persistent
# cache (each run is a fresh call), so the window check lives here: 09:50-16:30 ET, or 04:15-20:15 when the config
# sets "extended": "true".
#   TIDBYT_DEVICE=<device id> TIDBYT_AUTH=~/.config/tidbyt/auth STOCKS_CONFIG=~/.config/tidbyt/stocks.json push_tidbyt.sh
# TIDBYT_AUTH is a mode-600 file holding one line, "Authorization: Bearer <token>". STOCKS_CONFIG is a mode-600
# JSON config ({"rapidapi_key": ..., "symbols": ...}); keep both out of git. launchd's PATH is minimal, so set
# PIXLET to pixlet's absolute path if it is not in one of the directories added below. Each run is a fresh process
# (no persistent cache); export PIXLET_REDIS_URL to give pixlet one.
set -eu
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
now=$(TZ=America/New_York date +%u%H%M)  # weekday 1-7, then HHMM
day=${now:0:1} hm=${now:1}
ext=$(python3 -c 'import json,sys; print(str(json.load(open(sys.argv[1])).get("extended","")).lower())' "$STOCKS_CONFIG")
lo=950 hi=1630
if [[ $ext == true ]]; then lo=415 hi=2015; fi
if (( day > 5 || 10#$hm < lo || 10#$hm >= hi )); then exit 0; fi

app=${STOCKS_APP:-${0:A:h}/../apps/stockwatchlist/stock_watchlist.star}  # the owner points this at local/
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
out=$tmp/out.webp
state_dir=${STATE_DIR:-$HOME/.stockwatchlist/state}
mkdir -p "$state_dir"
state_file=$state_dir/render.state
read -r symbols_hash key_hash <<< "$(python3 -c 'import hashlib,json,sys; c=json.load(open(sys.argv[1])); print(hashlib.sha256(str(c.get("symbols", "")).encode()).hexdigest(), hashlib.sha256(str(c.get("rapidapi_key", "")).strip().encode()).hexdigest())' "$STOCKS_CONFIG")"
read -r page_offset plan_limit <<< "$(python3 - "$state_file" "$symbols_hash" "$key_hash" <<'PY'
import pathlib, re, sys
try:
    state = dict(line.split("=", 1) for line in pathlib.Path(sys.argv[1]).read_text().splitlines())
except (OSError, ValueError, UnicodeError):
    state = {}
if state.get("symbols_hash") != sys.argv[2]:
    state = {}
if state.get("key_hash", sys.argv[3]) != sys.argv[3]:
    state = {}
# Old files contain page_offset + fetch_cursor, a chunk index that is no longer used.
# Keep their page offset, but probe the plan again before trusting a nonzero offset.
if "key_hash" not in state:
    state.pop("plan_limit", None)
print(*(state.get(k, "0") if re.fullmatch(r"[0-9]{1,18}", state.get(k, "")) else "0"
        for k in ("page_offset", "plan_limit")))
PY
)"
if ! "${PIXLET:-pixlet}" render "$app" -c "$STOCKS_CONFIG" -o "$out" \
    "page_offset=$page_offset" "plan_limit=$plan_limit" > "$tmp/render.log" 2>&1; then
  cat "$tmp/render.log" >&2
  exit 1
fi
if [[ ! -s $out ]]; then
  printf '%s\n' 'Skip: Pixlet render output is missing or empty; keeping previous state' >&2
  exit 0
fi
# Stage validated output locally; neither invalid metadata nor a failed push may advance state.
if ! python3 - "$tmp/render.log" "$tmp/next.state" "$symbols_hash" "$key_hash" <<'PY'
import pathlib, re, sys
lines = pathlib.Path(sys.argv[1]).read_text().splitlines()
states = [line for line in lines for _ in re.finditer(r"(?<!\S)STATE\b", line)]
match = re.search(r"(?:^|\s)STATE page_offset=([0-9]{1,18}) plan_limit=([0-9]{1,18})$", states[0]) if len(states) == 1 else None
if not match:
    print("Warning: missing, malformed or duplicate Pixlet STATE; keeping previous state and pushing image", file=sys.stderr)
    sys.exit(0)
page, limit = match.groups()
pathlib.Path(sys.argv[2]).write_text(f"symbols_hash={sys.argv[3]}\nkey_hash={sys.argv[4]}\npage_offset={page}\nplan_limit={limit}\n")
PY
then
  rm -f "$tmp/next.state"
  printf '%s\n' 'Warning: could not parse Pixlet STATE; keeping previous state and pushing image' >&2
fi
printf '{"deviceID":"%s","image":"%s","installationID":"stockwatchlist","background":true}' \
  "$TIDBYT_DEVICE" "$(base64 < "$out" | tr -d '\n')" |
  curl -fsS --max-time 30 -X POST "https://api.tidbyt.com/v0/devices/$TIDBYT_DEVICE/push" \
    -H @"$TIDBYT_AUTH" -H 'Content-Type: application/json' --data-binary @-
if [[ -f $tmp/next.state ]]; then
  if ! python3 - "$tmp/next.state" "$state_file" <<'PY'
import os, pathlib, sys, tempfile
target = pathlib.Path(sys.argv[2])
fd, name = tempfile.mkstemp(prefix=".render.state-", dir=target.parent)
try:
    with os.fdopen(fd, "w") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(pathlib.Path(sys.argv[1]).read_text())
    os.replace(name, target)
finally:
    if os.path.exists(name):
        os.unlink(name)
PY
  then
    printf '%s\n' 'Warning: push succeeded but state could not be saved; keeping previous state' >&2
  fi
fi
