import base64
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import camera_tool


class CameraToolTests(unittest.TestCase):
    def test_returns_only_a_fresh_jpeg_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lease, image, lock = root / 'lease', root / 'snapshot.jpg', root / 'lock'
            state = root / 'state'; state.write_text('vision=ready\n')
            image.write_bytes(b'\xff\xd8old\xff\xd9')
            os.utime(image, (1, 1))
            def publish():
                while not lease.exists():
                    time.sleep(.01)
                image.write_bytes(b'\xff\xd8new\xff\xd9')
            thread = threading.Thread(target=publish)
            with patch.multiple(camera_tool, LEASE=lease, SNAPSHOT=image, LOCK=lock, STATE=state):
                thread.start()
                result = camera_tool.capture(timeout=2)
                thread.join()
                self.assertEqual(base64.b64decode(result['data_base64']), b'\xff\xd8new\xff\xd9')
                self.assertFalse(lease.exists())
                self.assertFalse(image.exists())

    def test_stale_jpeg_is_never_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lease, image, lock = root / 'lease', root / 'snapshot.jpg', root / 'lock'
            state = root / 'state'; state.write_text('vision=ready\n')
            image.write_bytes(b'\xff\xd8old\xff\xd9')
            os.utime(image, (1, 1))
            with patch.multiple(camera_tool, LEASE=lease, SNAPSHOT=image, LOCK=lock, STATE=state):
                with self.assertRaises(RuntimeError):
                    camera_tool.capture(timeout=.25)
                self.assertFalse(lease.exists())

    def test_degraded_camera_fails_without_starting_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, lease = root / 'state', root / 'lease'
            state.write_text('vision=degraded\n')
            with patch.multiple(camera_tool, STATE=state, LEASE=lease):
                with self.assertRaisesRegex(RuntimeError, 'not ready'):
                    camera_tool.capture(timeout=.1)
                self.assertFalse(lease.exists())


if __name__ == '__main__':
    unittest.main()
