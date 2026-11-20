#!/usr/bin/env bash
set -euo pipefail
[[ $EUID == 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
app=/opt/muse-ui
repo=$(cd "$(dirname "$0")/.." && pwd)
rev=08e59803b7606a0ba585238ad7e701a058072dfa
sdk="$app/call-sdk"
apt-get install -y --no-install-recommends git libportaudio2 libasound2-plugins
"$app/venv/bin/pip" install 'sounddevice>=0.5,<1' 'numpy<2'
install -d "$sdk/empty-cadir"
if [[ ! -d "$sdk/$rev/musegadget" ]]; then
    tmp=$(mktemp -d "$sdk/.download.XXXXXX")
    trap 'rm -rf -- "$tmp"' EXIT
    git -C "$tmp" init --quiet
    git -C "$tmp" fetch --quiet --depth=1 --filter=blob:none https://github.com/facebookincubator/muse-gadget-sdk.git "$rev"
    mkdir "$tmp/release"
    git -C "$tmp" archive "$rev:linux/src" musegadget | tar -xC "$tmp/release"
    git -C "$tmp" show "$rev:LICENSE" > "$tmp/release/LICENSE"
    mv "$tmp/release" "$sdk/$rev"
fi
ln -sfnT "$rev/musegadget" "$sdk/.musegadget.next"
mv -Tf "$sdk/.musegadget.next" "$sdk/musegadget"
if [[ "$repo" != "$app" ]]; then
    install -m 644 "$repo/native_call.py" "$repo/call_chat.py" "$app/"
fi
