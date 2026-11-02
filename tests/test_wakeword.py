import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
import numpy as np


class FakeVad:
    def __init__(self):
        self.speech = False
        self.segment = False
    def reset(self):
        pass
    def accept_waveform(self, samples):
        pass
    def is_speech_detected(self):
        return self.speech
    def empty(self):
        return not self.segment
    def pop(self):
        self.segment = False


class TurnEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        module_path = Path(__file__).resolve().parents[1] / "wakeword.py"
        spec = importlib.util.spec_from_file_location("muse_wakeword_test", module_path)
        cls.module = importlib.util.module_from_spec(spec)
        # End-of-speech decisions do not depend on Sherpa native bindings.
        with patch.dict(sys.modules, {"sherpa_onnx": types.ModuleType("sherpa_onnx")}):
            spec.loader.exec_module(cls.module)

    def test_no_voice_cancels_instead_of_sending_silence(self):
        turn = self.module.TurnEnd(10, FakeVad())
        self.assertIsNone(turn.update(np.zeros(512), 16.9))
        self.assertEqual(turn.update(np.zeros(512), 17), "cancel")

    def test_voice_sends_after_pause_and_tracks_latest_speech(self):
        vad = FakeVad()
        turn = self.module.TurnEnd(10, vad)
        vad.speech = True
        self.assertIsNone(turn.update(np.ones(512), 11))
        vad.speech = False
        self.assertIsNone(turn.update(np.zeros(512), 12.3))
        vad.segment = True
        self.assertEqual(turn.update(np.zeros(512), 13.9), "stop")

    def test_continuous_speech_has_a_maximum_duration(self):
        vad = FakeVad()
        vad.speech = True
        turn = self.module.TurnEnd(0, vad)
        self.assertIsNone(turn.update(np.ones(512), 24))
        self.assertEqual(turn.update(np.ones(512), 25), "stop")


if __name__ == "__main__":
    unittest.main()
