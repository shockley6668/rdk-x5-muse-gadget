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
grep -q 'action == "cancel" and self.recording' "$app/backend.py" || {
    echo 'Update the Muse UI installation before enabling wake word.' >&2; exit 1;
}
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
if [[ ! -s "$assets/model/silero_vad.onnx" ]]; then
    curl -fL --connect-timeout 15 --max-time 90 \
        https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx \
        -o "$assets/model/silero_vad.onnx"
fi
"$app/venv/bin/pip" install 'sherpa-onnx==1.13.8'
"$app/venv/bin/python" - "$assets/model/wake.wav" <<'PY'
import math, struct, sys, wave
rate = 16000
samples = [int(2500 * min(1, i / 160, (1600 - i) / 160) *
               math.sin(2 * math.pi * 800 * i / rate)) for i in range(1600)]
with wave.open(sys.argv[1], 'wb') as out:
    out.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
    out.writeframes(struct.pack('<' + 'h' * len(samples), *samples))
PY
install -m 755 "$repo/wakeword.py" "$app/wakeword.py"
install -m 644 "$repo/muse-wakeword.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now muse-wakeword.service
