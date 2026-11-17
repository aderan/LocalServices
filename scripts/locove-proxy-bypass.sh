#!/bin/bash
# Run with administrator privileges. Change only the current primary network service.
set -euo pipefail
export LC_ALL=C

suffix="${1:-locove}"
if [[ ! "$suffix" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]]; then
  echo "Invalid local domain suffix." >&2
  exit 1
fi
rule="*.$suffix"

primary_service() {
  local family service
  for family in IPv4 IPv6; do
    service="$(printf 'show State:/Network/Global/%s\n' "$family" | /usr/sbin/scutil |
      /usr/bin/awk '$1 == "PrimaryService" && $2 == ":" { print $3 }')"
    if [[ "$service" =~ ^[[:xdigit:]-]{36}$ ]]; then
      printf '%s\n' "$service"
      return
    fi
  done
  return 1
}

if ! service_id="$(primary_service)"; then
  echo "No active primary network service." >&2
  exit 1
fi
service="$(printf 'show Setup:/Network/Service/%s\n' "$service_id" | /usr/sbin/scutil |
  /usr/bin/sed -n 's/^[[:space:]]*UserDefinedName : //p')"
if [[ -z "$service" || "$service" == *$'\n'* ]]; then
  echo "Could not identify the active network service." >&2
  exit 1
fi
if ! current="$(/usr/sbin/networksetup -getproxybypassdomains "$service")"; then
  echo "Could not read proxy exceptions for $service: $current" >&2
  exit 1
fi

entries=()
if [[ "$current" != "There aren't any bypass domains set on $service." ]]; then
  while IFS= read -r entry; do
    if [[ "$entry" == "$rule" ]]; then
      exit 0
    fi
    if [[ -n "$entry" ]]; then
      entries+=("$entry")
    fi
  done <<< "$current"
fi
entries+=("$rule")
if [[ "$(primary_service)" != "$service_id" ]]; then
  echo "The active network changed. Try again on the current network." >&2
  exit 1
fi

/usr/sbin/networksetup -setproxybypassdomains "$service" "${entries[@]}"
