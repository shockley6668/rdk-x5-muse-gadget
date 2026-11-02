"""Frame-matched overlays for the web viewer and the native SPI screen."""
from collections import deque
import copy
import os
from pathlib import Path
import time

from .adapter import normalize_gesture
from .depth_frames import DepthRenderer
from .preview_demand import latest_ready, screen_active, web_active


def stamp_ns(message):
    return message.header.stamp.sec * 1_000_000_000 + message.header.stamp.nanosec


def translate_left_gestures(message, offset=56):
    """Move left-eye hand geometry into the fixed 384-pixel panorama."""
    if message is None:
        return None
    result = copy.deepcopy(message)
    for target in result.targets:
        for roi in target.rois:
            roi.rect.x_offset += offset
        for group in target.points:
            for point in group.point:
                point.x += offset
    return result


class OverlayCache:
    """Allow sub-millisecond timestamp rounding, never a neighbouring frame."""
    def __init__(self, tolerance_ns=1_000_000, ttl=2.0, limit=128):
        self.tolerance_ns, self.ttl, self.limit = tolerance_ns, ttl, limit
        self.items = {}

    def add(self, message, now):
        self.items[stamp_ns(message)] = (now, message)
        self.expire(now)
        while len(self.items) > self.limit:
            self.items.pop(next(iter(self.items)))

    def expire(self, now):
        self.items = {key: value for key, value in self.items.items()
                      if now - value[0] <= self.ttl}

    def match(self, stamp, now):
        self.expire(now)
        if not self.items:
            return None
        nearest = min(self.items, key=lambda key: abs(key - stamp))
        return self.items[nearest][1] if abs(nearest - stamp) <= self.tolerance_ns else None

    def for_display(self, stamp, now, max_lag_ns=150_000_000):
        """A faster video may reuse the latest past result for at most 150 ms.

        This is display-only: pet actions still use original inference stamps.
        Never put a future result or a stale box on a video frame.
        """
        exact = self.match(stamp, now)
        if exact is not None:
            return exact
        candidates = [key for key in self.items if 0 <= stamp - key <= max_lag_ns]
        return self.items[max(candidates)][1] if candidates else None


