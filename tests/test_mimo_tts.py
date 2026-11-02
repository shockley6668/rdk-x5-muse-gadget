import base64
import io
import json
import unittest
import wave

from mimo_tts import synthesize


def sample_wav():
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        w.writeframes(b"\0\0" * 64)
    return out.getvalue()


class Response(io.BytesIO):
    pass


class MiMoTests(unittest.TestCase):
    def test_chinese_request_and_wav_response(self):
        expected = sample_wav()
        def opener(req, timeout):
            body = json.loads(req.data)
            self.assertEqual(body["model"], "mimo-v2.5-tts")
            self.assertEqual(body["messages"][-1], {"role": "assistant", "content": "你好"})
            self.assertEqual(body["audio"]["voice"], "冰糖")
            self.assertEqual(req.get_header("Authorization"), "Bearer secret")
            return Response(json.dumps({"choices": [{"message": {"audio": {
                "data": base64.b64encode(expected).decode()}}}]}).encode())
        self.assertEqual(synthesize("你好", "secret", opener=opener), expected)

    def test_invalid_audio_does_not_reach_the_player(self):
        def opener(req, timeout):
            return Response(json.dumps({"choices": [{"message": {"audio": {
                "data": base64.b64encode(b"bad").decode()}}}]}).encode())
        with self.assertRaises((wave.Error, EOFError)):
            synthesize("Hello", "secret", opener=opener)
