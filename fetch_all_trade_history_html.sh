#!/usr/bin/env bash
set -euo pipefail

BASE_URL="https://steamcommunity.com/id/nigol_/tradehistory/"
OUT_DIR="./steam-trade-html"
STATE_FILE="$OUT_DIR/state.env"
LOG_FILE="$OUT_DIR/fetch.log"

# Normal delay between requests.
SLEEP_MIN=8
SLEEP_MAX=15

# Longer cooldown every N successful pages.
LONG_BREAK_EVERY=10
LONG_BREAK_MIN=60
LONG_BREAK_MAX=120

# Backoff when Steam rate-limits.
RATE_LIMIT_SLEEP_1=300    # 5 min
RATE_LIMIT_SLEEP_2=900    # 15 min
RATE_LIMIT_SLEEP_3=1800   # 30 min
MAX_RATE_LIMIT_RETRIES=6

mkdir -p "$OUT_DIR"

: "${STEAM_COOKIE_STEAMLOGINSECURE:?Missing STEAM_COOKIE_STEAMLOGINSECURE}"
: "${STEAM_COOKIE_SESSIONID:?Missing STEAM_COOKIE_SESSIONID}"

STEAM_COOKIE_HEADER="steamLoginSecure=${STEAM_COOKIE_STEAMLOGINSECURE}; sessionid=${STEAM_COOKIE_SESSIONID}; Steam_Language=english"

if [[ -z "${STEAM_USER_AGENT:-}" ]]; then
  STEAM_USER_AGENT='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
fi

log() {
  local msg
  msg="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
  echo "$msg" | tee -a "$LOG_FILE"
}

rand_sleep() {
  awk -v min="$1" -v max="$2" 'BEGIN{srand(); print min + rand() * (max - min)}'
}

save_state() {
  local page="$1"
  local url="$2"
  local done_flag="${3:-0}"

  cat > "$STATE_FILE" <<EOF
CURRENT_PAGE=$page
NEXT_URL='$url'
DONE=$done_flag
EOF
}

load_state() {
  if [[ -f "$STATE_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$STATE_FILE"
  else
    CURRENT_PAGE=1
    NEXT_URL="$BASE_URL"
    DONE=0
  fi
}

extract_next_url() {
  local file="$1"

  perl -0777 -ne '
    while (/<a[^>]*class="pagebtn"[^>]*href="([^"]+)"[^>]*>(.*?)<\/a>/sg) {
      my $u = $1;
      my $label = $2;

      next unless $u =~ /after_time=\d+/ && $u =~ /after_trade=\d+/;
      next if $u =~ /(?:^|[?&])prev=1(?:&|$)/;

      if ($label =~ /&gt;|>/) {
        print "$u\n";
        last;
      }
    }
  ' "$file" | sed 's/&amp;/\&/g'
}

is_auth_failure() {
  local file="$1"

  if grep -qE 'g_steamID|g_sessionID|wallet_balance|application_config' "$file"; then
    return 1
  fi

  grep -Eiq \
    'openid/login|Sign in through Steam|<input[^>]+name="password"|<input[^>]+name="username"|/login/home/' \
    "$file"
}

is_rate_limited() {
  local file="$1"
  grep -q "You've made too many requests recently" "$file"
}

build_next_url() {
  local next_rel="$1"

  if [[ "$next_rel" == http* ]]; then
    printf '%s\n' "$next_rel"
  elif [[ "$next_rel" == \?* ]]; then
    printf '%s%s\n' "$BASE_URL" "$next_rel"
  else
    printf 'https://steamcommunity.com%s\n' "$next_rel"
  fi
}

rate_limit_sleep_for_attempt() {
  local attempt="$1"

  if (( attempt == 1 )); then
    echo "$RATE_LIMIT_SLEEP_1"
  elif (( attempt == 2 )); then
    echo "$RATE_LIMIT_SLEEP_2"
  else
    echo "$RATE_LIMIT_SLEEP_3"
  fi
}

fetch_one_page() {
  local url="$1"
  local tmpfile="$2"

  curl -sS \
    "$url" \
    -H "Cookie: ${STEAM_COOKIE_HEADER}" \
    -H "User-Agent: ${STEAM_USER_AGENT}" \
    -H "Accept-Language: en-US,en;q=0.9" \
    -o "$tmpfile"
}

main() {
  load_state

  if [[ "${DONE:-0}" == "1" ]]; then
    log "Already marked as complete in $STATE_FILE"
    exit 0
  fi

  log "Starting/resuming from page $CURRENT_PAGE"
  log "Next URL: $NEXT_URL"

  while :; do
    local outfile tmpfile next_rel next_url sleep_for rate_limit_attempt retry_sleep
    outfile="$OUT_DIR/page-${CURRENT_PAGE}.html"
    tmpfile="$OUT_DIR/.page-${CURRENT_PAGE}.html.tmp"

    if [[ -f "$outfile" && -s "$outfile" ]]; then
      log "Page $CURRENT_PAGE already exists, reusing: $outfile"
    else
      rate_limit_attempt=0

      while :; do
        log "Fetching page $CURRENT_PAGE: $NEXT_URL"
        rm -f "$tmpfile"

        fetch_one_page "$NEXT_URL" "$tmpfile"

        if [[ ! -s "$tmpfile" ]]; then
          log "ERROR: empty response for page $CURRENT_PAGE"
          rm -f "$tmpfile"
          exit 1
        fi

        mv "$tmpfile" "$outfile"
        log "Saved $outfile"

        if is_auth_failure "$outfile"; then
          log "ERROR: looks like auth failed on page $CURRENT_PAGE"
          exit 1
        fi

        if is_rate_limited "$outfile"; then
          rate_limit_attempt=$((rate_limit_attempt + 1))

          if (( rate_limit_attempt > MAX_RATE_LIMIT_RETRIES )); then
            log "ERROR: hit rate limit too many times on page $CURRENT_PAGE, giving up"
            exit 2
          fi

          retry_sleep="$(rate_limit_sleep_for_attempt "$rate_limit_attempt")"
          log "Rate limited on page $CURRENT_PAGE, retry ${rate_limit_attempt}/${MAX_RATE_LIMIT_RETRIES}, sleeping ${retry_sleep}s"

          rm -f "$outfile"
          sleep "$retry_sleep"
          continue
        fi

        break
      done
    fi

    next_rel="$(extract_next_url "$outfile" || true)"

    if [[ -z "$next_rel" ]]; then
      log "No next older page found. Finished."
      save_state "$CURRENT_PAGE" "$NEXT_URL" 1
      exit 0
    fi

    next_url="$(build_next_url "$next_rel")"

    CURRENT_PAGE=$((CURRENT_PAGE + 1))
    NEXT_URL="$next_url"
    save_state "$CURRENT_PAGE" "$NEXT_URL" 0

    if (( (CURRENT_PAGE - 1) % LONG_BREAK_EVERY == 0 )); then
      sleep_for="$(rand_sleep "$LONG_BREAK_MIN" "$LONG_BREAK_MAX")"
      log "Taking longer cooldown: ${sleep_for}s"
    else
      sleep_for="$(rand_sleep "$SLEEP_MIN" "$SLEEP_MAX")"
      log "Sleeping ${sleep_for}s before next request"
    fi

    sleep "$sleep_for"
  done
}

main "$@"