def render_screen(jpeg, messages, fill_panorama=False):
    import cv2
    import numpy as np

    encoded = np.frombuffer(jpeg, dtype=np.uint8)
    # Reduce large frames, but keep native detail in the small inference stream.
    source = cv2.imdecode(encoded, cv2.IMREAD_REDUCED_COLOR_4)
    if source is None:
        raise ValueError("Invalid preview JPEG")
    divisor = 4
    if source.shape[1] < 240 or source.shape[0] < 160:
        source = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        divisor = 1
    height, width = (dimension * divisor for dimension in source.shape[:2])
    if fill_panorama and (width, height) == (384, 320) and divisor == 1:
        # The left eye always occupies columns 56..327; only the right eye's
        # extra coverage can extend beyond those bounds. Ignore JPEG noise.
        # NV12 black can decode as grey (around 17), not necessarily zero.
        # Estimate the padding colour from the outside corners instead.
        border = np.concatenate((source[:16, :8].reshape(-1, 3),
                                 source[:16, -8:].reshape(-1, 3)), axis=0)
        padding = np.median(border, axis=0).astype(np.int16)
        difference = np.max(np.abs(source.astype(np.int16) - padding), axis=2)
        active = (difference > 12).sum(axis=0) >= 16
        left_extra = np.flatnonzero(active[:56])
        right_extra = np.flatnonzero(active[328:])
        x0 = int(left_extra[0]) if left_extra.size else 56
        x1 = 328 + int(right_extra[-1]) + 1 if right_extra.size else 328
        crop_height = min(height, round((x1 - x0) * 320 / 480))
        y0 = (height - crop_height) // 2
        canvas = cv2.resize(source[y0:y0 + crop_height, x0:x1], (480, 320))
        scale_x, scale_y = 480 / (x1 - x0), 320 / crop_height
        offset_x, offset_y = -x0 * scale_x, -y0 * scale_y
        bounds = (0, 0, 479, 319)
    else:
        scale_x = scale_y = min(480 / width, 320 / height)
        w, h = max(1, round(width * scale_x)), max(1, round(height * scale_y))
        canvas = np.zeros((320, 480, 3), dtype=np.uint8)
        offset_x, offset_y = (480 - w) // 2, (320 - h) // 2
        canvas[offset_y:offset_y + h, offset_x:offset_x + w] = cv2.resize(source, (w, h))
        bounds = (offset_x, offset_y, offset_x + w - 1, offset_y + h - 1)
    for index, message in enumerate(messages):
        if message is None:
            continue
        color = (140, 255, 120) if index == 0 else (255, 190, 110)
        for target in message.targets:
            gesture_names = [normalize_gesture(a.value) for a in target.attributes
                             if a.type == "gesture"]
            gesture_name = next((name for name in gesture_names
                                 if name and name != "background"), None)
            for roi in target.rois:
                rect = roi.rect
                x = round(rect.x_offset * scale_x + offset_x)
                y = round(rect.y_offset * scale_y + offset_y)
                x2 = round((rect.x_offset + rect.width) * scale_x + offset_x)
                y2 = round((rect.y_offset + rect.height) * scale_y + offset_y)
                if x2 < bounds[0] or x > bounds[2] or y2 < bounds[1] or y > bounds[3]:
                    continue
                x, x2 = max(bounds[0], x), min(bounds[2], x2)
                y, y2 = max(bounds[1], y), min(bounds[3], y2)
                cv2.rectangle(canvas, (x, y), (x2, y2), color, 1)
                label = target.type or roi.type
                if index == 1:
                    label = gesture_name.replace('_', ' ').title() if gesture_name else "Hand"
                label = label[:22]
                (tw, th), baseline = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, .4, 1)
                tx, ty = min(x, 478 - tw), max(th + 4, y - 4)
                cv2.rectangle(canvas, (tx, ty - th - 3), (tx + tw + 2, ty + baseline), (15, 15, 20), -1)
                cv2.putText(canvas, label, (tx + 1, ty), cv2.FONT_HERSHEY_SIMPLEX,
                            .4, color, 1, cv2.LINE_AA)
            for group in target.points:
                if group.type != "hand_kps":
                    continue
                points = [(round(p.x * scale_x + offset_x), round(p.y * scale_y + offset_y))
                          for p in group.point]
                for finger in ((0, 1, 2, 3, 4), (0, 5, 6, 7, 8),
                               (0, 9, 10, 11, 12), (0, 13, 14, 15, 16),
                               (0, 17, 18, 19, 20)):
                    for a, b in zip(finger, finger[1:]):
                        if b < len(points):
                            cv2.line(canvas, points[a], points[b], color, 1)
                for point in points:
                    cv2.circle(canvas, point, 2, (150, 120, 255), -1)
    return canvas


