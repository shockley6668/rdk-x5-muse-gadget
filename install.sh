#!/bin/bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run with sudo bash install.sh'; exit 1; }
source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
target_dir=/opt/muse-ui
install_pet_vision="${INSTALL_PET_VISION:-0}"
install_pet_camera="${INSTALL_PET_CAMERA:-0}"
install_pet_preview="${INSTALL_PET_PREVIEW:-0}"
install_pet_depth="${INSTALL_PET_DEPTH:-0}"
install_pet_runtime="${INSTALL_PET_RUNTIME:-$install_pet_depth}"
case "$install_pet_runtime" in
    0|1) ;;
    *) echo 'INSTALL_PET_RUNTIME must be 0 or 1.' >&2; exit 2 ;;
esac
if [[ "$install_pet_runtime" == 1 ]]; then
    install_pet_vision=1
    install_pet_preview=1
    install_pet_camera=0
fi
case "$install_pet_depth" in
    0|1) ;;
    *) echo 'INSTALL_PET_DEPTH must be 0 or 1.' >&2; exit 2 ;;
esac
[[ "$install_pet_depth" == 0 ]] || install_pet_preview=1
case "$install_pet_preview" in
    0|1) ;;
    *) echo 'INSTALL_PET_PREVIEW must be 0 or 1.' >&2; exit 2 ;;
esac
case "$install_pet_camera" in
    0|1) ;;
    *) echo 'INSTALL_PET_CAMERA must be 0 or 1.' >&2; exit 2 ;;
esac
case "$install_pet_vision" in
    0|1) ;;
    *) echo 'INSTALL_PET_VISION must be 0 or 1.' >&2; exit 2 ;;
esac
[[ "$install_pet_preview" == 0 ]] || install_pet_vision=1
[[ -r /var/lib/musegadget/pairing.json ]] || { echo 'Pair the Linux Muse SDK first.'; exit 1; }
id sunrise >/dev/null
export DEBIAN_FRONTEND=noninteractive
apt-get install -y --no-install-recommends libsdl2-dev libx11-dev ninja-build build-essential pkg-config cmake python3-venv python3-cryptography ffmpeg pulseaudio-utils xdotool x11-utils
if [[ "$install_pet_preview" == 1 ]]; then
    apt-get install -y --no-install-recommends python3-opencv
fi
mkdir -p "$target_dir"
if [[ "$source_dir" != "$target_dir" ]]; then
    tar -C "$source_dir" --exclude="./.git" --exclude="./build" --exclude="./venv" --exclude="./.venv" --exclude="./cad" --exclude="./.vscode" --exclude="./ros2_ws/build" --exclude="./ros2_ws/install" --exclude="./ros2_ws/log" --exclude="__pycache__" -cf - . | tar -C "$target_dir" -xf -
fi
python3 -m venv --system-site-packages "$target_dir/venv"
"$target_dir/venv/bin/python" -m pip install 'websockets==16.0' 'edge-tts==7.3.1'
cmake -S "$target_dir/simulator" -B "$target_dir/build" -G Ninja -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF
cmake --build "$target_dir/build" --parallel 4
cd "$target_dir"
PYTHONPATH="$target_dir/sdk" "$target_dir/venv/bin/python" -m unittest test_backend -v
PYTHONPATH="$target_dir/sdk" "$target_dir/venv/bin/python" -m unittest discover -s tests -v
chmod 755 start-display.sh launch-display.py send.py petctl.py scripts/petctl scripts/start-vision.sh scripts/start-camera.sh scripts/start-preview.sh scripts/start-depth.sh scripts/start-depth-controller.sh scripts/run-depth.py scripts/depth-session.py
install -m 755 scripts/petctl /usr/local/bin/petctl
install -d -o sunrise -g sunrise /home/sunrise/.local/share/applications /home/sunrise/.config/autostart
install -m 644 -o sunrise -g sunrise muse-ui.desktop /home/sunrise/.local/share/applications/muse-ui.desktop
install -m 644 -o sunrise -g sunrise muse-ui-autostart.desktop /home/sunrise/.config/autostart/muse-ui.desktop
if [[ -d /home/sunrise/Desktop ]]; then
    install -m 755 -o sunrise -g sunrise muse-ui.desktop /home/sunrise/Desktop/Muse.desktop
fi
install -m 644 muse-ui.service muse-ui-display.service muse-pet-vision.service muse-pet-camera.service muse-pet-preview.service muse-pet-depth.service muse-pet-depth-pipeline.service muse-vision-runtime.service /etc/systemd/system/
install -d -m 755 /etc/default
if [[ ! -e /etc/default/muse-pet-vision ]]; then
    install -m 644 muse-pet-vision.env /etc/default/muse-pet-vision
