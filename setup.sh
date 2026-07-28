#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="${SCRIPT_DIR}/ha-mesh-setup-output"
YES=0
DRY_RUN=0
VERBOSE=0
CONFIG_DIR="${HA_CONFIG_DIR:-}"
INSTALL_COMPONENT=0
COMPONENT_INSTALLED=0

WIFI_MESHTASTIC=()
WIFI_MESHCORE=()
USB_MESHTASTIC=()
USB_MESHCORE=()

TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
LOG_FILE=""
ROLLBACK_FILE=""

usage() {
  cat <<'EOF'
Home Assistant MeshNet setup helper

This script is safe by default:
  - writes generated files to ./ha-mesh-setup-output/
  - asks before changing Home Assistant
  - backs up anything it replaces
  - supports dry-run mode

Usage:
  ./setup.sh --help
  ./setup.sh --dry-run
  ./setup.sh --wifi-meshtastic 192.0.2.50:4403
  ./setup.sh --wifi-meshcore 192.0.2.51:PORT
  ./setup.sh --usb-meshtastic /dev/serial/by-id/usb-...
  ./setup.sh --usb-meshcore /dev/ttyACM0
  ./setup.sh --yes --verbose

Options:
  --config-dir PATH              Home Assistant config directory.
  --output-dir PATH              Staging output directory. Default: ./ha-mesh-setup-output
  --install-custom-component     Copy custom_components/meshnet into HA config after backup.
  --wifi-meshtastic HOST:PORT    Add/test a Meshtastic TCP gateway. Common port: 4403.
  --wifi-meshcore HOST:PORT      Add/test a MeshCore TCP gateway. Port depends on firmware/gateway.
  --usb-meshtastic DEVICE        Add/test a Meshtastic serial gateway.
  --usb-meshcore DEVICE          Add/test a MeshCore serial gateway.
  --yes                          Do not ask confirmation questions.
  --dry-run                      Detect and generate files, but do not modify Home Assistant.
  --verbose                      Print extra command output.
  --help                         Show this help.

Important:
  If MeshCore TCP port is unknown, pass HOST:PORT with PORT as a real number after checking
  your MeshCore firmware/gateway docs or device UI. The script cannot safely guess it.
EOF
}

log() {
  local msg="$*"
  printf '%s\n' "$msg"
  if [[ -n "${LOG_FILE}" ]]; then
    printf '[%s] %s\n' "$(date --iso-8601=seconds 2>/dev/null || date)" "$msg" >>"${LOG_FILE}"
  fi
}

debug() {
  if [[ "${VERBOSE}" -eq 1 ]]; then
    log "DEBUG: $*"
  fi
}

die() {
  log "ERROR: $*"
  exit 1
}

run_cmd() {
  log "+ $*"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    log "DRY RUN: command not executed"
    return 0
  fi
  "$@"
}

yaml_quote() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//$'\n'/\\n}"
  value="${value//$'\r'/\\r}"
  value="${value//$'\t'/\\t}"
  printf '"%s"' "${value}"
}

