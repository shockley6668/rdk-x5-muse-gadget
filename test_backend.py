import asyncio
import base64
import io
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend import Bridge, ChatSession, make_voice_payload
from pet import PetEngine


class FakeBridge:
    def __init__(self):
        self.states = []
    def set_state(self, **state):
        self.states.append(state)


class Tests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_discards_recording_without_sending(self):
        with tempfile.TemporaryDirectory() as directory, patch("backend.RUNTIME", Path(directory)):
            bridge = Bridge()
            process = MagicMock()
            bridge.recording = process
            bridge.record_task = asyncio.get_running_loop().create_future()
            bridge.record_task.set_result(b"a" * 32000)
            bridge.set_state(face="listening")
            await bridge.command({"action": "cancel"})
            process.terminate.assert_called_once()
            self.assertIsNone(bridge.recording)
            self.assertIsNone(bridge.turn_task)
            self.assertEqual(bridge.state["face"], "idle")

    async def test_unchanged_state_skips_file_writes_but_keeps_heartbeat_and_immediate_changes(self):
        with tempfile.TemporaryDirectory() as directory, patch("backend.RUNTIME", Path(directory)), \
             patch("backend.time.monotonic", return_value=10.0) as clock:
            bridge = Bridge()
            state = Path(directory) / "state"
            first = state.stat().st_ino
            bridge.write_state()
            self.assertEqual(state.stat().st_ino, first)
            clock.return_value = 15.0
            bridge.write_state()
            self.assertNotEqual(state.stat().st_ino, first)
            bridge.set_state(caption="new reply")
            self.assertIn("caption=new reply\n", state.read_text())

    async def test_reply_ignores_other_parent_and_does_not_duplicate_final(self):
        s = object.__new__(ChatSession)
        s.bridge = FakeBridge()
        s.user_ids = {"our-user-message"}
        s.messages = {}
        s.turn_done = asyncio.get_running_loop().create_future()
        s.consume({"event": "message.assistant", "payload": {
            "message_id": "other-reply", "reply_to_message_id": "other-user-message",
            "display_text": "Should not speak this"}})
        self.assertFalse(s.turn_done.done())
        s.consume({"event": "delta.text_append", "payload": {
            "message_id": "reply", "reply_to_message_id": "our-user-message", "text": "你好"}})
        s.consume({"event": "delta.message_done", "payload": {"message_id": "reply"}})
        self.assertEqual(s.turn_done.result(), "你好")
        s.consume({"event": "message.assistant", "payload": {
            "message_id": "reply", "display_text": "你好"}})
        self.assertEqual(s.turn_done.result(), "你好")

    async def test_voice_note_is_valid_mono_pcm_wav(self):
        pcm = struct.pack("<hhh", -32768, 0, 32767)
        note = make_voice_payload(pcm)
        raw = base64.b64decode(note["items"][0]["data_base64"])
        with wave.open(io.BytesIO(raw)) as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate()), (1, 2, 16000))
            self.assertEqual(w.readframes(3), pcm)

    async def test_pet_commands_add_fields_without_replacing_chat_state(self):
        with tempfile.TemporaryDirectory() as directory, patch("backend.RUNTIME", Path(directory)):
            bridge = Bridge()
            bridge.set_state(face="speaking", caption="chat reply", level="0.5")
            await bridge.command({"action": "pet_command", "command": "wake"})
            await bridge.command({
                "action": "pet_command", "command": "move", "direction": "right"})
            self.assertEqual((bridge.state["face"], bridge.state["caption"]),
                             ("speaking", "chat reply"))
            self.assertEqual((bridge.state["pet_awake"], bridge.state["pet_direction"]),
                             ("true", "right"))

            written = (Path(directory) / "state").read_text(encoding="utf-8")
            self.assertIn("caption=chat reply\n", written)
            self.assertIn("pet_position=55\n", written)

            await bridge.command({
                "action": "pet_command", "command": "timer start 25"})
            self.assertEqual(bridge.state["pet_timer_duration"], "1500")
            self.assertEqual(bridge.state["pet_timer"], "25:00")

    async def test_backend_accepts_nested_pet_events(self):
        class Clock:
            value = 10.0
            def __call__(self):
                return self.value

        clock = Clock()
        engine = PetEngine(clock=clock)
        with tempfile.TemporaryDirectory() as directory, patch("backend.RUNTIME", Path(directory)):
            bridge = Bridge(engine)
            await bridge.command({"action": "pet_event", "event": {
                "type": "gesture", "name": "Palm", "event_id": "palm-1",
                "observed_at": 10.0, "stable_ms": 300,
            }})
            self.assertEqual(bridge.state["pet_awake"], "true")
            self.assertEqual(bridge.state["pet_last_gesture"], "Palm")


if __name__ == "__main__":
    unittest.main()
