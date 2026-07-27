#!/usr/bin/env bash
set -Eeuo pipefail

# Safe wrapper around setup.sh.
#
# Use setup.sh directly when you want every option on the command line.
# Use install.sh when you prefer to keep repeatable local values in .env.
# The wrapper prints the exact setup.sh command before running it.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ENV_FILE:-${SCRIPT_DIR}/.env}"

usage() {
  cat <<'EOF'
MeshNet install wrapper

Usage:
  cp .env.example .env
  edit .env
  ./install.sh

This wrapper reads .env and calls ./setup.sh with matching flags.
For one-off installs you can call ./setup.sh directly.

Important safety behavior:
  - If .env is missing, this runs a dry run.
  - setup.sh does not edit configuration.yaml.
  - custom_components/meshnet is installed only when INSTALL_CUSTOM_COMPONENT=1.
EOF
}

if [[ "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

trim_space() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "${value}"
}

allowed_env_key() {
  case "$1" in
    HA_CONFIG_DIR|MESHNET_OUTPUT_DIR|INSTALL_CUSTOM_COMPONENT|YES|DRY_RUN|VERBOSE|\
    WIFI_MESHTASTIC|USB_MESHTASTIC|WIFI_MESHCORE|USB_MESHCORE|\
    HA_VERSION|HA_CONFIG_DOCKER|HA_HTTP_PORT|TZ) return 0 ;;
    *) return 1 ;;
  esac
}

load_env_file() {
  local line key value line_number=0
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line_number=$((line_number + 1))
    line="$(trim_space "${line}")"
    [[ -z "${line}" || "${line}" == \#* ]] && continue
    [[ "${line}" == export\ * ]] && line="${line#export }"
    if [[ "${line}" != *=* ]]; then
      echo "Ignoring malformed .env line ${line_number}." >&2
      continue
    fi
    key="$(trim_space "${line%%=*}")"
    value="$(trim_space "${line#*=}")"
    if ! allowed_env_key "${key}"; then
      echo "Ignoring unknown .env key on line ${line_number}: ${key}" >&2
      continue
    fi
    if [[ "${#value}" -ge 2 ]]; then
      if [[ "${value:0:1}" == '"' && "${value: -1}" == '"' ]]; then
        value="${value:1:${#value}-2}"
      elif [[ "${value:0:1}" == "'" && "${value: -1}" == "'" ]]; then
        value="${value:1:${#value}-2}"
      fi
    fi
    # printf -v assigns the literal value. It never evaluates command
    # substitutions, backticks, shell operators, or variable references.
    printf -v "${key}" '%s' "${value}"
  done <"${ENV_FILE}"
}

if [[ -f "${ENV_FILE}" ]]; then
  load_env_file
else
  echo "No .env found at ${ENV_FILE}; running setup.sh in dry-run mode."
  DRY_RUN=1
fi

truthy() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|y|Y|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

append_list_args() {
  local flag="$1"
  local value="$2"
  local normalized item
  local -a items
  normalized="${value//,/ }"
  read -r -a items <<<"${normalized}"
  for item in "${items[@]}"; do
    [[ -n "${item}" ]] || continue
    ARGS+=("${flag}" "${item}")
  done
}

ARGS=()

[[ -n "${HA_CONFIG_DIR:-}" ]] && ARGS+=("--config-dir" "${HA_CONFIG_DIR}")
[[ -n "${MESHNET_OUTPUT_DIR:-}" ]] && ARGS+=("--output-dir" "${MESHNET_OUTPUT_DIR}")

truthy "${INSTALL_CUSTOM_COMPONENT:-0}" && ARGS+=("--install-custom-component")
truthy "${YES:-0}" && ARGS+=("--yes")
truthy "${DRY_RUN:-0}" && ARGS+=("--dry-run")
truthy "${VERBOSE:-0}" && ARGS+=("--verbose")

[[ -n "${WIFI_MESHTASTIC:-}" ]] && append_list_args "--wifi-meshtastic" "${WIFI_MESHTASTIC}"
[[ -n "${USB_MESHTASTIC:-}" ]] && append_list_args "--usb-meshtastic" "${USB_MESHTASTIC}"
[[ -n "${WIFI_MESHCORE:-}" ]] && append_list_args "--wifi-meshcore" "${WIFI_MESHCORE}"
[[ -n "${USB_MESHCORE:-}" ]] && append_list_args "--usb-meshcore" "${USB_MESHCORE}"

printf 'Running: %q' "${SCRIPT_DIR}/setup.sh"
printf ' %q' "${ARGS[@]}"
printf '\n'

exec "${SCRIPT_DIR}/setup.sh" "${ARGS[@]}"
