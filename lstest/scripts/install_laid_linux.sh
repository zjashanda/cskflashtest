#!/usr/bin/env bash
set -euo pipefail

BEGIN_MARKER="# >>> laid >>>"
END_MARKER="# <<< laid <<<"

read -r -d '' LAID_BLOCK <<'EOF' || true
# >>> laid >>>
laid_stream_channels() {
  local card="$1"
  local section="$2"
  local file value

  for file in /proc/asound/card"${card}"/stream*; do
    [ -f "$file" ] || continue
    value="$(
      awk -v section="$section" '
        BEGIN { in_section = 0; max_channels = 0 }
        /^[A-Za-z][A-Za-z ]*:/ {
          if ($0 == section ":") {
            in_section = 1
            next
          }
          if (in_section) {
            in_section = 0
          }
        }
        in_section && $1 == "Channels:" {
          if (($2 + 0) > max_channels) {
            max_channels = $2 + 0
          }
        }
        END {
          if (max_channels > 0) {
            print max_channels
          }
        }
      ' "$file"
    )"
    if [ -n "$value" ]; then
      printf '%s\n' "$value"
      return 0
    fi
  done

  printf '?\n'
}

laid_sanitize_token() {
  printf '%s\n' "$1" | tr '[:lower:]' '[:upper:]' | sed -E 's/[^A-Z0-9]+/_/g; s/^_+//; s/_+$//'
}

laid_compact_token() {
  local token
  token="$(laid_sanitize_token "$1")"
  if [ -z "$token" ]; then
    return 0
  fi

  case "$token" in
    *_USB_*)
      token="${token#*_USB_}"
      token="USB_${token}"
      ;;
  esac

  if [[ "$token" =~ ^([A-Z0-9]{4,})_0_([A-Z0-9]{2,})$ ]]; then
    token="${BASH_REMATCH[1]}_${BASH_REMATCH[2]}"
  fi

  printf '%s\n' "$token"
}

laid_read_text_if_exists() {
  local path="$1"
  [ -f "$path" ] || return 0
  sed -n '1{s/[[:space:]]*$//;p;q;}' "$path"
}

laid_derive_token_from_sysfs_path() {
  local resolved_path="$1"
  local token=""
  local part sanitized

  IFS='/' read -r -a parts <<< "$resolved_path"
  for part in "${parts[@]}"; do
    if [[ "$part" =~ ^[0-9]+-[0-9.]+(:[0-9.]+)?$ ]]; then
      sanitized="$(laid_compact_token "$part")"
      if [ -n "$sanitized" ]; then
        if [ -n "$token" ]; then
          token="${token}_${sanitized}"
        else
          token="$sanitized"
        fi
      fi
    fi
  done

  printf '%s\n' "$token"
}

laid_identity() {
  local card="$1"
  local dev="/dev/snd/controlC${card}"
  local props="" vid="" pid="" token=""
  local sysfs_path resolved current maybe_vid maybe_pid maybe_serial

  if command -v udevadm >/dev/null 2>&1; then
    props="$(udevadm info -q property -n "$dev" 2>/dev/null || true)"
  fi

  if [ -n "$props" ]; then
    vid="$(printf '%s\n' "$props" | awk -F= '/^ID_VENDOR_ID=/{print toupper($2); exit}')"
    pid="$(printf '%s\n' "$props" | awk -F= '/^ID_MODEL_ID=/{print toupper($2); exit}')"
    token="$(printf '%s\n' "$props" | awk -F= '/^ID_PATH_TAG=/{print $2; exit}')"
    if [ -z "$token" ]; then
      token="$(printf '%s\n' "$props" | awk -F= '/^ID_SERIAL_SHORT=/{print $2; exit}')"
    fi
    if [ -n "$token" ]; then
      token="$(laid_compact_token "$token")"
    fi
  fi

  sysfs_path="/sys/class/sound/card${card}/device"
  if [ -e "$sysfs_path" ]; then
    resolved="$(readlink -f "$sysfs_path" 2>/dev/null || true)"
    if [ -n "$resolved" ]; then
      current="$resolved"
      while [ -n "$current" ] && [ "$current" != "/" ]; do
        if [ -z "$vid" ]; then
          maybe_vid="$(laid_read_text_if_exists "$current/idVendor")"
          maybe_pid="$(laid_read_text_if_exists "$current/idProduct")"
          if [ -n "$maybe_vid" ] && [ -n "$maybe_pid" ]; then
            vid="$(printf '%s\n' "$maybe_vid" | tr '[:lower:]' '[:upper:]')"
            pid="$(printf '%s\n' "$maybe_pid" | tr '[:lower:]' '[:upper:]')"
          fi
        fi
        if [ -z "$token" ]; then
          maybe_serial="$(laid_read_text_if_exists "$current/serial")"
          if [ -n "$maybe_serial" ]; then
            token="$(laid_compact_token "$maybe_serial")"
          fi
        fi
        current="$(dirname "$current")"
      done
      if [ -z "$token" ]; then
        token="$(laid_compact_token "$(laid_derive_token_from_sysfs_path "$resolved")")"
      fi
    fi
  fi

  if [ -z "$vid" ] || [ -z "$pid" ]; then
    return 1
  fi
  if [ -z "$token" ]; then
    token="CARD${card}"
  fi

  printf '%s|%s|%s\n' "$vid" "$pid" "$token"
}

