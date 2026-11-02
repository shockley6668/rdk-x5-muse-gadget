"""On-demand stereo snapshot for the Muse device camera command."""
import base64
import fcntl
import os
from pathlib import Path
import time

LEASE = Path('/run/user/1000/muse-capture.active')
SNAPSHOT = Path('/run/muse-pet-preview/snapshot.jpg')
LOCK = Path('/run/musegadget/muse-camera.lock')
STATE = Path('/run/muse-ui/state')


def capture(timeout=15.0):
    """Return a fresh paired snapshot; never return a previously saved frame."""
    try:
        fields = dict(line.split('=', 1) for line in STATE.read_text().splitlines() if '=' in line)
        if fields.get('vision') != 'ready':
            raise RuntimeError('Stereo camera is not ready')
    except OSError:
        raise RuntimeError('Stereo camera status unavailable') from None
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Camera is busy') from None
        started_ns = time.time_ns()
        deadline = time.monotonic() + timeout
        try:
            LEASE.write_text(str(started_ns))
            while time.monotonic() < deadline:
                LEASE.touch()
                try:
                    stat = SNAPSHOT.stat()
                    if stat.st_mtime_ns >= started_ns:
                        image = SNAPSHOT.read_bytes()
                        if (image.startswith(b'\xff\xd8') and image.endswith(b'\xff\xd9')
                                and len(image) <= 160_000):
                            return {"format": "jpeg-base64",
                                    "data_base64": base64.b64encode(image).decode('ascii')}
                except FileNotFoundError:
                    pass
                time.sleep(.2)
            raise RuntimeError('Fresh stereo camera frame unavailable')
        finally:
            LEASE.unlink(missing_ok=True)
            try:
                if SNAPSHOT.stat().st_mtime_ns >= started_ns:
                    SNAPSHOT.unlink()
            except FileNotFoundError:
                pass
