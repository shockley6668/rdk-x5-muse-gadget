# Pet demo

The demo separates capture, perception and display:

- `mipi_cam` publishes the GS130WI channels.
- `muse_pet_vision` consumes existing ROS topics and sends timestamped events
  to the pet state machine.
- `muse-ui` owns the pet state, touch input, Muse chat, captions and audio.
- `muse-pet-preview` encodes the left image as JPEG with the hardware codec,
  serves the TROS web viewer and produces the small-screen recognition image.

The perception launch does not start or stop the camera.

## Prerequisites

- RDK X5 with Ubuntu 22.04 and TROS Humble. Current images normally provide
  `/opt/tros/setup.bash`; `/opt/tros/humble/setup.bash` is also supported.
- A working GS130WI dual-channel launch.
- `python3-colcon-common-extensions` for the local ROS workspace.
- TROS `hbm_img_msgs` for the C++ image relay, including its shared-image mode.
- D-Robotics `mono2d_body_detection`, `hand_lmk_detection`,
  `hand_gesture_detection` and `dnn_node_example` for the full pipeline.

The repository does not redistribute or fetch model assets. Install the
versions that match the board image and TROS release, then use absolute paths
in `/etc/default/muse-pet-vision` when overriding package defaults. The launch
resolves installed assets and makes a separate YOLO config with absolute model
and label paths. X5 Model Zoo models use Bayes-e `.bin`; some TROS gesture
packages retain `.hbm` filenames for their packaged X5 models. Use the X5
package assets, rather than renaming or substituting an S-series model.

