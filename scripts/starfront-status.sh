#!/usr/bin/env bash
set -u

# starfront-status.sh
#
# Query Starfront Alpaca roof safety and useful SkyAlert weather telemetry.
#
# Usage:
#   ./starfront-status.sh BUILDING_NUMBER
#
# Environment overrides:
#   ALPACA_BASE_URL=https://alpaca-api.tx.starfront.space
#   ALPACA_WEATHER_DEVICE=1
#   ALPACA_CLIENT_ID=override ~/.sfro_alpaca_clientid
#   ALPACA_TIMEOUT=8
#
# Exit codes:
#   0   roof safe and at least one useful weather value was read
#   1   usage error
#   2   missing dependency
#   10  roof explicitly unsafe
#   11  roof status unknown / roof API / transport / JSON error
#   20  roof safe, but all useful weather telemetry failed

BASE_URL="${ALPACA_BASE_URL:-https://alpaca-api.tx.starfront.space}"
WEATHER_DEVICE="${ALPACA_WEATHER_DEVICE:-1}"
TIMEOUT="${ALPACA_TIMEOUT:-8}"

if [[ $# -ne 1 || ! "$1" =~ ^[0-9]+$ ]]; then
  echo "Usage: $0 BUILDING_NUMBER" >&2
  exit 1
fi

for dep in curl jq date; do
  if ! command -v "$dep" >/dev/null 2>&1; then
    echo "Missing dependency: $dep" >&2
    exit 2
  fi
done

CLIENT_ID_FILE="${HOME}/.sfro_alpaca_clientid"
if [[ -n "${ALPACA_CLIENT_ID:-}" ]]; then
  CLIENT_ID="$ALPACA_CLIENT_ID"
elif [[ -f "$CLIENT_ID_FILE" ]]; then
  IFS= read -r CLIENT_ID < "$CLIENT_ID_FILE" || CLIENT_ID=""
else
  # RANDOM is a built-in bash var to generate 15-bit random unsigned integers.
  # We use this to generate a random 32-bit client ID.
  CLIENT_ID=$(( (RANDOM << 17) | (RANDOM << 2) | (RANDOM & 3) ))
  if [[ "$CLIENT_ID" -eq 0 ]]; then
    CLIENT_ID=1
  fi

  if ! printf '%s\n' "$CLIENT_ID" > "$CLIENT_ID_FILE"; then
    echo "Failed to write client ID file: $CLIENT_ID_FILE" >&2
    exit 1
  fi
fi

if [[ ! "$CLIENT_ID" =~ ^[1-9][0-9]*$ || ${#CLIENT_ID} -gt 10 ]]; then
  echo "Client ID must be an integer from 1 to 4294967295: $CLIENT_ID" >&2
  exit 1
fi
CLIENT_ID_NUM=$((10#$CLIENT_ID))
if (( CLIENT_ID_NUM < 1 || CLIENT_ID_NUM > 4294967295 )); then
  echo "Client ID must be an integer from 1 to 4294967295: $CLIENT_ID" >&2
  exit 1
fi
CLIENT_ID="$CLIENT_ID_NUM"

BUILDING="$1"
TXN=0

next_txn() {
  TXN=$((TXN + 1))
  printf '%s' "$TXN"
}

alpaca_get() {
  local path="$1"
  local txn sep url response curl_status

  txn="$(next_txn)"

  if [[ "$path" == *\?* ]]; then
    sep="&"
  else
    sep="?"
  fi

  url="${BASE_URL}${path}${sep}ClientID=${CLIENT_ID}&ClientTransactionID=${txn}"

  response="$(
    curl -fsS \
      --connect-timeout "$TIMEOUT" \
      --max-time "$TIMEOUT" \
      "$url" 2>&1
  )"
  curl_status=$?

  if [[ $curl_status -ne 0 ]]; then
    jq -n -c \
      --arg path "$path" \
      --arg url "$url" \
      --arg error "$response" \
      --argjson curl_status "$curl_status" \
      '{
        ok: false,
        path: $path,
        url: $url,
        transport_error: $error,
        curl_status: $curl_status
      }'
    return
  fi

  printf '%s\n' "$response" |
    jq -c \
      --arg path "$path" \
      --arg url "$url" '
        if ((.ErrorNumber // 0) | tonumber) != 0 then
          . + {
            ok: false,
            path: $path,
            url: $url
          }
        else
          . + {
            ok: true,
            path: $path,
            url: $url
          }
        end
      '
}

json_conditions_from_devicestate() {
  local raw="$1"

  printf '%s\n' "$raw" |
    jq -c '
      def clean:
        with_entries(select(.value != null));

      def state_entry($state_name):
        [ .Value[]? | select((.Name // "" | ascii_downcase) == ($state_name | ascii_downcase)) ][0];

      def condition($name; $state_name; $units):
        {
          ($name): (state_entry($state_name) as $entry | {
            ok: ($entry != null),
            endpoint: "devicestate",
            value: (if $entry == null then null else $entry.Value end),
            units: $units
          } | clean)
        };

      condition("dew_point"; "DewPoint"; "C") *
      condition("humidity"; "Humidity"; "percent") *
      condition("sky_temperature"; "SkyTemperature"; "C") *
      condition("temperature"; "Temperature"; "C") *
      condition("wind_speed"; "WindSpeed"; "m/s")
    '
}

roof_raw="$(alpaca_get "/api/v1/safetymonitor/${BUILDING}/issafe")"
conditions_raw="$(alpaca_get "/api/v1/observingconditions/${WEATHER_DEVICE}/devicestate")"
CONDITIONS_JSON="$(json_conditions_from_devicestate "$conditions_raw")"
VALID_WEATHER_COUNT="$(printf '%s\n' "$CONDITIONS_JSON" | jq '[.[] | select(.ok == true)] | length')"

ROOF_OK="$(printf '%s\n' "$roof_raw" | jq -r '.ok // false')"
ROOF_SAFE="$(printf '%s\n' "$roof_raw" | jq -r 'if has("Value") then .Value else "unknown" end')"

if [[ "$ROOF_OK" != "true" ]]; then
  OVERALL_SAFE=false
  STATE="ROOF_UNKNOWN"
  EXIT_CODE=11
elif [[ "$ROOF_SAFE" != "true" ]]; then
  OVERALL_SAFE=false
  STATE="ROOF_UNSAFE"
  EXIT_CODE=10
elif [[ "$VALID_WEATHER_COUNT" -eq 0 ]]; then
  OVERALL_SAFE=false
  STATE="WEATHER_UNAVAILABLE"
  EXIT_CODE=20
else
  OVERALL_SAFE=true
  STATE="SAFE"
  EXIT_CODE=0
fi

jq -n \
  --arg timestamp_utc "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg base_url "$BASE_URL" \
  --arg client_id "$CLIENT_ID" \
  --argjson building "$BUILDING" \
  --argjson weather_device "$WEATHER_DEVICE" \
  --argjson roof "$roof_raw" \
  --argjson conditions "$CONDITIONS_JSON" \
  --argjson valid_weather_count "$VALID_WEATHER_COUNT" \
  --arg state "$STATE" \
  --argjson safe "$OVERALL_SAFE" \
  --argjson exit_code "$EXIT_CODE" '
    {
      timestamp_utc: $timestamp_utc,
      base_url: $base_url,
      client_id: $client_id,
      building_number: $building,
      roof: {
        ok: ($roof.ok // false),
        safe: (if $roof | has("Value") then $roof.Value else null end),
        error_number: (if $roof | has("ErrorNumber") then $roof.ErrorNumber else null end),
        error_message: (if $roof | has("ErrorMessage") then $roof.ErrorMessage else null end),
        transport_error: ($roof.transport_error // null),
        curl_status: ($roof.curl_status // null),
        parse_error: ($roof.parse_error // null)
      } | with_entries(select(.value != null)),
      weather: {
        device_number: $weather_device,
        valid_condition_count: $valid_weather_count,
        conditions: $conditions,
      },
      overall: {
        safe: $safe,
        state: $state,
        exit_code: $exit_code
      }
    }
  '

exit "$EXIT_CODE"
