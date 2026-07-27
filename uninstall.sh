#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
METADATA="${SCRIPT_DIR}/ha-mesh-setup-output/rollback_info.json"
YES=0
DRY_RUN=0

usage() {
  cat <<'EOF'
Uninstall helper for files installed by setup.sh.

Usage:
  ./uninstall.sh
  ./uninstall.sh --metadata ./ha-mesh-setup-output/rollback_info.json
  ./uninstall.sh --dry-run
  ./uninstall.sh --yes

This script only removes the custom component path recorded by setup.sh.
It does not edit configuration.yaml, automations.yaml, dashboards, or Home Assistant storage.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help) usage; exit 0 ;;
    --metadata) METADATA="$2"; shift 2 ;;
    --yes) YES=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "Unknown option: $1"; usage; exit 1 ;;
  esac
done

log() { printf '%s\n' "$*"; }

json_value() {
  local key="$1"
  python3 - "$METADATA" "$key" <<'PY' 2>/dev/null || true
import json, sys
path, key = sys.argv[1], sys.argv[2]
with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)
value = data.get(key, "")
print(value if value is not None else "")
PY
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

[[ -f "${METADATA}" ]] || {
  log "Metadata not found: ${METADATA}"
  log "Nothing removed. Pass --metadata PATH if rollback_info.json is elsewhere."
  exit 1
}

COMPONENT_PATH="$(json_value custom_component_path)"
BACKUP_PATH="$(json_value backup_path)"
CONFIG_DIR="$(json_value config_dir)"
OUTPUT_DIR="$(json_value output_dir)"
INSTALLED="$(json_value custom_component_installed_by_setup)"

log "Metadata: ${METADATA}"
log "Recorded component path: ${COMPONENT_PATH:-none}"
log "Recorded backup path: ${BACKUP_PATH:-none}"

validate_component_path() {
  python3 - "$1" "$2" <<'PY'
import json
import os
from pathlib import Path
import sys

component = Path(os.path.abspath(sys.argv[1]))
config_dir = Path(os.path.abspath(sys.argv[2]))
expected = config_dir / "custom_components" / "meshnet"

if component != expected:
    raise SystemExit(
        "refusing path that is not exactly <config_dir>/custom_components/meshnet"
    )
if component.name != "meshnet" or component.parent.name != "custom_components":
    raise SystemExit("refusing path without the expected custom_components/meshnet suffix")
if component == Path("/") or config_dir == Path("/"):
    raise SystemExit("refusing a root-level uninstall target")
if component.is_symlink() or component.parent.is_symlink():
    raise SystemExit("refusing a symlinked component or custom_components directory")

manifest = component / "manifest.json"
try:
    data = json.loads(manifest.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as err:
    raise SystemExit(f"refusing target without a readable manifest.json: {err}") from err
if data.get("domain") != "meshnet":
    raise SystemExit("refusing target whose manifest domain is not meshnet")

print(component)
PY
}

validate_backup_path() {
  python3 - "$1" "$2" <<'PY'
import json
from pathlib import Path
import sys

backup_input = Path(sys.argv[1])
if backup_input.is_symlink():
    raise SystemExit("backup path is a symlink")
backup = backup_input.resolve(strict=False)
output_dir = Path(sys.argv[2]).resolve(strict=False)
if backup.parent != output_dir:
    raise SystemExit("backup is not directly inside the recorded output directory")
if not backup.name.startswith("backup-custom_components-meshnet-"):
    raise SystemExit("backup does not have the expected MeshNet backup name")

manifest = backup / "manifest.json"
try:
    data = json.loads(manifest.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as err:
    raise SystemExit(f"backup has no readable manifest.json: {err}") from err
if data.get("domain") != "meshnet":
    raise SystemExit("backup manifest domain is not meshnet")

print(backup)
PY
}

if [[ "${INSTALLED}" != "1" && "${INSTALLED}" != "True" && "${INSTALLED}" != "true" ]]; then
  log "Metadata says setup.sh did not install the custom component."
  log "Nothing removed."
  exit 0
fi

if [[ -z "${COMPONENT_PATH}" || ! -e "${COMPONENT_PATH}" ]]; then
  log "Component path does not exist. Nothing to remove."
  exit 0
fi

if [[ -z "${CONFIG_DIR}" ]]; then
  log "ERROR: metadata has no Home Assistant config directory. Nothing removed."
  exit 1
fi

if ! COMPONENT_PATH="$(validate_component_path "${COMPONENT_PATH}" "${CONFIG_DIR}")"; then
  log "ERROR: uninstall target validation failed. Nothing removed."
  exit 1
fi

log
log "WARNING: this will remove:"
log "  ${COMPONENT_PATH}"
log "It will not remove generated files or Home Assistant entity history."

if ! ask_yes_no "Continue with uninstall?"; then
  log "Uninstall cancelled."
  exit 0
fi

if [[ "${DRY_RUN}" -eq 1 ]]; then
  log "DRY RUN: would remove ${COMPONENT_PATH}"
else
  rm -rf -- "${COMPONENT_PATH}"
  log "Removed ${COMPONENT_PATH}"
fi

if [[ -n "${BACKUP_PATH}" && -e "${BACKUP_PATH}" ]]; then
  if [[ -z "${OUTPUT_DIR}" ]] || ! BACKUP_PATH="$(validate_backup_path "${BACKUP_PATH}" "${OUTPUT_DIR}")"; then
    log
    log "WARNING: recorded backup is not a validated MeshNet component; it will not be restored."
    BACKUP_PATH=""
  fi
fi

if [[ -n "${BACKUP_PATH}" && -e "${BACKUP_PATH}" ]]; then
  log
  log "A backup exists at:"
  log "  ${BACKUP_PATH}"
  if ask_yes_no "Restore this backup to ${COMPONENT_PATH}?"; then
    if [[ "${DRY_RUN}" -eq 1 ]]; then
      log "DRY RUN: would restore ${BACKUP_PATH} to ${COMPONENT_PATH}"
    else
      mkdir -p "$(dirname "${COMPONENT_PATH}")"
      cp -a "${BACKUP_PATH}" "${COMPONENT_PATH}"
      log "Restored backup."
    fi
  fi
fi

log
log "Restart Home Assistant after uninstalling."