laid() {
  local found=0
  local dev identity vid pid token card name key playback_channels capture_channels

  printf '%-9s %-64s %-10s %-8s %s\n' "Direction" "DeviceKey" "Card" "Channels" "Name"
  for dev in /dev/snd/controlC*; do
    [ -e "$dev" ] || continue

    card="${dev##*controlC}"
    identity="$(laid_identity "$card" || true)"
    [ -n "$identity" ] || continue
    IFS='|' read -r vid pid token <<< "$identity"
    name="$(cat "/proc/asound/card${card}/id" 2>/dev/null || true)"
    key="VID_${vid}&PID_${pid}:${token}"
    playback_channels="$(laid_stream_channels "$card" "Playback")"
    capture_channels="$(laid_stream_channels "$card" "Capture")"

    if [ "$playback_channels" != "?" ]; then
      printf '%-9s %-64s %-10s %-8s %s\n' "Render" "$key" "card${card}" "$playback_channels" "${name:-unknown}"
      found=1
    fi
    if [ "$capture_channels" != "?" ]; then
      printf '%-9s %-64s %-10s %-8s %s\n' "Capture" "$key" "card${card}" "$capture_channels" "${name:-unknown}"
      found=1
    fi
    if [ "$playback_channels" = "?" ] && [ "$capture_channels" = "?" ]; then
      printf '%-9s %-64s %-10s %-8s %s\n' "Unknown" "$key" "card${card}" "?" "${name:-unknown}"
      found=1
    fi
  done

  if [ "$found" -eq 0 ]; then
    printf 'No active USB audio cards found.\n'
  fi
}
# <<< laid <<<
EOF

update_rc_file() {
  local target="$1"
  local tmp

  mkdir -p "$(dirname "$target")"
  [ -f "$target" ] || : > "$target"
  tmp="$(mktemp)"

  awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" '
    $0 == begin { skip=1; next }
    $0 == end   { skip=0; next }
    !skip       { print }
  ' "$target" > "$tmp"

  {
    cat "$tmp"
    if [ -s "$tmp" ]; then
      printf '\n'
    fi
    printf '%s\n' "$LAID_BLOCK"
  } > "$target"

  rm -f "$tmp"
  printf 'laid installed to: %s\n' "$target"
}

if [ "$#" -eq 0 ]; then
  update_rc_file "$HOME/.bashrc"
  update_rc_file "$HOME/.zshrc"
else
  for shell_name in "$@"; do
    case "$shell_name" in
      bash)
        update_rc_file "$HOME/.bashrc"
        ;;
      zsh)
        update_rc_file "$HOME/.zshrc"
        ;;
      *)
        printf 'Unsupported shell target: %s\n' "$shell_name" >&2
        exit 1
        ;;
    esac
  done
fi

printf 'Open a new shell, or run: source ~/.bashrc / source ~/.zshrc\n'
