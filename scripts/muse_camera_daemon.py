#!/usr/bin/env python3
"""Add on-demand camera.capture to the existing Linux Muse daemon."""
import sys
sys.path.insert(0, '/opt/muse-ui')
from camera_tool import capture
from musegadget import cli
from musegadget.executor import COMMAND_SPECS, Executor

COMMAND_SPECS['camera.capture'] = {
    'description': ('Capture a fresh left/right stereo JPEG only when the user '
                    'asks to see or describe the camera view. The image is returned as base64.'),
    'required': {}, 'optional': {}, 'timeout_ms': 20000,
}
original_run = Executor.run


def run(self, command, params, timeout_ms=None):
    if command != 'camera.capture':
        return original_run(self, command, params, timeout_ms)
    try:
        return {'ok': True, 'payload': capture(timeout=15)}
    except (OSError, RuntimeError) as exc:
        return {'ok': False, 'error': {'code': 'camera_capture_failed', 'message': str(exc)}}


Executor.run = run
if __name__ == '__main__':
    raise SystemExit(cli.main(['run']))
