#!/bin/bash
set -euo pipefail
[[ $(id -u) -eq 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
repo=$(cd "$(dirname "$0")/.." && pwd)
app=/opt/muse-ui
assets="$app/wakeword-assets"
model=sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20
[[ -x "$app/venv/bin/python" && -f "$app/backend.py" ]] || {
    echo 'Install Muse UI first.' >&2; exit 1;
}
grep -q 'async def realtime_call(' "$app/backend.py" || {
    echo 'Update the Muse UI installation before enabling wake word.' >&2; exit 1;
}
bash "$repo/scripts/install-native-call.sh"
install -d -m 755 "$assets"
if [[ ! -f "$assets/$model/tokens.txt" ]]; then
    archive=$(mktemp)
    trap 'rm -f "$archive"' EXIT
    curl -fL --connect-timeout 15 --max-time 180 \
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/$model.tar.bz2" -o "$archive"
    tar -xjf "$archive" -C "$assets"
fi
ln -sfn "$model" "$assets/model"
printf '%s\n' 'HH AH0 L OW1 M Y UW1 Z @HELLO_MUSE' > "$assets/model/hello-muse.txt"
"$app/venv/bin/pip" install 'sherpa-onnx==1.13.8'
if [[ "$repo" != "$app" ]]; then
    install -m 755 "$repo/wakeword.py" "$app/wakeword.py"
fi
install -m 644 "$repo/muse-wakeword.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable muse-wakeword.service
systemctl restart muse-ui.service muse-wakeword.service