ask_yes_no() {
  local prompt="$1"
  if [[ "${YES}" -eq 1 ]]; then
    log "${prompt} yes (--yes)"
    return 0
  fi
  local answer
  printf '%s [y/N]: ' "${prompt}"
  read -r answer || true
  case "${answer}" in
    y|Y|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

need_linux() {
  [[ "$(uname -s)" == "Linux" ]] || die "This script only supports Linux."
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --help) usage; exit 0 ;;
      --yes) YES=1; shift ;;
      --dry-run) DRY_RUN=1; shift ;;
      --verbose) VERBOSE=1; shift ;;
      --install-custom-component) INSTALL_COMPONENT=1; shift ;;
      --config-dir)
        [[ $# -ge 2 ]] || die "--config-dir requires a path"
        CONFIG_DIR="$2"; shift 2 ;;
      --output-dir)
        [[ $# -ge 2 ]] || die "--output-dir requires a path"
        OUTPUT_DIR="$2"; shift 2 ;;
      --wifi-meshtastic)
        [[ $# -ge 2 ]] || die "--wifi-meshtastic requires HOST:PORT"
        WIFI_MESHTASTIC+=("$2"); shift 2 ;;
      --wifi-meshcore)
        [[ $# -ge 2 ]] || die "--wifi-meshcore requires HOST:PORT"
        WIFI_MESHCORE+=("$2"); shift 2 ;;
      --usb-meshtastic)
        [[ $# -ge 2 ]] || die "--usb-meshtastic requires a device path"
        USB_MESHTASTIC+=("$2"); shift 2 ;;
      --usb-meshcore)
        [[ $# -ge 2 ]] || die "--usb-meshcore requires a device path"
        USB_MESHCORE+=("$2"); shift 2 ;;
      *)
        die "Unknown option: $1. Run ./setup.sh --help."
        ;;
    esac
  done
}

prepare_output() {
  mkdir -p "${OUTPUT_DIR}"
  chmod 700 "${OUTPUT_DIR}"
  LOG_FILE="${OUTPUT_DIR}/install_log.txt"
  ROLLBACK_FILE="${OUTPUT_DIR}/rollback_info.json"
  : >"${LOG_FILE}"
  log "MeshNet setup started at ${TIMESTAMP}"
  log "Output directory: ${OUTPUT_DIR}"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    log "Dry run mode: enabled"
  fi
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

detect_install_type() {
  local out="${OUTPUT_DIR}/detected_environment.txt"
  {
    echo "timestamp=${TIMESTAMP}"
    echo "kernel=$(uname -srm)"
    echo
    echo "Detected Home Assistant hints:"
    if [[ -n "${CONFIG_DIR}" && -f "${CONFIG_DIR}/.HA_VERSION" ]]; then
      echo "- Home Assistant config version: $(tr -d '[:space:]' <"${CONFIG_DIR}/.HA_VERSION")"
    fi
    if command_exists ha; then
      echo "- ha CLI found: Home Assistant OS or Supervised is likely."
    else
      echo "- ha CLI not found."
    fi
    if command_exists docker; then
      echo "- docker found."
      if docker ps --format '{{.Image}}' 2>/dev/null | grep -Eqi 'homeassistant|hassio|supervisor'; then
        echo "  A running Home Assistant-related container is visible."
      else
        echo "  No running Home Assistant-related container is visible."
      fi
    else
      echo "- docker not found."
    fi
    if command_exists systemctl; then
      echo "- systemd found."
      if systemctl list-units --type=service --all 2>/dev/null | grep -Eqi 'home-assistant|hass'; then
        echo "  A Home Assistant-related service is visible."
      else
        echo "  No obvious Home Assistant service is visible."
      fi
    else
      echo "- systemd not found."
    fi
    echo
    echo "Python:"
    command_exists python3 && echo "- python3: found" || echo "- python3: missing"
    python3 --version 2>&1 || true
    echo
    echo "Network tools:"
    for tool in ping nc ss ip curl; do
      if command_exists "${tool}"; then
        echo "- ${tool}: found"
      else
        echo "- ${tool}: missing"
      fi
    done
  } >"${out}"
  log "Wrote environment detection to ${out}"
}

check_ha_version() {
  local detected=""
  if [[ -n "${CONFIG_DIR}" && -f "${CONFIG_DIR}/.HA_VERSION" ]]; then
    detected="$(tr -d '[:space:]' <"${CONFIG_DIR}/.HA_VERSION")"
  elif command_exists hass; then
    detected="$(hass --version 2>/dev/null | head -1 || true)"
  fi
  [[ -n "${detected}" ]] || return 0
  log "Detected Home Assistant version: ${detected}"
  if command_exists sort; then
    local oldest
    oldest="$(printf '%s\n%s\n' "2025.1.4" "${detected}" | sort -V | head -1)"
    if [[ "${oldest}" != "2025.1.4" ]]; then
      log "WARNING: MeshNet 0.5.3 targets Home Assistant 2025.1.4 or newer."
      log "Upgrade Home Assistant before installing this component."
    fi
  fi
}

find_config_candidates() {
  local candidates=()
  [[ -n "${CONFIG_DIR}" ]] && candidates+=("${CONFIG_DIR}")
  candidates+=(
    "/config"
    "/homeassistant"
    "/usr/share/hassio/homeassistant"
    "/var/lib/homeassistant"
    "/home/homeassistant/.homeassistant"
    "${HOME:-}/.homeassistant"
    "${PWD}/config"
  )
  local found=()
  local path
  for path in "${candidates[@]}"; do
    [[ -n "${path}" ]] || continue
    if [[ -d "${path}" ]] && {
      [[ -f "${path}/configuration.yaml" ]] \
        || [[ -d "${path}/.storage" ]] \
        || [[ -d "${path}/custom_components" ]];
    }; then
      found+=("${path}")
    fi
  done
  if [[ "${#found[@]}" -gt 0 ]]; then
    printf '%s\n' "${found[@]}" | awk '!seen[$0]++'
  fi
}

choose_config_dir() {
  local candidates
  mapfile -t candidates < <(find_config_candidates)
  if [[ -n "${CONFIG_DIR}" ]]; then
    [[ -d "${CONFIG_DIR}" ]] || die "Configured Home Assistant directory does not exist: ${CONFIG_DIR}"
    log "Using Home Assistant config directory from argument/environment: ${CONFIG_DIR}"
    return
  fi
  if [[ "${#candidates[@]}" -eq 1 ]]; then
    CONFIG_DIR="${candidates[0]}"
    log "Detected one likely Home Assistant config directory: ${CONFIG_DIR}"
    return
  fi
  if [[ "${#candidates[@]}" -gt 1 ]]; then
    log "Multiple possible Home Assistant config directories found:"
    local i
    for i in "${!candidates[@]}"; do
      log "  $((i + 1))) ${candidates[$i]}"
    done
    if [[ "${YES}" -eq 1 ]]; then
      log "Refusing to guess among multiple Home Assistant directories in --yes mode."
      log "Rerun with --config-dir PATH. Detection and staging will continue without installation."
      return
    fi
    local choice
    printf 'Pick a number, or press Enter to skip installing files: '
    read -r choice || true
    if [[ "${choice}" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#candidates[@]} )); then
      CONFIG_DIR="${candidates[$((choice - 1))]}"
      log "Using Home Assistant config directory: ${CONFIG_DIR}"
      return
    fi
  fi
  log "No Home Assistant config directory selected."
  log "This is OK. The script will still generate config snippets in ${OUTPUT_DIR}."
}

detect_serial_devices() {
  local out="${OUTPUT_DIR}/detected_serial_devices.txt"
  {
    echo "PRIVATE: contains local hardware identifiers; do not publish this file."
    echo
    echo "Serial device scan at ${TIMESTAMP}"
    echo
    echo "Stable paths from /dev/serial/by-id:"
    if compgen -G "/dev/serial/by-id/*" >/dev/null; then
      for path in /dev/serial/by-id/*; do
        printf '%s -> %s\n' "${path}" "$(readlink -f "${path}")"
        if command_exists udevadm; then
          udevadm info --query=property --name="$(readlink -f "${path}")" 2>/dev/null \
            | grep -E 'ID_MODEL=|ID_VENDOR=|ID_SERIAL=|ID_USB_DRIVER=' \
            | sed 's/^/  /' || true
        fi
      done
    else
      echo "  none found"
    fi
    echo
    echo "Short paths:"
    ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null || echo "  none found"
    echo
    echo "USB devices:"
    if command_exists lsusb; then
      lsusb 2>&1 || echo "  lsusb failed in this environment"
    else
      echo "  lsusb not installed"
    fi
    echo
    echo "Likely device classifications:"
    if compgen -G "/dev/serial/by-id/*" >/dev/null; then
      for path in /dev/serial/by-id/*; do
        local lower
        lower="$(basename "${path}" | tr '[:upper:]' '[:lower:]')"
        if [[ "${lower}" =~ meshtastic|heltec|lilygo|t-beam|tbeam|rak|wisblock|cp210|ch340|esp32|nrf52 ]]; then
          echo "  likely Meshtastic-capable serial adapter: ${path}"
        fi
        if [[ "${lower}" =~ meshcore|heltec|rak|wisblock|nrf52|esp32|rp2040|pico|stm32|cp210|ch340 ]]; then
          echo "  possible MeshCore serial adapter: ${path}"
        fi
      done
    else
      echo "  no serial-by-id devices available to classify"
    fi
    echo
    echo "Serial permission hint:"
    if id -nG | grep -Eq '(^| )(dialout|tty|uucp|lock)( |$)'; then
      echo "  Your user is already in at least one common serial group."
    else
      echo "  Your user is not in dialout/tty/uucp/lock."
      echo "  On Debian/Ubuntu, run: sudo usermod -aG dialout <YOUR_USER>"
      echo "  Then log out and back in."
    fi
  } >"${out}"
  log "Wrote serial detection to ${out}"
}

resolve_meshcore_ports() {
  local resolved=()
  local item host port answer
  for item in "${WIFI_MESHCORE[@]}"; do
    host="${item%:*}"
    port="${item##*:}"
    if [[ "${port}" =~ ^[0-9]+$ ]]; then
      resolved+=("${item}")
      continue
    fi
    log "MeshCore TCP port is not numeric in: ${item}"
    log "MeshCore TCP ports depend on firmware/gateway configuration."
    log "Find it in the MeshCore firmware, gateway UI, bridge config, or documentation."
    if [[ "${YES}" -eq 1 ]]; then
      log "Skipping ${item} because --yes was passed and the port is unknown."
      continue
    fi
    printf 'Enter MeshCore TCP port for %s, or press Enter to skip: ' "${host}"
    read -r answer || true
    if [[ "${answer}" =~ ^[0-9]+$ && "${answer}" -ge 1 && "${answer}" -le 65535 ]]; then
      resolved+=("${host}:${answer}")
      log "Using MeshCore TCP endpoint ${host}:${answer}"
    else
      log "Skipped MeshCore TCP endpoint ${host}; no valid port entered."
    fi
  done
  WIFI_MESHCORE=("${resolved[@]}")
}

validate_host_port() {
  local value="$1"
  [[ "${value}" == *:* ]] || return 1
  local host="${value%:*}"
  local port="${value##*:}"
  [[ -n "${host}" && "${port}" =~ ^[0-9]+$ && "${port}" -ge 1 && "${port}" -le 65535 ]]
}

test_tcp() {
  local label="$1"
  local value="$2"
  if ! validate_host_port "${value}"; then
    log "${label}: ${value} is not HOST:PORT with a numeric port."
    log "If this is MeshCore, find the TCP API port in the firmware/gateway UI, then rerun with --wifi-meshcore HOST:PORT."
    return 1
  fi
  local host="${value%:*}"
  local port="${value##*:}"
  log "Testing ${label} gateway ${host}:${port}"
  if command_exists ping; then
    if ping -c 1 -W 2 "${host}" >/dev/null 2>&1; then
      log "  ping: ok"
    else
      log "  ping: failed or blocked. Continuing to port test."
    fi
  fi
  if command_exists nc; then
    if nc -z -w 3 "${host}" "${port}" >/dev/null 2>&1; then
      log "  TCP port ${port}: open"
      return 0
    fi
    log "  TCP port ${port}: not reachable"
    return 1
  fi
  if timeout 4 bash -c 'cat < /dev/null > /dev/tcp/"$1"/"$2"' _ "${host}" "${port}" >/dev/null 2>&1; then
    log "  TCP port ${port}: open"
    return 0
  fi
  log "  TCP port ${port}: not reachable"
  return 1
}

test_serial() {
  local label="$1"
  local device="$2"
  log "Testing ${label} serial device ${device}"
  if [[ ! -e "${device}" ]]; then
    log "  missing: ${device}"
    return 1
  fi
  ls -l "${device}" | tee -a "${LOG_FILE}" >/dev/null
  if [[ -r "${device}" && -w "${device}" ]]; then
    log "  permissions: readable and writable"
  else
    log "  permissions: not readable/writable by current user"
    log "  likely fix: sudo usermod -aG dialout <YOUR_USER>"
    log "  then log out/in, replug the USB device, and rerun this script"
  fi
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    log "  dry run: skipped stty configuration; read-only checks only"
    return 0
  fi
  if command_exists stty; then
    if stty -F "${device}" 115200 raw -echo -echoe -echok -echoctl -echoke >/dev/null 2>&1; then
      log "  stty: ok"
      return 0
    fi
    log "  stty: failed. Device may be busy, permission denied, or not a serial adapter."
    return 1
  fi
  log "  stty missing; only existence/permission checks were performed"
}

run_connectivity_tests() {
  local item
  for item in "${WIFI_MESHTASTIC[@]}"; do
    test_tcp "Meshtastic WiFi/TCP" "${item}" || true
  done
  for item in "${WIFI_MESHCORE[@]}"; do
    test_tcp "MeshCore WiFi/TCP" "${item}" || true
  done
  for item in "${USB_MESHTASTIC[@]}"; do
    test_serial "Meshtastic USB" "${item}" || true
  done
  for item in "${USB_MESHCORE[@]}"; do
    test_serial "MeshCore USB" "${item}" || true
  done
}

write_gateway_yaml() {
  local out="${OUTPUT_DIR}/generated_config.yaml"
  local has_gateways=0
  if [[ ${#WIFI_MESHTASTIC[@]} -gt 0 || ${#USB_MESHTASTIC[@]} -gt 0 || ${#WIFI_MESHCORE[@]} -gt 0 || ${#USB_MESHCORE[@]} -gt 0 ]]; then
    has_gateways=1
  fi
  {
    echo "# Generated by setup.sh at ${TIMESTAMP}"
    echo "# Copy this into configuration.yaml only if you want YAML import."
    echo "# The safer path is Settings -> Devices & Services -> Add Integration -> MeshNet."
    echo
    echo "meshnet:"
    echo "  node_timeout: 900"
    echo "  history_days: 30"
    if [[ "${has_gateways}" -eq 1 ]]; then
      echo "  gateways:"
    else
      echo "  gateways: {}"
      echo "  # No gateways were passed on the command line."
      echo "  # Add one from the MeshNet UI or rerun setup.sh with gateway options."
    fi
    local idx=1
    local item host port dev
    for item in "${WIFI_MESHTASTIC[@]}"; do
      host="${item%:*}"; port="${item##*:}"
      echo "    meshtastic_wifi_${idx}:"
      echo "      gateway_id: meshtastic_wifi_${idx}"
      echo "      name: Meshtastic WiFi ${idx}"
      echo "      protocol: meshtastic"
      echo "      transport: tcp"
      echo "      host: $(yaml_quote "${host}")"
      echo "      port: ${port}"
      idx=$((idx + 1))
    done
    idx=1
    for dev in "${USB_MESHTASTIC[@]}"; do
      echo "    meshtastic_usb_${idx}:"
      echo "      gateway_id: meshtastic_usb_${idx}"
      echo "      name: Meshtastic USB ${idx}"
      echo "      protocol: meshtastic"
      echo "      transport: serial"
      echo "      serial_path: $(yaml_quote "${dev}")"
      idx=$((idx + 1))
    done
    idx=1
    for item in "${WIFI_MESHCORE[@]}"; do
      host="${item%:*}"; port="${item##*:}"
      echo "    meshcore_wifi_${idx}:"
      echo "      gateway_id: meshcore_wifi_${idx}"
      echo "      name: MeshCore WiFi ${idx}"
      echo "      protocol: meshcore"
      echo "      transport: tcp"
      echo "      host: $(yaml_quote "${host}")"
      echo "      port: ${port}"
      echo "      options:"
      echo "        # Set pin/debug/baudrate here only if your MeshCore gateway requires it."
      echo "        debug: false"
      idx=$((idx + 1))
    done
    idx=1
    for dev in "${USB_MESHCORE[@]}"; do
      echo "    meshcore_usb_${idx}:"
      echo "      gateway_id: meshcore_usb_${idx}"
      echo "      name: MeshCore USB ${idx}"
      echo "      protocol: meshcore"
      echo "      transport: serial"
      echo "      serial_path: $(yaml_quote "${dev}")"
      echo "      options:"
      echo "        baudrate: 115200"
      echo "        debug: false"
      idx=$((idx + 1))
    done
  } >"${out}"
  log "Wrote generated Home Assistant config to ${out}"
}

write_automations_yaml() {
  local out="${OUTPUT_DIR}/generated_automations.yaml"
  cp "${SCRIPT_DIR}/examples/automations.yaml" "${out}"
  log "Wrote generated automations to ${out}"
}

write_dashboard_yaml() {
  local out="${OUTPUT_DIR}/generated_dashboard.yaml"
  cp "${SCRIPT_DIR}/examples/dashboard.yaml" "${out}"
  log "Wrote generated dashboard to ${out}"
}

write_rollback_json() {
  local installed_path=""
  if [[ -n "${CONFIG_DIR}" ]]; then
    installed_path="${CONFIG_DIR}/custom_components/meshnet"
  fi
  cat >"${ROLLBACK_FILE}" <<EOF
{
  "timestamp": "${TIMESTAMP}",
  "script_dir": "${SCRIPT_DIR}",
  "output_dir": "${OUTPUT_DIR}",
  "config_dir": "${CONFIG_DIR}",
  "custom_component_path": "${installed_path}",
  "custom_component_installed_by_setup": ${COMPONENT_INSTALLED},
  "backup_path": "${OUTPUT_DIR}/backup-custom_components-meshnet-${TIMESTAMP}",
  "notes": [
    "setup.sh does not edit configuration.yaml automatically.",
    "Generated YAML is staged in ha-mesh-setup-output.",
    "uninstall.sh can remove the copied custom component if setup.sh installed it."
  ]
}
EOF
  log "Wrote rollback metadata to ${ROLLBACK_FILE}"
  if [[ -n "${CONFIG_DIR}" && -d "${CONFIG_DIR}" ]]; then
    if [[ "${DRY_RUN}" -eq 0 ]]; then
      if cp "${ROLLBACK_FILE}" "${CONFIG_DIR}/.meshnet_setup_rollback.json" 2>/dev/null; then
        chmod 600 "${CONFIG_DIR}/.meshnet_setup_rollback.json"
      fi
    fi
  fi
}

install_python_notes() {
  local out="${OUTPUT_DIR}/python_package_notes.txt"
  {
    echo "Python package notes"
    echo
    echo "The MeshNet custom integration declares Python requirements in manifest.json:"
    echo "  meshtastic==2.7.11"
    echo "  meshcore==2.3.7"
    echo
    echo "Home Assistant usually installs custom integration requirements when the integration loads."
    echo
    echo "Home Assistant OS:"
    echo "  Do not use apt or pip on the host. Restart HA after installing the custom component."
    echo
    echo "Home Assistant Container:"
    echo "  Requirements install inside the container when HA loads the integration."
    echo "  If that fails, check container logs and internet access."
    echo
    echo "Home Assistant Core venv:"
    echo "  Activate the venv, then run:"
    echo "    pip install meshtastic==2.7.11 meshcore==2.3.7"
    echo
    echo "Local CLI testing outside HA:"
    echo "  python3 -m pip install --user meshtastic==2.7.11 meshcore==2.3.7"
  } >"${out}"
  log "Wrote Python package notes to ${out}"
}

backup_existing_component() {
  local target="$1"
  local backup="${OUTPUT_DIR}/backup-custom_components-meshnet-${TIMESTAMP}"
  if [[ -e "${target}" ]]; then
    log "Existing MeshNet component found at ${target}"
    log "Backup location will be ${backup}"
    if [[ "${DRY_RUN}" -eq 1 ]]; then
      log "DRY RUN: would copy ${target} to ${backup}"
      return
    fi
    cp -a "${target}" "${backup}"
    log "Backed up existing component to ${backup}"
  fi
}

install_custom_component() {
  [[ "${INSTALL_COMPONENT}" -eq 1 ]] || return 0
  if [[ -z "${CONFIG_DIR}" ]]; then
    log "Cannot install custom component because no Home Assistant config directory was selected."
    return 0
  fi
  local source="${SCRIPT_DIR}/custom_components/meshnet"
  local target="${CONFIG_DIR}/custom_components/meshnet"
  [[ -d "${source}" ]] || die "Source custom component not found: ${source}"
  if ! ask_yes_no "Install/update MeshNet custom component at ${target}?"; then
    log "Skipped custom component install."
    return 0
  fi
  backup_existing_component "${target}"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    log "DRY RUN: would install ${source} to ${target}"
    return 0
  fi
  mkdir -p "${CONFIG_DIR}/custom_components"
  rm -rf "${target}.tmp"
  cp -a "${source}" "${target}.tmp"
  if [[ -e "${target}" ]]; then
    rm -rf "${target}"
  fi
  mv "${target}.tmp" "${target}"
  COMPONENT_INSTALLED=1
  log "Installed MeshNet custom component to ${target}"
}

write_next_steps() {
  local out="${OUTPUT_DIR}/NEXT_STEPS.txt"
  {
    echo "Next steps"
    echo "=========="
    echo
    echo "1. Read docs/INSTALL.md."
    echo "2. Review generated_config.yaml."
    echo "3. Install the custom component if you did not pass --install-custom-component:"
    echo "   cp -a custom_components/meshnet <HA_CONFIG>/custom_components/meshnet"
    echo "4. Restart Home Assistant."
    echo "5. Add MeshNet from Settings -> Devices & Services and use the guided gateway forms."
    echo "6. Advanced alternative: put generated_config.yaml in configuration.yaml before"
    echo "   creating the UI entry. Do not use UI setup and YAML import at the same time."
    if [[ -n "${CONFIG_DIR}" ]]; then
      echo "7. Run ./verify_setup.sh --output-dir '${OUTPUT_DIR}' --config-dir '${CONFIG_DIR}'"
    else
      echo "7. Run ./verify_setup.sh --output-dir '${OUTPUT_DIR}' --config-dir <HA_CONFIG>"
    fi
    echo
    echo "Files generated:"
    echo "  ${OUTPUT_DIR}/detected_environment.txt"
    echo "  ${OUTPUT_DIR}/detected_serial_devices.txt"
    echo "  ${OUTPUT_DIR}/generated_config.yaml"
    echo "  ${OUTPUT_DIR}/generated_automations.yaml"
    echo "  ${OUTPUT_DIR}/generated_dashboard.yaml"
    echo "  ${OUTPUT_DIR}/install_log.txt"
    echo "  ${OUTPUT_DIR}/rollback_info.json"
  } >"${out}"
  log "Wrote next steps to ${out}"
}

main() {
  parse_args "$@"
  need_linux
  prepare_output
  choose_config_dir
  detect_install_type
  check_ha_version
  detect_serial_devices
  resolve_meshcore_ports
  run_connectivity_tests
  write_gateway_yaml
  write_automations_yaml
  write_dashboard_yaml
  install_python_notes
  write_rollback_json
  install_custom_component
  write_rollback_json
  write_next_steps
  chmod -R go-rwx "${OUTPUT_DIR}"
  log
  log "Done."
  log "Open this file next: ${OUTPUT_DIR}/NEXT_STEPS.txt"
}

main "$@"
