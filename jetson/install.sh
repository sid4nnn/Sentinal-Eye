#!/usr/bin/env bash
set -euo pipefail
umask 077

address=${1:?Missing Jetson IP}
release=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
project=/home/admin/sentinal-eye
config="$project/.panel"

if [[ $(id -un) != admin || ! -x "$project/.venv/bin/python" ]]; then
    printf 'Install Sentinal Eye and its Python environment under %s first.\n' "$project" >&2
    exit 1
fi
if ! ip -4 -o address show | awk '{sub(/\/.*/, "", $4); print $4}' | grep -Fx -- "$address" >/dev/null; then
    printf 'The address %s is not configured on this Jetson.\n' "$address" >&2
    exit 1
fi

sudo -v
install -d -m 700 -- "$config"
if [[ ! -s "$config/password" ]]; then
    openssl rand -hex 24 > "$config/password"
fi

if [[ ! -s "$config/server.key" || ! -s "$config/ca.cer" ]] || ! openssl verify \
    -purpose sslserver -verify_ip "$address" -CAfile "$config/ca.crt" "$config/server.crt" >/dev/null 2>&1; then
    certs=$(mktemp -d "$config/.certs.XXXXXXXX")
    trap 'rm -rf -- "$certs"' EXIT
    openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 370 -subj '/CN=Sentinal Eye Setup CA' \
        -addext 'basicConstraints=critical,CA:TRUE,pathlen:0' -addext 'keyUsage=critical,keyCertSign,cRLSign' \
        -keyout "$certs/ca.key" -out "$certs/ca.crt" 2> "$certs/openssl.log" || { cat "$certs/openssl.log" >&2; exit 1; }
    openssl req -new -newkey rsa:2048 -nodes -sha256 -subj '/CN=Sentinal Eye Jetson' \
        -keyout "$certs/server.key" -out "$certs/server.csr" 2> "$certs/openssl.log" || { cat "$certs/openssl.log" >&2; exit 1; }
    cat > "$certs/server.ext" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=IP:$address
EOF
    openssl x509 -req -sha256 -days 365 -in "$certs/server.csr" -CA "$certs/ca.crt" -CAkey "$certs/ca.key" \
        -CAcreateserial -extfile "$certs/server.ext" -out "$certs/server.crt"
    openssl verify -purpose sslserver -verify_ip "$address" -CAfile "$certs/ca.crt" "$certs/server.crt" >/dev/null
    openssl x509 -in "$certs/ca.crt" -outform DER -out "$certs/ca.cer"
    for file in ca.crt ca.cer server.crt server.key; do
        mv -- "$certs/$file" "$config/$file"
    done
    rm -rf -- "$certs"
    trap - EXIT
fi
printf 'PANEL_IP=%s\n' "$address" > "$config/service.env"

install -d -- "$project/gui" "$project/sentinal-people"
install -m 644 -- "$release/sentinal_gui.py" "$release/live_insightface.py" "$release/dan_emotions.py" "$project/"
install -m 755 -- "$release/run.sh" "$project/run.sh"
install -m 644 -- "$release/gui/"* "$project/gui/"

sudo install -m 644 -- "$release/jetson/sentinal-eye.service" /etc/systemd/system/sentinal-eye.service
sudo systemctl daemon-reload
sudo systemctl enable sentinal-eye.service
sudo systemctl restart sentinal-eye.service

for attempt in {1..10}; do
    code=$(curl --silent --max-time 2 --noproxy '*' --cacert "$config/ca.crt" \
        --output /dev/null --write-out '%{http_code}' "https://$address:8750/" || true)
    if [[ "$code" == 303 ]]; then
        printf 'Sentinal Eye is listening at https://%s:8750/\n' "$address"
        exit 0
    fi
    sleep 1
done
printf 'The panel did not become ready. Run: sudo journalctl -u sentinal-eye.service -n 30\n' >&2
exit 1
