#!/bin/bash
set -e
cd -- "$(dirname -- "$0")"
panel_url="$HOME/Library/Application Support/Sentinal Eye/panel-url"
if [[ -s "$panel_url" ]]; then
    IFS= read -r url < "$panel_url"
    exec /usr/bin/open "$url"
fi
exec python3 sentinal_gui.py
