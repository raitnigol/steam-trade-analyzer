page=1
start_after_time=""
start_after_tradeid=""

while :; do
  if [ -n "$start_after_time" ] && [ -n "$start_after_tradeid" ]; then
    curl -s "https://api.steampowered.com/IEconService/GetTradeHistory/v1/" \
      --get \
      --data-urlencode "key=${STEAM_API_KEY}" \
      --data-urlencode "max_trades=100" \
      --data-urlencode "get_descriptions=1" \
      --data-urlencode "include_total=1" \
      --data-urlencode "start_after_time=${start_after_time}" \
      --data-urlencode "start_after_tradeid=${start_after_tradeid}" \
      -H "Cookie: steamLoginSecure=${STEAM_COOKIE_STEAMLOGINSECURE}; sessionid=${STEAM_COOKIE_SESSIONID}; browserid=${STEAM_COOKIE_BROWSERID}" \
      -H "User-Agent: ${STEAM_USER_AGENT}" \
      > "trade-history-page-${page}.json"
  else
    curl -s "https://api.steampowered.com/IEconService/GetTradeHistory/v1/" \
      --get \
      --data-urlencode "key=${STEAM_API_KEY}" \
      --data-urlencode "max_trades=100" \
      --data-urlencode "get_descriptions=1" \
      --data-urlencode "include_total=1" \
      -H "Cookie: steamLoginSecure=${STEAM_COOKIE_STEAMLOGINSECURE}; sessionid=${STEAM_COOKIE_SESSIONID}; browserid=${STEAM_COOKIE_BROWSERID}" \
      -H "User-Agent: ${STEAM_USER_AGENT}" \
      > "trade-history-page-${page}.json"
  fi

  more=$(jq -r '.response.more // false' "trade-history-page-${page}.json")
  last_time=$(jq -r '.response.trades[-1].time_init // empty' "trade-history-page-${page}.json")
  last_tradeid=$(jq -r '.response.trades[-1].tradeid // empty' "trade-history-page-${page}.json")

  echo "page=${page} more=${more} last_time=${last_time} last_tradeid=${last_tradeid}"

  [ "$more" != "true" ] && break
  [ -z "$last_time" ] && break
  [ -z "$last_tradeid" ] && break

  start_after_time="$last_time"
  start_after_tradeid="$last_tradeid"
  page=$((page + 1))

  sleep 2
done