fi
if [[ ! -e /etc/default/muse-pet-camera ]]; then
    install -m 644 muse-pet-camera.env /etc/default/muse-pet-camera
fi
if [[ "$install_pet_runtime" == 1 ]]; then
    python3 scripts/select-pet-transport.py --mode shm
    install -d /etc/systemd/system/muse-pet-vision.service.d
    cat > /etc/systemd/system/muse-pet-vision.service.d/30-unified-runtime.conf <<'CONF'
[Unit]
Wants=muse-vision-runtime.service
After=muse-vision-runtime.service
[Service]
Environment=MUSE_PET_UNIFIED_RUNTIME=1
CONF
    install -d /etc/systemd/system/muse-pet-preview.service.d
    cat > /etc/systemd/system/muse-pet-preview.service.d/30-panorama.conf <<'CONF'
[Service]
Environment=MUSE_PET_PANORAMA=1
CONF
fi
install -d -m 700 -o sunrise -g sunrise /home/sunrise/.config/pulse
if [[ -f /home/sunrise/.config/pulse/default.pa ]] && ! grep -q 'sink_name=muse_output' /home/sunrise/.config/pulse/default.pa; then
    cp -an /home/sunrise/.config/pulse/default.pa /home/sunrise/.config/pulse/default.pa.before-muse
    sed 's|.include /etc/pulse/default.pa|.include /home/sunrise/.config/pulse/default.pa.before-muse|' pulse-default.pa > /home/sunrise/.config/pulse/default.pa
    chown sunrise:sunrise /home/sunrise/.config/pulse/default.pa
elif [[ ! -f /home/sunrise/.config/pulse/default.pa ]]; then
    install -m 644 -o sunrise -g sunrise pulse-default.pa /home/sunrise/.config/pulse/default.pa
fi
systemctl --user --machine=sunrise@.host restart pulseaudio.service
systemctl daemon-reload
systemctl enable muse-ui.service
systemctl disable --now muse-ui-display.service
systemctl restart muse-ui.service
if [[ "$install_pet_camera" == 1 ]]; then
    systemctl enable muse-pet-camera.service
    systemctl restart muse-pet-camera.service
fi
if [[ "$install_pet_vision" == 1 ]]; then
    tros_setup="${TROS_SETUP:-/opt/tros/setup.bash}"
    if [[ ! -r "$tros_setup" && -z "${TROS_SETUP:-}" ]]; then
        tros_setup=/opt/tros/humble/setup.bash
    fi
    [[ -r "$tros_setup" ]] || {
        echo 'TROS setup.bash was not found. See docs/PET-DEMO.md.' >&2
        exit 1
    }
    [[ -r "$target_dir/ros2_ws/src/muse_pet_vision/package.xml" ]] || {
        echo 'The muse_pet_vision ROS package is missing.' >&2
        exit 1
    }
    (
        set +u
        # shellcheck source=/dev/null
        source "$tros_setup"
        set -u
        command -v colcon >/dev/null || {
            echo 'colcon is missing; install python3-colcon-common-extensions.' >&2
            exit 1
        }
        cd "$target_dir/ros2_ws"
        packages=(muse_pet_camera_throttle muse_pet_vision)
        [[ "$install_pet_runtime" != 1 ]] || packages+=(muse_pet_runtime)
        colcon build --merge-install --packages-select "${packages[@]}" --cmake-args -DCMAKE_BUILD_TYPE=Release
    )
    if [[ "$install_pet_runtime" == 1 ]]; then
        systemctl disable --now muse-pet-camera muse-pet-depth muse-pet-depth-pipeline || true
        systemctl enable --now muse-vision-runtime
    fi
    systemctl enable muse-pet-vision.service
    systemctl restart muse-pet-vision.service
fi
if [[ "$install_pet_preview" == 1 ]]; then
    systemctl enable muse-pet-preview.service
    systemctl restart muse-pet-preview.service
fi
runuser -u sunrise -- pkill -TERM -x muse_simulator || true
runuser -u sunrise -- env DISPLAY=:0 XAUTHORITY=/home/sunrise/.Xauthority XDG_RUNTIME_DIR=/run/user/1000 sh -c 'nohup /opt/muse-ui/start-display.sh >/dev/null 2>&1 </dev/null &'
systemctl is-active muse-ui.service
