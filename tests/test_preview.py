"""Preview setup must preserve the installed TROS viewer and its assets."""
from pathlib import Path
from collections import deque
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS
import importlib.util
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ros2_ws/src/muse_pet_vision"))
from muse_pet_vision.preview import prepare_viewer
from muse_pet_vision.preview_frames import OverlayCache, render_screen, translate_left_gestures
from muse_pet_vision.preview_demand import latest_ready, screen_active, web_active


class PreviewTests(unittest.TestCase):
    def test_left_hand_coordinates_translate_without_mutating_source(self):
        message = NS(targets=[NS(rois=[NS(rect=NS(x_offset=20))],
                                 points=[NS(point=[NS(x=30.0)])])])
        translated = translate_left_gestures(message)
        self.assertEqual(translated.targets[0].rois[0].rect.x_offset, 76)
        self.assertEqual(translated.targets[0].points[0].point[0].x, 86)
        self.assertEqual(message.targets[0].rois[0].rect.x_offset, 20)
        self.assertIsNone(translate_left_gestures(None))

    def test_screen_lease_expires_after_closed_or_crashed_frontend(self):
        with tempfile.TemporaryDirectory() as tmp:
            lease = Path(tmp) / "muse-preview.active"
            self.assertFalse(screen_active(lease, 10))
            lease.touch()
            os.utime(lease, (10, 10))
            self.assertTrue(screen_active(lease, 12.9))
            self.assertFalse(screen_active(lease, 13))
            self.assertFalse(screen_active(lease, 9))

    def test_web_demand_requires_live_client_on_viewer_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            tcp = Path(tmp) / "tcp"
            for local, state, expected in (("1F90", "0A", False),  # listening
                                            ("1F90", "06", False),  # closed
                                            ("0050", "01", False),
                                            ("1F90", "01", True),
                                            ("1F92", "01", True)):
                tcp.write_text(f"header\n0: 00000000:{local} 00000000:1234 {state}\n")
                self.assertEqual(web_active([tcp]), expected)

    def test_stalled_renderer_skips_backlog_and_keeps_newer_pending_frames(self):
        pending = deque([(1.0, "old"), (1.5, "latest"), (1.9, "not-ready")])
        self.assertEqual(latest_ready(pending, 2.0), "latest")
        self.assertEqual(list(pending), [(1.9, "not-ready")])
        self.assertIsNone(latest_ready(pending, 4.0))

    def test_idle_and_web_only_preview_never_decode_screen_images(self):
        from types import ModuleType
        from muse_pet_vision import preview_frames

        class Publisher:
            def __init__(self): self.messages = []
            def publish(self, msg): self.messages.append(msg)

        class Node:
            def __init__(self, name): self.params, self.pubs, self.callbacks = {}, {}, {}
            def declare_parameter(self, name, value): self.params[name] = value
            def get_parameter(self, name): return NS(value=self.params[name])
            def create_publisher(self, kind, topic, qos):
                self.pubs[topic] = Publisher()
                return self.pubs[topic]
            def create_subscription(self, kind, topic, callback, qos):
                self.callbacks[topic] = callback
                return callback
            def create_timer(self, period, callback): pass
            def destroy_node(self): pass

        def module(name, **values):
            result = ModuleType(name)
            result.__dict__.update(values)
            return result

        def spin(node):
            frame = NS(header=NS(stamp=NS(sec=10, nanosec=0)))
            # The consumer must not subscribe to its own web output.
            self.assertIn("/muse/preview/encoded", node.callbacks)
            self.assertNotIn("/muse/preview/jpeg", node.callbacks)
            with patch.object(preview_frames, "screen_active", return_value=False), \
                 patch.object(preview_frames, "web_active", return_value=False):
                node.check_viewers()
            self.assertFalse(node.pubs["/muse/preview/demand"].messages[-1].data)
            node.pending.append((0, frame))
            node.tick()
            self.assertFalse(node.pending)
            self.assertFalse(node.pubs["/muse/preview/jpeg"].messages)
            node.web = True
            node.pending.append((__import__('time').monotonic() - .3, frame))
            node.tick()
            self.assertEqual(node.pubs["/muse/preview/jpeg"].messages, [frame])
            for topic in ("objects", "gestures"):
                result = node.pubs[f"/muse/preview/{topic}"].messages[0]
                self.assertEqual(result.header.stamp.sec, 10)

        modules = {
            "cv2": module("cv2", setNumThreads=lambda n: None),
            "rclpy": module("rclpy", init=lambda **k: None, shutdown=lambda: None, spin=spin),
            "rclpy.node": module("rclpy.node", Node=Node),
            "rclpy.qos": module("rclpy.qos", qos_profile_sensor_data=1),
            "ai_msgs": module("ai_msgs"),
            "ai_msgs.msg": module("ai_msgs.msg", PerceptionTargets=NS),
            "sensor_msgs": module("sensor_msgs"),
            "sensor_msgs.msg": module("sensor_msgs.msg", CompressedImage=NS, Image=NS),
            "std_msgs": module("std_msgs"),
            "std_msgs.msg": module("std_msgs.msg", Bool=NS),
        }
        with patch.dict(sys.modules, modules), patch.object(preview_frames, "render_screen") as render:
            preview_frames.main()
            render.assert_not_called()

    def test_overlay_rounding_is_allowed_but_other_frames_and_stale_results_are_not(self):
        cache = OverlayCache(limit=2)
        message = NS(header=NS(stamp=NS(sec=10, nanosec=500)))
        cache.add(message, 1.0)
        self.assertIs(cache.match(10_000_000_000, 1.1), message)
        self.assertIsNone(cache.match(10_033_000_000, 1.1))
        self.assertIsNone(cache.match(10_000_000_000, 3.1))
        for sec in range(4):
            cache.add(NS(header=NS(stamp=NS(sec=sec, nanosec=0))), 4.0)
        self.assertEqual(len(cache.items), 2)

    def test_fast_video_only_reuses_recent_past_results(self):
        cache = OverlayCache()
        message = NS(header=NS(stamp=NS(sec=10, nanosec=0)))
        cache.add(message, 1.0)
        self.assertIs(cache.for_display(10_100_000_000, 1.1), message)
        self.assertIsNone(cache.for_display(10_151_000_000, 1.1))
        self.assertIsNone(cache.for_display(9_966_000_000, 1.1))
        self.assertIsNone(cache.for_display(10_100_000_000, 3.1))

    @unittest.skipUnless(importlib.util.find_spec("cv2"), "OpenCV is installed on X5")
    def test_real_jpeg_renders_overlay_at_native_screen_size(self):
        import cv2
        import numpy as np
        source = np.zeros((1280, 1088, 3), dtype=np.uint8)
        source[:] = (60, 70, 80)
        _, jpeg = cv2.imencode('.jpg', source)
        target = NS(type='banana', attributes=[], points=[], rois=[NS(
            type='banana', rect=NS(x_offset=100, y_offset=100, width=300, height=400))])
        output = render_screen(bytes(jpeg), [NS(targets=[target]), None])
        self.assertEqual(output.shape, (320, 480, 3))
        self.assertTrue(((output[:, :224, 1] > 200) & (output[:, :224, 2] < 180)).any())
        with self.assertRaises(ValueError):
            render_screen(b'not jpeg', [None, None])

    @unittest.skipUnless(importlib.util.find_spec("cv2"), "OpenCV is installed on X5")
    def test_small_preview_preserves_native_pixels_and_the_complete_camera_frame(self):
        import cv2
        import numpy as np
        source = np.zeros((320, 272, 3), dtype=np.uint8)
        source[:, ::4] = (210, 130, 60)
        source[::3, :] = (50, 220, 150)
        _, jpeg = cv2.imencode('.jpg', source)
        decoded = cv2.imdecode(jpeg, cv2.IMREAD_COLOR)
        output = render_screen(bytes(jpeg), [None, None])
        self.assertEqual(output.shape, (320, 480, 3))
        self.assertTrue(np.array_equal(output[:, 104:376], decoded))
        self.assertFalse(output[:, :104].any())
        self.assertFalse(output[:, 376:].any())

    @unittest.skipUnless(importlib.util.find_spec("cv2"), "OpenCV is installed on X5")
    def test_panorama_fills_screen_and_transforms_overlays(self):
        import cv2
        import numpy as np
        source = np.full((320, 384, 3), 17, dtype=np.uint8)
        source[:, 56:328] = (40, 60, 80)
        _, jpeg = cv2.imencode('.jpg', source, [cv2.IMWRITE_JPEG_QUALITY, 100])
        target = NS(type='person', attributes=[], points=[], rois=[NS(
            type='person', rect=NS(x_offset=100, y_offset=100, width=80, height=80))])
        output = render_screen(bytes(jpeg), [NS(targets=[target]), None], fill_panorama=True)
        self.assertEqual(output.shape, (320, 480, 3))
        self.assertGreater(int(output[160, 0].sum()), 50)
        self.assertGreater(int(output[160, -1].sum()), 50)
        self.assertTrue(((output[40:110, 60:100, 1] > 200) &
                         (output[40:110, 60:100, 2] < 180)).any())

    def test_runtime_copy_enables_both_views_without_mutating_vendor_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vendor = root / "vendor"
            source = vendor / "html/TogetheROS/index.html"
            source.parent.mkdir(parents=True)
            original = '<html><head></head><script src="./main.js"></script></html>'
            source.write_text(original)
            runtime = root / "runtime"
            config = prepare_viewer(vendor, runtime)
            self.assertEqual(source.read_text(), original)
            self.assertIn('path_num=2;', (runtime / "index.html").read_text())
            self.assertIn('video-wrap-8082', (runtime / "index.html").read_text())
            self.assertIn('listen 8000;', config.read_text())
            self.assertIn(str(vendor / "html"), config.read_text())
            self.assertIn('daemon off;', config.read_text())

    def test_unknown_viewer_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "html/TogetheROS/index.html"
            source.parent.mkdir(parents=True)
            source.write_text('<html></html>')
            with self.assertRaises(ValueError):
                prepare_viewer(root, root / "runtime")
