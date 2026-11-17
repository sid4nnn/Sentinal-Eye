#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
python="$PWD/.venv/bin/python"
if [[ ! -x "$python" ]]; then
    printf 'Missing Python environment at %s\n' "$python" >&2
    exit 1
fi

if [[ ${1:-} == --preview ]]; then
    shift
    export DISPLAY="${DISPLAY:-:1}"
    export XAUTHORITY="${XAUTHORITY:-/run/user/$(id -u)/gdm/Xauthority}"
else
    unset DISPLAY XAUTHORITY WAYLAND_DISPLAY
    case "${1:-}" in
        --web) shift; set -- --web-port 8765 "$@" ;;
        --headless) ;;
        *) set -- --headless "$@" ;;
    esac
fi

exec "$python" -u live_insightface.py --device cuda --model buffalo_m --people-dir sentinal-people "$@"
