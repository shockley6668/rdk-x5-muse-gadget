import asyncio
import base64
import io
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend import Bridge, ChatSession, make_voice_payload
from pet import PetEngine
from musegadget.muse_turn import ReplayScope


class FakeBridge:
    def __init__(self):
        self.states = []
    def set_state(self, **state):
        self.states.append(state)


class Tests(unittest.IsolatedAsyncioTestCase):
    async def test_wake_call_cannot_start_muted_or_duplicate_the_microphone(self):
        with tempfile.TemporaryDirectory() as directory, patch("backend.RUNTIME", Path(directory)):
            bridge = Bridge()
            bridge.native_calls = bridge.chat_enabled = bridge.audio_enabled = True
            bridge.muted = True
            await bridge.command({"action": "call"})
            self.assertIsNone(bridge.call_task)
            bridge.muted = False
            bridge.set_state(face="error")
            # Hang up between the accepted wake and subprocess creation.
            with patch("backend.asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
                await bridge.command({"action": "call"})
                task = bridge.call_task
                self.assertEqual(bridge.state["call"], "connecting")
                await bridge.command({"action": "call"})
                self.assertIs(task, bridge.call_task)
                await bridge.command({"action": "start"})
                await bridge.command({"action": "stop"})
                await task
                spawn.assert_not_awaited()
            self.assertIsNone(bridge.recording)
            self.assertEqual((bridge.state["call"], bridge.state["face"]), ("off", "idle"))

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

    async def test_scoped_voice_reply_and_replay(self):
        s = object.__new__(ChatSession)
        s.bridge = FakeBridge()
        s.voice_chat, s.voice_replay = "side", ReplayScope()
        def event(name, **fields):
            return {"type": "event", "event": name,
                    "payload": {"session_id": "side", **fields}}
        def final(mid, row, **fields):
            return event("delta.message_done", message_id=mid, message_seq=row,
                         display_text=mid, **fields)
        def status(mid, value):
            return event("task.status", task_id=mid, status=value)
        async def open_stream():
            if not hasattr(s, "attempted"):
                s.attempted = True
                return False
            if s.voice_tracker and s.voice_tracker.acknowledged:
                s.on_voice_event(status("task1", "running"))
                s.on_voice_event(final("checking", 2))
                loop = asyncio.get_running_loop()
                loop.call_later(.35, s.on_voice_event, final("actual answer", 3))
                loop.call_later(.40, s.on_voice_event, status("task1", "completed"))
            return True
        s.voice_tracker = None
        s.open_voice_stream = open_stream
        ack = {"session_id": "side", "is_thread": True, "message_id": "user1"}
        s.send_message = AsyncMock(return_value=ack)
        first = await s.voice_turn({"output_modality": "voice"})
        self.assertEqual(first.message_id, "actual answer")
        async def send_again(payload):
            s.on_voice_event(final("unrelated", 4))
            s.on_voice_event(event("message.user", message_id="user2", seq=5))
            s.on_voice_event(final("wrong parent", 6, reply_to_message_id="foreign"))
            s.on_voice_event(final("second", 7))
            s.on_voice_event(event("message.assistant", message_id="old", seq=1, display_text="old"))
            s.on_voice_event(event("message.user", message_id="someone else", seq=8))
            s.on_voice_event(final("another device", 9))
            return {**ack, "message_id": "user2"}
        s.send_message = send_again
        second = await s.voice_turn({"output_modality": "voice"})
        self.assertEqual((second.message_id, second.text), ("second", "second"))

    async def test_native_audio_uses_scoped_id_and_legacy_404(self):
        s = object.__new__(ChatSession)
        s.request = AsyncMock(side_effect=[(404, b""), (200, b"mp3")])
        self.assertEqual(await s.native_audio("id/a&b"), b"mp3")
        urls = [call.args[1] for call in s.request.await_args_list]
        self.assertEqual(urls, [prefix + "?message_id=id%2Fa%26b"
                                for prefix in ("/api/voice/tts-stream", "/voice/tts-stream")])

    async def test_voice_note_is_valid_mono_pcm_wav(self):
        pcm = struct.pack("<hhh", -32768, 0, 32767)
        note = make_voice_payload(pcm)
        self.assertEqual(note["output_modality"], "voice")
        self.assertEqual(note["message"], "")
        self.assertEqual([item["type"] for item in note["items"]], ["file"])
        raw = base64.b64decode(note["items"][0]["data_base64"])
        with wave.open(io.BytesIO(raw)) as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate()), (1, 2, 16000))
            self.assertEqual(w.readframes(3), pcm)

    async def test_quiet_microphone_is_boosted_without_amplifying_silence(self):
        for peak, expected in ((0, 0), (80, 80), (500, 8000), (5000, 16384), (22000, 22000)):
            with self.subTest(peak=peak):
                note = make_voice_payload(struct.pack("<hh", -peak, peak))
                wav = base64.b64decode(note["items"][0]["data_base64"])
                with wave.open(io.BytesIO(wav)) as w:
                    self.assertEqual(struct.unpack("<hh", w.readframes(2)), (-expected, expected))

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
