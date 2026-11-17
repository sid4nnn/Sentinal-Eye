#!/bin/bash
set -euo pipefail
umask 077
cd -- "$(dirname -- "$0")"

address=${1:-}
if [[ -z "$address" ]]; then
    read -r -p 'Jetson IPv4 address [192.168.0.29]: ' address
    address=${address:-192.168.0.29}
fi
if ! python3 -c 'from ipaddress import IPv4Address; import sys; IPv4Address(sys.argv[1])' "$address" 2>/dev/null; then
    printf 'Enter a valid IPv4 address.\n' >&2
    exit 1
fi

target="admin@$address"
scratch=$(mktemp -d /tmp/sentinal-install.XXXXXXXX)
control="$scratch/ssh"
remote_stage=
cleanup() {
    if [[ -n "$remote_stage" ]]; then
        ssh -n -S "$control" -o BatchMode=yes "$target" "rm -rf -- '$remote_stage'" >/dev/null 2>&1 || true
    fi
    ssh -S "$control" -O exit "$target" >/dev/null 2>&1 || true
    rm -rf -- "$scratch"
}
trap cleanup EXIT

tar -cf "$scratch/release.tar" sentinal_gui.py live_insightface.py dan_emotions.py run.sh gui jetson
ssh -M -N -f -S "$control" -o ControlPersist=no -o ConnectTimeout=8 -o ForwardAgent=no "$target"
remote_stage=$(ssh -S "$control" "$target" 'mktemp -d /tmp/sentinal-eye-install.XXXXXXXX')
if [[ ! "$remote_stage" =~ ^/tmp/sentinal-eye-install\.[a-zA-Z0-9]{8}$ ]]; then
    remote_stage=
    printf 'Could not create the installation folder on the Jetson.\n' >&2
    exit 1
fi
ssh -S "$control" "$target" "tar -xf - -C '$remote_stage'" < "$scratch/release.tar"
ssh -tt -S "$control" "$target" "bash '$remote_stage/jetson/install.sh' '$address'"

settings="$HOME/Library/Application Support/Sentinal Eye"
access="$settings/$address"
mkdir -p -- "$access"
chmod 700 "$access"
scp -q -o "ControlPath=$control" "$target:sentinal-eye/.panel/ca.cer" "$access/SentinalEyeCA.cer"
scp -q -o "ControlPath=$control" "$target:sentinal-eye/.panel/password" "$access/panel-password.txt"
chmod 600 "$access/panel-password.txt"
python3 - "$access" <<'PY'
import plistlib
from pathlib import Path
import sys
from uuid import uuid4

path = Path(sys.argv[1])
profile = {
    "PayloadDisplayName": "Sentinal Eye HTTPS",
    "PayloadIdentifier": "sentinal-eye.local.https",
    "PayloadType": "Configuration",
    "PayloadUUID": str(uuid4()),
    "PayloadVersion": 1,
    "PayloadContent": [{
        "PayloadCertificateFileName": "SentinalEyeCA.cer",
        "PayloadContent": (path / "SentinalEyeCA.cer").read_bytes(),
        "PayloadDisplayName": "Sentinal Eye Setup CA",
        "PayloadIdentifier": "sentinal-eye.local.https.ca",
        "PayloadType": "com.apple.security.root",
        "PayloadUUID": str(uuid4()),
        "PayloadVersion": 1,
    }],
}
with (path / "SentinalEye.mobileconfig").open("wb") as file:
    plistlib.dump(profile, file)
PY

printf 'https://%s:8750/\n' "$address" > "$settings/panel-url"
printf '\nOpen https://%s:8750/ from a device on the same network.\n' "$address"
printf 'User: admin\nPanel password file: %s/panel-password.txt\n' "$access"
printf 'iPad profile: %s/SentinalEye.mobileconfig\n' "$access"
/usr/bin/open "$access" || true
