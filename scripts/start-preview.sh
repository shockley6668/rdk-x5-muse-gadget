#!/bin/bash
set -eo pipefail
tros_setup="${TROS_SETUP:-/opt/tros/setup.bash}"
if [[ ! -r "$tros_setup" && -z "${TROS_SETUP:-}" ]]; then
    tros_setup=/opt/tros/humble/setup.bash
fi
overlay_setup="${MUSE_PET_VISION_SETUP:-/opt/muse-ui/ros2_ws/install/setup.bash}"
[[ -r "$tros_setup" && -r "$overlay_setup" ]] || {
    echo 'TROS or Muse ROS overlay is missing.' >&2; exit 1;
}
source "$tros_setup"
source "$overlay_setup"
source "$(dirname "${BASH_SOURCE[0]}")/ros-transport.sh"
set -u
left_topic="${MUSE_PET_LEFT_TOPIC:-/image_left_raw}"
case "${MUSE_PET_THROTTLE:-true}" in
    true|1) left_topic=/muse/camera/preview ;;
esac
if [[ "$MUSE_PET_TRANSPORT" == shm ]]; then
    case "${MUSE_PET_THROTTLE:-true}" in
        true|1)
            left_topic=/muse/camera/preview/hbmem
            if [[ "${MUSE_PET_PANORAMA:-0}" == 1 ]]; then
                left_topic=/muse/camera/panorama/preview/hbmem
            fi
            ;;
        *) echo 'Shared preview requires MUSE_PET_THROTTLE=true.' >&2; exit 2 ;;
    esac
fi
exec ros2 launch muse_pet_vision muse_pet_preview.launch.py \
    "left_topic:=$left_topic" \
    "image_transport:=$MUSE_PET_IMAGE_TRANSPORT" \
    "object_topic:=${MUSE_PET_OBJECT_TOPIC:-/muse/objects}" \
    "gesture_topic:=${MUSE_PET_GESTURE_TOPIC:-/muse/gestures}"