def main(args=None):
    import cv2
    import rclpy
    from ai_msgs.msg import PerceptionTargets
    from rclpy.node import Node
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import CompressedImage, Image
    from std_msgs.msg import Bool
    cv2.setNumThreads(1)

    class PreviewFrames(Node):
        def __init__(self):
            super().__init__("muse_preview_frames")
            for name, default in (("object_topic", "/muse/objects"),
                                  ("gesture_topic", "/muse/gestures"),
                                  ("screen_path", "/run/muse-pet-preview/vision.bmp")):
                self.declare_parameter(name, default)
            # Keep the matching delay at up to 30 FPS without evicting every
            # frame before it becomes eligible (4 frames only cover 100 ms).
            self.pending = deque(maxlen=12)
            self.caches = [OverlayCache(), OverlayCache()]
            self.publishers_ = [self.create_publisher(PerceptionTargets,
                f"/muse/preview/{kind}", 10) for kind in ("objects", "gestures")]
            self.subs = [self.create_subscription(CompressedImage, "/muse/preview/encoded",
                lambda msg: self.pending.append((time.monotonic(), msg)), qos_profile_sensor_data)]
            for i, parameter in enumerate(("object_topic", "gesture_topic")):
                self.subs.append(self.create_subscription(PerceptionTargets,
                    str(self.get_parameter(parameter).value),
                    lambda msg, i=i: self.caches[i].add(msg, time.monotonic()), 10))
            self.screen = self.web = self.capture = False
            self.lease = Path(os.environ.get("MUSE_UI_RUNTIME") or
                              os.environ.get("XDG_RUNTIME_DIR") or "/run/user/1000") / "muse-preview.active"
            self.depth_renderer = DepthRenderer(
                os.environ.get('MUSE_PET_DEPTH_RUNTIME', '/run/muse-pet-depth'),
                self.lease.with_name('muse-depth.active'))
            self.subs.append(self.create_subscription(Image, '/MuseStereo/stereonet_visual',
                                                      self.render_depth, 1))
            self.demand = self.create_publisher(Bool, "/muse/preview/demand", 1)
            self.jpeg = self.create_publisher(CompressedImage, "/muse/preview/jpeg", 1)
            self.path = Path(str(self.get_parameter("screen_path").value))
            self.capture_lease = self.lease.with_name('muse-capture.active')
            self.capture_path = self.path.with_name('snapshot.jpg')
            self.create_timer(.01, self.tick)
            self.create_timer(.5, self.check_viewers)

        def render_depth(self, message):
            try:
                self.depth_renderer.render(message)
            except (OSError, ValueError, cv2.error) as exc:
                self.get_logger().warning(str(exc), throttle_duration_sec=5.0)

        def check_viewers(self):
            self.screen = screen_active(self.lease)
            self.web = web_active()
            self.capture = screen_active(self.capture_lease)
            self.demand.publish(Bool(data=self.screen or self.web or self.capture))
            if not (self.screen or self.web or self.capture):
                self.pending.clear()

        def tick(self):
            now = time.monotonic()
            if not (self.screen or self.web or self.capture):
                self.pending.clear()
                return
            frame = latest_ready(self.pending, now)
            if frame is not None:
                overlays = [cache.for_display(stamp_ns(frame), now) for cache in self.caches]
                if os.environ.get('MUSE_PET_PANORAMA') == '1':
                    overlays[1] = translate_left_gestures(overlays[1])
                if self.web:
                    for publisher, overlay in zip(self.publishers_, overlays):
                        output = copy.deepcopy(overlay) if overlay is not None else PerceptionTargets()
                        # TROS websocket requires exact equality, including nanoseconds.
                        output.header = copy.deepcopy(frame.header)
                        publisher.publish(output)
                    self.jpeg.publish(frame)
                if self.capture and screen_active(self.capture_lease):
                    try:
                        requested_ns = int(self.capture_lease.read_text())
                        if stamp_ns(frame) >= requested_ns:
                            temp = self.capture_path.with_suffix('.tmp.jpg')
                            temp.write_bytes(bytes(frame.data))
                            os.replace(temp, self.capture_path)
                    except (OSError, ValueError) as exc:
                        self.get_logger().warning(str(exc), throttle_duration_sec=5.0)
                if not self.screen:
                    return
                try:
                    canvas = render_screen(bytes(frame.data), overlays,
                                           fill_panorama=os.environ.get('MUSE_PET_PANORAMA') == '1')
                    temporary = self.path.with_suffix(".tmp.bmp")
                    if not cv2.imwrite(str(temporary), canvas):
                        raise OSError("Cannot write screen preview")
                    os.replace(temporary, self.path)
                except (OSError, ValueError, cv2.error) as exc:
                    self.get_logger().warning(str(exc), throttle_duration_sec=5.0)

    rclpy.init(args=args)
    node = PreviewFrames()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()