The official [gesture install guide](https://github.com/D-Robotics/hand_gesture_detection)
uses `sudo apt install -y tros-humble-hand-gesture-detection` for TROS Humble.
Check all four ROS packages with `ros2 pkg prefix <package>` before enabling
the complete pipeline. Match the TROS release to your board image.

## 1. Start the camera

```sh
source /opt/tros/setup.bash 2>/dev/null || source /opt/tros/humble/setup.bash
./scripts/start-camera.sh
```

This wraps the user's working dual-channel launch with 1088×1280 images,
`mipi_frame_ts_type:=realtime` and `dual_combine:=0`. Realtime timestamps are
required by the bridge's stale-frame checks; default sensor timestamps use a
different clock. Leave this terminal running.

Without a selected common profile, launchers use the existing UDP fallback
(`scripts/fastdds.xml`). The earlier cross-user setup did not deliver shared
frames; that does not establish that X5 cannot use shared images. The new mode
below uses a common profile and matching service UIDs. Use the same selected
profile for diagnostic subscribers:

```sh
source scripts/ros-transport.sh
```

### Shared image mode

This path was built and tested on X5 / RDK OS 3.5.0 / TROS Humble on 19 October
2026 with the real stereo camera and installed BPU models. It
follows Magicbox's `shared_mem` image path without launching Magicbox's extra
camera, face models or web server.

- Capture publishes `HbmMsg1080P` on `/hbmem_left_img` and `/hbmem_right_img`.
- The C++ relay limits model delivery to 10 FPS and previews to 8 FPS separately.
  Both limits are configurable up to 30. A faster preview does not raise model
  FPS. When preview FPS is no higher than model FPS, it selects model-input
  frames for exact overlay matching. Faster video can reuse the latest past
  detection for up to 150 ms; future and older results are omitted. This affects
  visualization only, not gesture events or pet actions.
  It borrows output buffers and copies only `data_size` active bytes, rather
  than assigning the whole 6.2 MB message array. Idle right-eye output is not copied.
- Body, landmark and YOLO nodes subscribe to `/muse/camera/left/hbmem` with
  shared input enabled. The codec reads `/muse/camera/preview/hbmem` only while
  a viewer requests it. Python receives compact health and AI results, not NV12.
- `fastdds-shm.xml` enables automatic data sharing, loans are enabled, and ROS
  traffic stays local by default. Unbounded AI messages retain normal transport.
  The relay exits with an error if its publisher cannot borrow messages.

After copying the updated source to `/opt/muse-ui` and rebuilding both ROS
packages, select the profile **on X5** with all three image services stopped:

```sh
sudo systemctl stop muse-pet-preview muse-pet-vision muse-pet-camera
sudo python3 /opt/muse-ui/scripts/select-pet-transport.py --mode shm
sudo systemctl daemon-reload
# Start only when ready to measure the new workload:
sudo systemctl start muse-pet-camera muse-pet-vision muse-pet-preview
```

The selector only writes `/etc/default/muse-pet-transport` and managed service
drop-ins; it never starts services. Camera, perception and codec run as root
to match the camera's current device access and buffer ownership. The desktop
UI remains with `sunrise`. Original settings are backed up once. To inspect
generated files without changing the machine, pass `--root /tmp/muse-profile`.

To return to the original path, stop all three services, run the selector with
`--mode udp`, reload systemd and start them together. This restores `sunrise`
for perception/preview and disables image loans. Do not mix modes across nodes.
Foreground runs use `MUSE_PET_TRANSPORT=shm` with the same UID and environment
for every image process. Scripts read the common config; an explicit mode wins.

The GS130WI profile uses `MUSE_PET_SHM_SUBSTREAM=true` with a 272×320
hardware VSE substream, one quarter of the main 1088×1280 image in each direction.
The relay reads `/sub_hbmem_left_img` and `/sub_hbmem_right_img`; models and
preview use the same coordinates. The earlier 544×640 stream was tested with
real hand landmarks, gesture labels and pet state changes on X5; the smaller
preset extends the range before near hands hit the ROI size limit. It reduces
detail for distant/small objects too. It is not a stereo calibration source.

The installed TROS 2.4.1 hand model filters expanded hand ROIs of 255 pixels
or larger. At full resolution this discarded nearby hands before producing
keypoints. The smaller stream avoids that limit at typical desk distances;
a hand filling most of the image can still be rejected. Change substream
dimensions for a different camera or viewing distance, preserving its aspect ratio.
The bridge also removes body keypoint groups before gesture classification.
Preview draws the 21 hand keypoints and finger connections when available.

Check relay logs for input/delivered/preview FPS, plus CPU, temperature and BPU
usage before and after the switch. Avoid adding Python full-image subscribers
to the benchmark: they change the workload being measured. A successful loan
check does not prove end-to-end zero-copy or a particular temperature drop;
capture and BPU preprocessing still contain copies, as does the bounded relay.

The relay's ROS tests can be reproduced without a physical camera or BPU:

```sh
cd ros2_ws
colcon build --merge-install --packages-select muse_pet_camera_throttle --cmake-args -DBUILD_TESTING=ON
source install/setup.bash
colcon test --packages-select muse_pet_camera_throttle
colcon test-result --verbose
```

Run these with the normal camera applications stopped. The test uses its own
ROS domain and separate synthetic-source, relay and observer processes. It
checks frame contents, metadata, throttling, independent 30 FPS preview with
10 FPS model delivery, preview start/stop and explicit
failure when message loans are disabled. It does not execute the TROS models.

Check the topics before starting perception:

```sh
ros2 topic list -t
```

Topic names and message types can differ across TROS images. Set the actual
left and right topic names instead of relying on auto-discovery.

Check each X5 model before enabling a pipeline:

```sh
hrt_model_exec model_info --model_file /absolute/path/to/model.bin
```

## 2. Build the optional ROS bridge

From the repository checkout:

```sh
sudo apt-get install -y python3-colcon-common-extensions
sudo env INSTALL_PET_VISION=1 bash install.sh
```

This copies the project to `/opt/muse-ui`, builds `muse_pet_vision` and its image relay, and
enables `muse-pet-vision.service`. It does not install TROS, launch the camera,
or download gesture and YOLO models.

For camera autostart, stop the foreground camera and install with both flags:

```sh
sudo env INSTALL_PET_CAMERA=1 INSTALL_PET_VISION=1 bash install.sh
```

This also enables `muse-pet-camera.service`. Do not run a second camera launch.
Resolution, frame-rate request and combined-image settings are in
`/etc/default/muse-pet-camera`.

For a foreground run, stop the service and launch the same no-camera stack:

```sh
sudo systemctl stop muse-pet-vision
source /opt/tros/setup.bash 2>/dev/null || source /opt/tros/humble/setup.bash
source /opt/muse-ui/ros2_ws/install/setup.bash
source /opt/muse-ui/scripts/ros-transport.sh
ros2 launch muse_pet_vision muse_pet_vision.launch.py \
  left_topic:=/image_left_raw right_topic:=/image_right_raw \
  gesture_topic:=/muse/gestures object_topic:=/muse/objects \
  socket_path:=/run/muse-ui/input.sock \
  start_gesture:=true start_yolo:=true auto_discover:=false
```

This starts YOLOv8 and the body → hand landmarks → static gesture pipeline on
the left image. It never starts another camera. If perception is already
running, set both `start_*` flags to `false` and supply its output topics.
Model overrides are listed in `muse-pet-vision.env`.

For systemd, edit `/etc/default/muse-pet-vision` and restart the service:

```sh
sudoedit /etc/default/muse-pet-vision
# Set MUSE_PET_START_GESTURE=true and MUSE_PET_START_YOLO=true.
sudo systemctl restart muse-pet-vision
systemctl status muse-pet-vision --no-pager
journalctl -u muse-pet-vision -n 80 --no-pager
```

## 3. Recognition preview

Install with `INSTALL_PET_PREVIEW=1` in addition to the vision flag. This needs
the installed TROS `hobot_codec` and `websocket` packages; the installer adds
Python OpenCV. The service reuses the camera and inference results.

```sh
sudo env INSTALL_PET_VISION=1 INSTALL_PET_PREVIEW=1 bash install.sh
systemctl status muse-pet-preview --no-pager
```

On the SPI screen, swipe left to **Vision**. The image shows detection boxes,
hand landmarks and current matched labels. **Controls** opens the pet buttons;
**Vision** returns to the preview. Images expire after three seconds, so a
stopped camera is not shown as live.

Open `http://<X5-IP>:8000/TogetheROS/` for two views of the same left image:
YOLO objects on the left and hands/gestures on the right. HTTP uses port 8000;
the image sockets use 8080 and 8082. Nginx runs in the service rather than
being left behind by the vendor launch file. Installed viewer files are not edited.

The preview first matches exact model timestamps (within one millisecond).
For video faster than inference, the display can hold a past result for up to
150 ms. Empty results keep the video moving. The output queue waits 250 ms
for AI results and drops older eligible frames after a stall.
The JPEG decoder uses quarter-resolution decoding and one OpenCV thread.

The native UI targets 30 FPS. On X11 it compares changed pixels and sends only
their bounding region, keeping static background out of SPI transfers. Set
`MUSE_UI_FPS=8` to lower its target or `MUSE_UI_X11_DAMAGE=0` for the original
SDL presentation path. `MUSE_UI_FPS_METRICS=1` logs the software rendering rate.
This is distinct from measured panel refresh: a full 480×320 RGB565 frame at
30 FPS needs 73.7 Mbit/s, beyond the tested 40 MHz SPI connection. Small moving
regions can fit within that bandwidth. Preview FPS and model FPS remain
separate settings; do not raise both just to make the animation smoother.

With the default relay enabled, preview frames are requested only while the
native Vision page or a web viewer is open. Leaving Vision stops BMP decoding
and writes; closing both viewers also stops JPEG delivery to the codec. The
native page refreshes a lease in `/run/user/1000/muse-preview.active`, which
expires after three seconds if the UI crashes. Web demand uses established
connections on ports 8080/8082. After a processing stall, older queued frames
are discarded rather than replayed. This does not stop gesture/YOLO inference.

Control buttons return to the avatar after dispatch. Wake waves briefly,
Feed shows chewing and a banana, Focus shows a book, and Sleep closes the eyes.
Play dances with music notes, Stretch yawns and reaches up, and Look scans with
a curious expression. Petting gives a soft, eyes-closed cuddle with hearts.
Find banana patrols with a magnifying glass until the actual object detector
reports banana. Stop timer gives a short reaction and removes the countdown.

Vision uses the whole 480×320 screen, with boxes, names and hand keypoints
drawn on the image. There is no side information column. Aspect ratio and the
complete camera frame are preserved, so portrait video has side margins. Small
images decode at full detail; large images may use reduced JPEG decode.

### Depth page

Swipe left again from Vision to open **Depth**. Swipe right to return.
The page runs D-Robotics' [MagicBox StereoNet](https://github.com/D-Robotics/hobot_stereonet/tree/magicbox-v1.0.0)
V2.4 INT8 model on the BPU. It displays the vendor's colored depth map and
meter grid. Distances are estimates, not a calibrated measurement claim.

Install with `sudo env INSTALL_PET_CAMERA=1 INSTALL_PET_DEPTH=1 bash install.sh`.
This also installs Vision/preview. The X5 needs `hobot_stereonet`, `mipi_cam`
and `DStereoV2.4_int8.bin` already installed through TROS; no model download is
hidden in the installer.

The production pipeline is `muse-vision-runtime.service`, one C++ process owning
GS130WI capture, body/hand detection, hand landmarks, gesture classification,
YOLO and StereoNet. It loads vendor components directly; the two older hand
nodes are compiled from their pinned official sources because their installed
packages expose only executables. No separate model processes are launched.

One capture graph supplies calibrated 640×352 stereo and a raw 272×320 substream.
The runtime preserves camera and model instances across page changes. Hand
interaction remains active; the Depth page pauses YOLO input and gives its
budget to StereoNet. Returning to Vision resumes YOLO. Depth receives frames
only while `/run/user/1000/muse-depth.active` is fresh; a closed or crashed UI
expires this lease within three seconds. Model weights stay cached without
running depth inference off-page.

The separate `muse-pet-vision.service` handles pet events and landmark cleanup;
`muse-pet-preview.service` handles display rendering. Neither launches camera or
inference workers in unified mode. The former depth controller and per-model
launchers are no longer part of the production pipeline. Startup loads the
camera and models once; changing pages does not invoke ROS launch or systemd.

Use `INSTALL_PET_RUNTIME=1` (or `INSTALL_PET_DEPTH=1`) with the installer.
This selects SHM transport, installs the bridge-only service override and
replaces the legacy standalone camera/depth services. Do not run another
camera or the legacy perception launch alongside the unified runtime.

The unified preset captures at 30 FPS, delivers body/hand and YOLO inputs at
10 FPS and allows up to 30 FPS of depth input. These settings currently live in
`ros2_ws/src/muse_pet_runtime/src/vision_runtime.cpp`. The legacy launchers use
`MUSE_PET_THROTTLE`, `MUSE_PET_INFERENCE_FPS`, `MUSE_PET_PREVIEW_FPS` and
`MUSE_PET_BODY_IMAGE_GAP` in `/etc/default/muse-pet-vision`; those overrides do
not configure the unified executable. The relay preserves
resolution and timestamps and uses depth-one queues before the expensive
multi-node fan-out. A foreground perception launch can enable it with
`start_throttle:=true mono2d_image_gap:=0`.

The Python event bridge watches compact frame metadata from the relay, without
receiving raw pixels just to check camera health. Right-eye raw output is only
republished when it has a consumer. These metadata topics are uncalibrated
health reports, not camera calibration. Per-frame model warnings are suppressed
by default; set `MUSE_PET_LOG_LEVEL=warn` when collecting diagnostics.

The installed `mipi_cam` 2.5.2 driver can still publish faster than the requested
camera frame rate. Its [publisher loop](https://github.com/D-Robotics/hobot_mipi_cam/blob/tros_2.5.2/src/hobot_mipi_node.cpp#L410)
does not apply the calculated period. The relay limits downstream work; it does
not fix camera capture or the driver's full-rate image copies.

The models run on the BPU and JPEG encoding uses the hardware codec. ROS image
copies, model pre/postprocessing, the small-screen JPEG decoder and drawing
still use CPU. Check temperature and CPU frequency when judging smoothness;
a busy BPU is not required for the image pipeline to be CPU-bound.

## 4. Check the pet without vision

`petctl` is useful before enabling models:

```sh
petctl wake
petctl pet
petctl move left
petctl move right
petctl move stop
petctl feed banana
petctl timer start 25
petctl timer cancel
petctl find banana
petctl status
```

## Demo sequence

1. Hold an open palm to wake the pet.
2. Use ThumbLeft or ThumbRight to move it; losing the gesture stops movement.
   To stroke the pet, hold one open palm still briefly, then bring it toward the
   camera. Pull back before repeating; holding it close does not repeatedly pet.
3. Hold the OK gesture to feed a virtual snack. If a banana is currently
   detected, it is used instead; a physical food object is optional.
   A V sign starts a five-second dance. Hold either pose for about a second.
4. Start a study timer through Muse or `petctl timer start 25`.
5. Use `petctl find banana` to start the find-object game.

The state machine filters short detections, expires stale camera events and
applies a cooldown after feeding. Movement and timers run locally when Muse is
offline.

Palm approach uses the changing size of the palm's wrist/MCP landmarks, with
smoothing, a 35% growth threshold and retreat hysteresis. It estimates relative
approach from 2-D images, not centimeters or stereo depth. Missing hands, track
changes and two open palms reset tracking. OK and closed-hand gestures do not
arm it. Keep the full palm and fingers visible; a clipped hand cannot be tracked.
While an open palm is tracked, it also sends gaze updates at most four times per
second. Gaze expires after 650 ms without updates. Those updates do not wake a
sleeping pet or replace an active pose. Open-palm motion suppresses conflicting
OK/V/ThumbUp classifications; OK and V require 600 ms of continuous classification.
Feeding, cuddling, dancing and stretching reject conflicting gesture actions
until their animation finishes. An already-awake pet ignores another Palm wake.

Muse can call `petctl` through the paired Linux gadget's `system.run` tool. Try “Start a 25 minute focus timer” or
“帮我找一下杯子”. Device command permission must be granted in Muse; voice
recognition and this tool call have not been verified on the assembled unit.

## Model Zoo extensions

The [X5 Model Zoo](https://github.com/D-Robotics/rdk_model_zoo) has useful next
steps beyond COCO feeding and finding:

- [YOLOWorld](https://github.com/D-Robotics/rdk_model_zoo/tree/main/samples/vision/yoloworld):
  a scavenger hunt with a prepared vocabulary. Its X5 sample requires offline
  text embeddings; arbitrary spoken words are not immediately new classes.
- [YOLO pose](https://github.com/D-Robotics/rdk_model_zoo/tree/main/samples/vision/ultralytics_yolo):
  copy a pose or take a stretch break with the pet.
- [ByteTrack](https://github.com/D-Robotics/rdk_model_zoo/tree/main/samples/vision/bytetrack):
  keep following the same person through a brief occlusion.

Those three games are extension ideas, not enabled pipelines in this revision.
The bridge accepts additional `ai_msgs/PerceptionTargets` detector topics via
`extra_detector_topics:='yoloworld:/muse/world,tracker:/muse/tracks'`.
Model Zoo Python outputs need an explicit ROS publisher adapter before they
can use that interface; a model filename alone is not an interchangeable plugin.

## Host replay

To exercise local interaction without a camera or Muse credentials:

```sh
mkdir -p /tmp/muse-pet-demo
export MUSE_UI_RUNTIME=/tmp/muse-pet-demo
PYTHONPATH=sdk MUSE_UI_CHAT=off python3 backend.py
# In another terminal, using the same MUSE_UI_RUNTIME:
MUSE_UI_RUNTIME=/tmp/muse-pet-demo ./build/muse_simulator
python3 scripts/replay-pet-demo.py --runtime /tmp/muse-pet-demo
```

Replay generates synthetic events. It does not run a model. Pet state and
timers currently reset when the backend restarts.

## Runtime checks

```sh
petctl status
systemctl status muse-ui muse-pet-camera muse-pet-vision --no-pager
journalctl -u muse-ui -u muse-pet-camera -u muse-pet-vision -n 100 --no-pager
pactl info
pactl list short sources
pactl list short sinks
```

On the test X5, both image topics and all four perception topics delivered
messages; YOLO detected a person and the bridge reported `vision=ready`.
This confirms data flow, not recognition accuracy. Physical gesture-to-action,
food confirmation and the selected audio hardware still need a live user test.

Check the actual image orientation before testing gestures: 1088×1280 is a
portrait-sized stream. Do not change rotation until you have inspected a frame.
Start at the camera's default rate, then measure latency and BPU load with both
algorithms enabled. The optional Depth page enables StereoNet input in the same
vision runtime; the IMU is not used.

Implementation references: [camera](https://github.com/D-Robotics/hobot_mipi_cam),
[gesture nodes](https://github.com/D-Robotics/hand_gesture_detection),
[DNN nodes](https://github.com/D-Robotics/hobot_dnn), and
[RDK Device Skills](https://github.com/D-Robotics/rdk-device-skills).
