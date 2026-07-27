#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="${SCRIPT_DIR}/ha-mesh-setup-output"
CONFIG_DIR="${HA_CONFIG_DIR:-}"
CONFIG_DIR_REQUESTED=0
VERBOSE=0
PASS_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0

[[ -n "${CONFIG_DIR}" ]] && CONFIG_DIR_REQUESTED=1

usage() {
  cat <<'EOF'
Verify a MeshNet Home Assistant setup.

Usage:
  ./verify_setup.sh
  ./verify_setup.sh --output-dir ./ha-mesh-setup-output
  ./verify_setup.sh --config-dir /config
  ./verify_setup.sh --verbose

Staged setup artifacts are optional. An installed MeshNet manifest, Home
Assistant configuration file, configured gateway reachability, and any Home
Assistant config check that can be run are treated as required checks.

This script does not modify Home Assistant.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help) usage; exit 0 ;;
    --output-dir)
      [[ $# -ge 2 ]] || { echo "--output-dir requires a path"; exit 2; }
      OUTPUT_DIR="$2"; shift 2 ;;
    --config-dir)
      [[ $# -ge 2 ]] || { echo "--config-dir requires a path"; exit 2; }
      CONFIG_DIR="$2"; CONFIG_DIR_REQUESTED=1; shift 2 ;;
    --verbose) VERBOSE=1; shift ;;
    *) echo "Unknown option: $1"; usage; exit 2 ;;
  esac
done

log() { printf '%s\n' "$*"; }
command_exists() { command -v "$1" >/dev/null 2>&1; }

pass() {
  PASS_COUNT=$((PASS_COUNT + 1))
  log "OK: $*"
}

warn() {
  WARN_COUNT=$((WARN_COUNT + 1))
  log "WARN: $*"
}

fail_required() {
  FAIL_COUNT=$((FAIL_COUNT + 1))
  log "FAIL: $*"
}

find_config_dir() {
  if [[ "${CONFIG_DIR_REQUESTED}" -eq 1 ]]; then
    [[ -d "${CONFIG_DIR}" ]]
    return
  fi
  local candidate
  for candidate in \
    /config \
    /homeassistant \
    /usr/share/hassio/homeassistant \
    /var/lib/homeassistant \
    /home/homeassistant/.homeassistant \
    "${HOME:-}/.homeassistant" \
    "${PWD}/config"; do
    [[ -n "${candidate}" ]] || continue
    if [[ -d "${candidate}" ]]; then
      CONFIG_DIR="${candidate}"
      return 0
    fi
  done
  return 1
}

check_optional_file() {
  local path="$1"
  if [[ -e "${path}" ]]; then
    pass "optional staged file present: ${path}"
  else
    warn "optional staged file not found: ${path}"
  fi
}

check_required_file() {
  local path="$1"
  if [[ -e "${path}" ]]; then
    pass "${path}"
  else
    fail_required "required file missing: ${path}"
  fi
}

test_tcp_from_generated_config() {
  local config="${OUTPUT_DIR}/generated_config.yaml"
  [[ -f "${config}" ]] || return 0
  log
  log "TCP checks from generated_config.yaml"
  local endpoint host port port_number
  local endpoint_count=0
  while IFS= read -r endpoint; do
    [[ -n "${endpoint}" ]] || continue
    endpoint_count=$((endpoint_count + 1))
    host="${endpoint%:*}"
    port="${endpoint##*:}"
    if [[ -z "${host}" || ! "${port}" =~ ^[0-9]+$ ]]; then
      fail_required "invalid generated TCP endpoint: ${endpoint}"
      continue
    fi
    port_number=$((10#${port}))
    if [[ "${port_number}" -lt 1 || "${port_number}" -gt 65535 ]]; then
      fail_required "invalid generated TCP endpoint: ${endpoint}"
      continue
    fi
    log "Testing ${host}:${port}"
    if command_exists nc; then
      if nc -z -w 3 "${host}" "${port}" >/dev/null 2>&1; then
        pass "TCP open: ${host}:${port}"
      else
        fail_required "TCP closed or unreachable: ${host}:${port}"
      fi
    elif command_exists timeout && command_exists bash; then
      if timeout 4 bash -c 'cat </dev/null >"/dev/tcp/${1}/${2}"' _ "${host}" "${port}" >/dev/null 2>&1; then
        pass "TCP open: ${host}:${port}"
      else
        fail_required "TCP closed or unreachable: ${host}:${port}"
      fi
    else
      warn "cannot test ${host}:${port}; neither nc nor timeout with bash is available"
    fi
  done < <(
    awk '
      $1 == "host:" {host=$2; gsub(/^"|"$/, "", host)}
      $1 == "port:" && host != "" {print host ":" $2; host=""}
    ' "${config}"
  )
  if [[ "${endpoint_count}" -eq 0 ]]; then
    log "SKIP: no TCP gateways in generated_config.yaml"
  fi
}

test_serial_from_generated_config() {
  local config="${OUTPUT_DIR}/generated_config.yaml"
  [[ -f "${config}" ]] || return 0
  log
  log "Serial checks from generated_config.yaml"
  local dev
  local device_count=0
  while IFS= read -r dev; do
    [[ -n "${dev}" ]] || continue
    device_count=$((device_count + 1))
    if [[ ! -e "${dev}" ]]; then
      fail_required "serial device missing: ${dev}"
    elif [[ -r "${dev}" && -w "${dev}" ]]; then
      pass "serial device is readable/writable: ${dev}"
    else
      fail_required "serial device is not readable/writable: ${dev}"
    fi
  done < <(awk '$1 == "serial_path:" {value=$2; gsub(/^"|"$/, "", value); print value}' "${config}")
  if [[ "${device_count}" -eq 0 ]]; then
    log "SKIP: no serial gateways in generated_config.yaml"
  fi
}

run_ha_config_check() {
  log
  log "Home Assistant config validation"
  if command_exists ha; then
    log "Running: ha core check"
    if ha core check; then
      pass "ha core check"
    else
      fail_required "ha core check failed"
    fi
    return 0
  fi
  if command_exists docker; then
    local container
    container="$(docker ps --format '{{.Names}}' 2>/dev/null | grep -Ei 'homeassistant|home-assistant' | head -1 || true)"
    if [[ -n "${container}" ]]; then
      log "Found container: ${container}"
      log "Running Home Assistant config check inside ${container}"
      if docker exec "${container}" python -m homeassistant --script check_config -c /config; then
        pass "container config check"
      else
        fail_required "container config check failed"
      fi
      return 0
    fi
  fi
  if command_exists hass && [[ -n "${CONFIG_DIR}" ]]; then
    log "Running: hass --script check_config -c ${CONFIG_DIR}"
    if hass --script check_config -c "${CONFIG_DIR}"; then
      pass "hass config check"
    else
      fail_required "hass config check failed"
    fi
    return 0
  fi
  warn "Home Assistant config check skipped; no usable ha CLI, HA container, or hass command found"
}

check_ha_version() {
  [[ -n "${CONFIG_DIR}" ]] || return 0
  local version_file="${CONFIG_DIR}/.HA_VERSION"
  if [[ ! -f "${version_file}" ]]; then
    warn "Home Assistant version file not found; confirm Home Assistant 2025.1.4 or newer"
    return 0
  fi
  local detected oldest
  detected="$(tr -d '[:space:]' <"${version_file}")"
  if [[ -z "${detected}" ]]; then
    warn "Home Assistant version file is empty"
    return 0
  fi
  if command_exists sort; then
    oldest="$(printf '%s\n%s\n' "2025.1.4" "${detected}" | sort -V | head -1)"
    if [[ "${oldest}" != "2025.1.4" ]]; then
      fail_required "Home Assistant ${detected} is older than the supported minimum 2025.1.4"
      return 0
    fi
  fi
  pass "Home Assistant version ${detected}"
}

main() {
  local config_found=0
  if find_config_dir; then
    config_found=1
  fi

  log "MeshNet verification"
  log "Output directory: ${OUTPUT_DIR}"
  log "Config directory: ${CONFIG_DIR:-not detected}"
  log

  check_optional_file "${OUTPUT_DIR}/detected_environment.txt"
  check_optional_file "${OUTPUT_DIR}/detected_serial_devices.txt"
  check_optional_file "${OUTPUT_DIR}/generated_config.yaml"
  check_optional_file "${OUTPUT_DIR}/generated_automations.yaml"
  check_optional_file "${OUTPUT_DIR}/generated_dashboard.yaml"
  check_optional_file "${OUTPUT_DIR}/rollback_info.json"

  if [[ "${config_found}" -eq 1 ]]; then
    check_required_file "${CONFIG_DIR}/custom_components/meshnet/manifest.json"
    check_required_file "${CONFIG_DIR}/configuration.yaml"
    check_ha_version
  elif [[ "${CONFIG_DIR_REQUESTED}" -eq 1 ]]; then
    fail_required "configured Home Assistant directory does not exist: ${CONFIG_DIR}"
  else
    fail_required "Home Assistant config directory was not detected; pass --config-dir"
  fi

  test_tcp_from_generated_config
  test_serial_from_generated_config
  run_ha_config_check

  log
  log "Manual Home Assistant checks still required:"
  log "  - Settings -> Devices & Services shows MeshNet loaded."
  log "  - MeshNet devices/entities appear."
  log "  - The MeshNet sidebar panel opens for an admin user."
  log "  - meshnet.send_message works from Developer Tools -> Actions."
  log
  log "Summary: ${PASS_COUNT} passed, ${WARN_COUNT} warnings, ${FAIL_COUNT} failed"
  if [[ "${FAIL_COUNT}" -gt 0 ]]; then
    log "Verification FAILED"
    return 1
  fi
  log "Verification PASSED"
  return 0
}

main "$@"
