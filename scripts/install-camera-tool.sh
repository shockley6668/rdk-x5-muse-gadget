#!/bin/bash
set -euo pipefail
[[ $(id -u) -eq 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
[[ -x /opt/musegadget/venv/bin/python && -f /opt/muse-ui/camera_tool.py && \
   -f /opt/muse-ui/scripts/muse_camera_daemon.py ]] || {
    echo 'Install the Muse Gadget SDK and Muse UI first.' >&2; exit 1;
}
install -d -m 755 /etc/systemd/system/musegadget.service.d
cat > /etc/systemd/system/musegadget.service.d/60-camera.conf <<'CONF'
[Service]
ExecStart=
ExecStart=/opt/musegadget/venv/bin/python /opt/muse-ui/scripts/muse_camera_daemon.py
CONF
systemctl daemon-reload
systemctl restart musegadget.service
