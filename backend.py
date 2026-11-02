#!/usr/bin/env python3
"""Live Muse chat/audio bridge for the production LVGL gadget UI.

Keeps the existing Linux link daemon and its device identity intact. Opens a
separate encrypted chat connection, as the ESP32 firmware does. Pairing is read
in place; this process never rotates tokens or writes the SDK's state.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import logging
import math
import os
from pathlib import Path
import signal
import socket
import struct
import time
import uuid
import unicodedata
import wave

from musegadget import muse_api
from musegadget.identity import Identity
from musegadget.link_client import DeviceDescription, LinkSession, _Request
from musegadget.noise import Header
from pet import PetEngine

LOG = logging.getLogger("muse-ui")
RUNTIME = Path(os.environ.get("MUSE_UI_RUNTIME", "/run/muse-ui"))
PAIRING = Path("/var/lib/musegadget/pairing.json")
IDENTITY = Path("/var/lib/musegadget/identity.json")
LANGUAGE_HINT = (
    "My spoken languages are Mandarin Chinese and English, sometimes mixed. "
    "Interpret the attached voice note using those languages. Reply in the "
    "language I used, with Simplified Chinese for Mandarin. If the audio is "
    "unclear, ask me to repeat rather than guessing another language."
)
PET_HINT = (
    "This is the RDK X5 desktop pet. For my requests to control it, use the "
    "paired Linux gadget's system.run tool with petctl. Available commands: "
    "petctl wake; petctl sleep; petctl pet; petctl move left|right|stop; "
    "petctl feed banana; petctl timer start MINUTES; petctl timer cancel; "
    "petctl find OBJECT; petctl status. Read petctl status after a command "
    "before claiming it worked. Object search uses the active detector's "
    "labels (COCO by default); do not claim open-vocabulary vision is running."
)


class Subscription:
    def __init__(self, session):
        self.session = session
        self.done = asyncio.get_running_loop().create_future()
        self.done.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        self.buffer = bytearray()

    def on_frame(self, frame):
        if frame.kind == "reset":
            raise ConnectionError("chat subscription reset")
        if frame.kind == "response":
            if frame.value.status >= 400:
                raise ConnectionError(f"chat subscription HTTP {frame.value.status}")
            self.session.ready.set()
            data, ended = frame.value.body, frame.value.end_body
        else:
            data, ended = frame.value.data, frame.value.end_body
        self.buffer.extend(data)
        if len(self.buffer) > 2 * 1024 * 1024:
            raise ValueError("chat subscription line exceeded limit")
        while b"\n" in self.buffer:
            line, _, rest = self.buffer.partition(b"\n")
            self.buffer = bytearray(rest)
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            self.session.on_event(event)
        if ended:
            raise ConnectionError("chat subscription ended")


class ChatSession(LinkSession):
    def __init__(self, vm, pairing, identity, bridge):
        super().__init__(
            noise_host=pairing.get("noise_host") or "hatch.metaaivm.com",
            vm_id=vm["vm_id"], vm_auth_token=vm["vm_auth_token"],
            device=DeviceDescription(identity.node_id, "Muse RDK X5 UI", "0.1.0", {}),
            run_command=lambda *_: {},
        )
        self.bridge = bridge
        self.ready = asyncio.Event()
        self.user_ids = set()
        self.messages = {}
        self.early_events = []
        self.turn_done = None
        self.acknowledged = False
        self.last_seq = 0

    async def _open_control_stream(self):
        # The paired Linux daemon owns /link-control and executes board commands.
        self._stream_id = -1
        encrypted = self._transport.encrypt_http_request(
            "POST", "/chat/subscribe", b"{}",
            headers=[Header("Content-Type", "application/json"),
                     Header("Accept", "application/x-ndjson")],
        )
        self._requests[encrypted.stream_id] = Subscription(self)
        await self._send_frames(encrypted.frames)

    async def post(self, payload):
        payload = {**payload, "output_modality": "text", "device_id": self._device.node_id}
        encrypted = self._transport.encrypt_http_request(
            "POST", "/chat/stream", json.dumps(payload).encode(),
            headers=[Header("Content-Type", "application/json"),
                     Header("x-request-id", str(uuid.uuid4())),
                     Header("x-app-id", "musegadget")],
        )
        request = _Request(asyncio.get_running_loop().create_future())
        self._requests[encrypted.stream_id] = request
        try:
            await self._send_frames(encrypted.frames)
            status, raw = await asyncio.wait_for(request.done, 60)
        finally:
            self._requests.pop(encrypted.stream_id, None)
        if not 200 <= status < 300:
            raise ConnectionError(f"chat message HTTP {status}")
        ack = json.loads(raw)
        ack = ack.get("result", ack)
        self.user_ids = {ack[k] for k in ("message_id", "reply_to_message_id") if ack.get(k)}
        if not self.user_ids:
            raise ValueError("chat acknowledged without a message id")
        self.acknowledged = True
        events, self.early_events = self.early_events, []
        for event in events:
            self.consume(event)
        LOG.info("chat message acknowledged")

    def on_event(self, event):
        if not isinstance(event, dict) or event.get("type") != "event":
            return
        seq = event.get("seq", 0)
        if isinstance(seq, int) and seq > 0:
            if seq <= self.last_seq:
                return
            self.last_seq = seq
        if self.turn_done is None:
            self.bridge.mirror_event(event)
            return
        if self.turn_done.done():
            return
        if not self.acknowledged:
            self.early_events.append(event)
            self.early_events = self.early_events[-128:]
        else:
            self.consume(event)

    def consume(self, event):
        name = event.get("event", "")
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            return
        if name not in ("delta.message_start", "delta.text_append",
                        "delta.message_done", "message.assistant"):
            return
        mid = payload.get("message_id") or event.get("message_id") or payload.get("id")
        if not mid:
            return
        parent = payload.get("reply_to_message_id") or payload.get("parent_message_id")
        if mid not in self.messages:
            if parent and parent not in self.user_ids and parent not in self.messages:
                return
            self.messages[mid] = ""
        if name == "delta.text_append":
            self.messages[mid] = (self.messages[mid] + str(payload.get("text", "")))[:20000]
            self.bridge.set_state(caption=self.messages[mid][:380], face="thinking")
        if name in ("delta.message_done", "message.assistant"):
            if name == "message.assistant" and payload.get("display_text_ready") is False:
                return
            text = payload.get("display_text") or payload.get("content") or self.messages[mid]
            if isinstance(text, str) and text.strip() and not self.turn_done.done():
                self.turn_done.set_result(text)

    async def turn(self, payload):
        self.user_ids.clear()
        self.messages.clear()
        self.early_events.clear()
        self.acknowledged = False
        self.turn_done = asyncio.get_running_loop().create_future()
        try:
            await self.post(payload)
            return await asyncio.wait_for(self.turn_done, 180)
        finally:
            self.turn_done = None


def make_voice_payload(pcm):
    wav = io.BytesIO()
    with wave.open(wav, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(pcm)
    return {"message": LANGUAGE_HINT + "\n" + PET_HINT, "items": [{
        "type": "file", "mime_type": "audio/wav", "filename": "voice_note.wav",
        "data_base64": base64.b64encode(wav.getvalue()).decode("ascii"),
    }]}


def audio_command(*args):
    routing = [f"{key}={os.environ[key]}" for key in ("PULSE_SOURCE", "PULSE_SINK")
               if os.environ.get(key)]
    return ["runuser", "-u", "sunrise", "--", "env", "-u", "MIMO_API_KEY",
            "XDG_RUNTIME_DIR=/run/user/1000", *routing, *args]


def caption_pages(text, width=27, lines=2):
    """Pages sized for the screen's 16 px font (CJK needs two cells)."""
    pages = []
    start = 0
    column = row = 0
    for i, char in enumerate(text):
        cell = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if char == "\n" or column + cell > width:
            row += 1
            column = 0
        if row >= lines:
            pages.append((start, i, text[start:i].strip()))
            start, row = i, 0
        if char != "\n":
            column += cell
    if start < len(text):
        pages.append((start, len(text), text[start:].strip()))
    return pages


class Bridge:
    def __init__(self, pet_engine=None):
        self.session = None
        self.stop = asyncio.Event()
        self.chat_enabled = os.environ.get("MUSE_UI_CHAT", "on") != "off"
        self.audio_enabled = os.environ.get("MUSE_UI_AUDIO", "off") == "on"
        self.mirror_enabled = os.environ.get("MUSE_UI_MIRROR", "0") == "1"
        self.state = dict(face="boot", caption="", level="0",
                          battery="-1", usb="true", ble="off", paired="true",
                          wifi="connecting", link="connecting",
                          audio="true" if self.audio_enabled else "false",
                          speaker="true" if self.audio_enabled else "false")
        self.pet = pet_engine or PetEngine()
        self.state.update(self.pet.state_fields())
        self.muted = not self.audio_enabled
        self.mirror_messages = {}
        self.mirror_done_ids = []
        self.mirror_task = None
        self.recording = None
        self.record_task = None
        self.turn_task = None
        self.player = None
        self._written_state = None
        self._written_at = 0.0
        self.write_state()

    def write_state(self):
        temp = RUNTIME / "state.tmp"
        # The UI's line protocol carries literal UTF-8, with captions on one line.
        values = dict(self.state)
        # LVGL captions have a 400-byte buffer; don't split a CJK code point.
        values["caption"] = str(values["caption"]).encode("utf-8")[:380].decode("utf-8", errors="ignore")
        payload = "".join(f"{k}={str(v).replace(chr(10), ' ').replace(chr(13), ' ')}\n"
                          for k, v in values.items())
        now = time.monotonic()
        # Keep a liveness heartbeat, but don't replace an unchanged file at 10 Hz.
        if payload == self._written_state and now - self._written_at < 5.0:
            return
        temp.write_text(payload, encoding="utf-8")
        temp.chmod(0o644)
        temp.replace(RUNTIME / "state")
        self._written_state, self._written_at = payload, now

    def set_state(self, **values):
        self.state.update(values)
        self.write_state()

    def update_pet_state(self):
        """Mirror the engine into the shared state file without owning UI state."""
        self.state.update(self.pet.state_fields())
        self.write_state()

    def mirror_event(self, event):
        """Display live main-chat replies spoken by the companion ESP32."""
        if not self.mirror_enabled:
            return
        name = event.get("event", "")
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            return
        mid = payload.get("message_id") or event.get("message_id") or payload.get("id")
        if name == "message.user":
            if self.mirror_task:
                self.mirror_task.cancel()
            self.set_state(face="thinking", caption="ESP32 to Muse / Thinking", level="0")
            return
        if not mid or name not in ("delta.message_start", "delta.text_append",
                                   "delta.message_done", "message.assistant"):
            return
        if mid in self.mirror_done_ids:
            return
        if mid not in self.mirror_messages:
            self.mirror_messages[mid] = ""
            if len(self.mirror_messages) > 32:
                self.mirror_messages.pop(next(iter(self.mirror_messages)))
        if name in ("delta.message_start", "delta.text_append"):
            if self.mirror_task:
                self.mirror_task.cancel()
            if name == "delta.text_append":
                self.mirror_messages[mid] = (self.mirror_messages[mid] +
                                            str(payload.get("text", "")))[:16000]
            pages = caption_pages(self.mirror_messages[mid])
            self.set_state(face="thinking", level="0",
                           caption=pages[-1][2] if pages else "Muse is replying")
        else:
            if name == "message.assistant" and payload.get("display_text_ready") is False:
                return
            text = payload.get("display_text") or payload.get("content") or self.mirror_messages[mid]
            if isinstance(text, str) and text.strip():
                self.mirror_done_ids = (self.mirror_done_ids + [mid])[-32:]
                if self.mirror_task:
                    self.mirror_task.cancel()
                self.mirror_task = asyncio.create_task(self.show_mirror_reply(text))

    async def show_mirror_reply(self, text):
        # The ESP32 plays the audio. Paging is paced for reading, not audio sync.
        for _, _, page in caption_pages(text):
            self.set_state(face="speaking", caption=page, level="0")
            await asyncio.sleep(max(2.5, min(7, len(page) / 8)))
        self.set_state(face="idle", level="0")

    async def connect_loop(self):
        while not self.stop.is_set():
            try:
                pairing = json.loads(PAIRING.read_text())
                identity = Identity(json.loads(IDENTITY.read_text())["mac"])
                vms, status = await asyncio.to_thread(
                    muse_api.fetch_vms_with_status, pairing["access_token"],
                    muse_api.api_root(pairing.get("api_url_v2", "")))
                if not vms:
                    raise ConnectionError(f"Muse VM lookup HTTP {status}")
                vm = next((v for v in vms if v.get("is_default")), vms[0])
                session = ChatSession(vm, pairing, identity, self)
                runner = asyncio.create_task(session.run(self.stop))
                ready = asyncio.create_task(session.ready.wait())
                try:
                    done, _ = await asyncio.wait({runner, ready}, timeout=30,
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if ready not in done:
                        if runner in done:
                            runner.result()
                        raise ConnectionError("Muse subscription not ready")
                    self.session = session
                    self.set_state(face="idle", caption="", wifi="connected", link="online")
                    LOG.info("live Muse chat subscription ready")
                    await runner
                finally:
                    ready.cancel()
                    runner.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await runner
                    self.session = None
                    if session.turn_done is not None and not session.turn_done.done():
                        session.turn_done.set_exception(ConnectionError("Muse disconnected"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("connection unavailable: %s", exc)
            self.set_state(face="error", caption="",
                           link="offline", wifi="connected")
            try:
                await asyncio.wait_for(self.stop.wait(), 5)
            except asyncio.TimeoutError:
                pass

    async def command(self, cmd):
        action = cmd.get("action")
        if action == "pet_command":
            name = cmd.get("command")
            params = cmd.get("params")
            if params is None:
                params = {key: value for key, value in cmd.items()
                          if key not in ("action", "command")}
            if not isinstance(name, str) or not isinstance(params, dict):
                raise ValueError("pet_command requires a command and object params")
            self.pet.command(name, **params)
            self.update_pet_state()
        elif action == "pet_event":
            event = cmd.get("event")
            if event is None:
                event = {key: value for key, value in cmd.items() if key != "action"}
            if not isinstance(event, dict):
                raise ValueError("pet_event requires an event object")
            result = self.pet.handle_event(event)
            if not result["accepted"]:
                LOG.debug("pet event rejected: %s", result["reason"])
            self.update_pet_state()
        elif action == "start":
            if not self.audio_enabled:
                return
            if self.recording or (self.turn_task and not self.turn_task.done()):
                return
            if not self.session:
                self.set_state(face="error", caption="Muse is offline")
                return
            self.recording = await asyncio.create_subprocess_exec(
                *audio_command("parecord", "--raw", "--rate=16000", "--channels=1",
                               "--format=s16le", "--latency-msec=100"), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            self.record_task = asyncio.create_task(self.capture(self.recording))
            self.set_state(face="listening", caption="", level="0")
        elif action == "stop" and self.recording:
            process, self.recording = self.recording, None
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            pcm = await self.record_task
            if len(pcm) < 16000:  # Less than half a second is unreliable ASR.
                self.set_state(face="idle", caption="", level="0")
            else:
                self.turn_task = asyncio.create_task(self.reply(make_voice_payload(pcm)))
        elif action == "cancel" and self.recording:
            process, self.recording = self.recording, None
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            await self.record_task
            self.set_state(face="idle", caption="", level="0")
        elif action == "text" and isinstance(cmd.get("message"), str):
            if self.turn_task and not self.turn_task.done():
                return
            self.turn_task = asyncio.create_task(self.reply({"message": cmd["message"][:10000]}))
        elif action == "mute":
            if not self.audio_enabled:
                return
            self.muted = not self.muted
            self.set_state(speaker="false" if self.muted else "true")
            if self.muted and self.player:
                with contextlib.suppress(ProcessLookupError):
                    self.player.terminate()

    async def capture(self, process):
        parts = []
        size = 0
        deadline = time.monotonic() + 45
        while size < 16000 * 2 * 45 and time.monotonic() < deadline:
            try:
                chunk = await asyncio.wait_for(process.stdout.read(1600), 1)
            except asyncio.TimeoutError:
                if process.returncode is not None:
                    break
                continue
            if not chunk:
                break
            parts.append(chunk)
            size += len(chunk)
            samples = struct.unpack("<" + "h" * (len(chunk) // 2), chunk[:len(chunk) // 2 * 2])
            rms = math.sqrt(sum(v*v for v in samples) / max(1, len(samples))) / 32768
            self.set_state(level=str(min(1.0, rms * 8)))
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
        await process.wait()
        pcm = b"".join(parts)
        if self.recording is process and not self.stop.is_set():
            self.recording = None
            if len(pcm) >= 16000:
                self.turn_task = asyncio.create_task(self.reply(make_voice_payload(pcm)))
            else:
                self.set_state(face="error", caption="Microphone unavailable", level="0")
        return pcm

    async def reply(self, payload):
        try:
            session = self.session
            if not session:
                raise ConnectionError("Muse is offline")
            self.set_state(face="thinking", caption="", level="0")
            text = await session.turn(payload)
            self.set_state(face="speaking", caption=text[:380], level="0.25")
            LOG.info("reply received (%d characters)", len(text))
            if not self.muted:
                await self.speak(text)
            self.set_state(face="idle", level="0")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("turn failed: %s", exc)
            self.set_state(face="error", caption=f"{type(exc).__name__}: {exc}"[:300], level="0")

    async def speak(self, text):
        import edge_tts
        mp3 = RUNTIME / "reply.mp3"
        wav = RUNTIME / "reply.wav"
        try:
            text = text[:12000]
            pages = caption_pages(text)
            boundaries = []
            mimo_key = os.environ.get("MIMO_API_KEY", "").strip()
            used_mimo = False
            if mimo_key:
                try:
                    from mimo_tts import synthesize
                    audio = await asyncio.to_thread(synthesize, text, mimo_key)
                    wav.write_bytes(audio)
                    used_mimo = True
                    LOG.info("MiMo TTS synthesized reply")
                except Exception as exc:
                    # Avoid logging credentials, request bodies or service errors.
                    LOG.warning("MiMo TTS failed (%s); using Edge TTS", type(exc).__name__)
            async def synthesize():
                cursor = 0
                with mp3.open("wb") as output:
                    async for part in edge_tts.Communicate(
                            text, "en-US-EmmaMultilingualNeural", boundary="WordBoundary").stream():
                        if part["type"] == "audio":
                            output.write(part["data"])
                        elif part["type"] in ("WordBoundary", "SentenceBoundary"):
                            pos = text.find(part.get("text", ""), cursor)
                            if pos >= 0:
                                boundaries.append((part["offset"] / 10000000, pos))
                                cursor = pos + len(part.get("text", ""))
            if not used_mimo:
                await asyncio.wait_for(synthesize(), 90)
                convert = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3), str(wav),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                _, err = await convert.communicate()
                if convert.returncode:
                    raise RuntimeError("TTS audio decode failed")
            wav.chmod(0o644)
            # Derive the mouth meter from the actual decoded playback audio.
            with wave.open(str(wav), "rb") as f:
                rate, channels = f.getframerate(), f.getnchannels()
                duration = f.getnframes() / rate
                pcm = f.readframes(f.getnframes())
            self.player = await asyncio.create_subprocess_exec(
                *audio_command("paplay", str(wav)), stderr=asyncio.subprocess.PIPE)
            playback = asyncio.create_task(self.player.communicate())
            started = time.monotonic()
            shown = -1
            while not playback.done():
                elapsed = time.monotonic() - started
                pos = (next((p for t, p in reversed(boundaries) if t <= elapsed), 0)
                       if boundaries else min(len(text) - 1, int(len(text) * elapsed / max(duration, .01))))
                page = next((i for i, (a, b, _) in enumerate(pages) if a <= pos < b), 0)
                a = int(elapsed * rate) * channels * 2
                sample = pcm[a:a + int(rate * .06) * channels * 2]
                values = struct.unpack("<" + "h" * (len(sample) // 2), sample)
                rms = math.sqrt(sum(v*v for v in values) / max(1, len(values))) / 32768
                update = dict(level=str(min(1, rms * 6)))
                if page != shown and pages:
                    shown = page
                    update["caption"] = pages[page][2]
                self.set_state(**update)
                await asyncio.sleep(.06)
            _, err = await playback
            if self.player.returncode and not self.muted:
                raise RuntimeError("audio output failed")
            LOG.info("TTS playback completed")
        except Exception as exc:
            LOG.warning("TTS unavailable: %s", exc)
            self.set_state(caption=(text[:280] + " [Audio unavailable]")[:380])
        finally:
            self.player = None
            mp3.unlink(missing_ok=True)
            wav.unlink(missing_ok=True)

    async def serve(self):
        path = RUNTIME / "input.sock"
        path.unlink(missing_ok=True)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.bind(str(path))
        sock.setblocking(False)
        path.chmod(0o660)
        import grp
        if os.geteuid() == 0:
            os.chown(path, 0, grp.getgrnam("sunrise").gr_gid)
        loop = asyncio.get_running_loop()
        connection = asyncio.create_task(self.connect_loop() if self.chat_enabled else self.stop.wait())
        if not self.chat_enabled:
            self.set_state(face="idle", link="offline", wifi="off")
        heartbeat = asyncio.create_task(self.heartbeat())
        try:
            while not self.stop.is_set():
                raw = await loop.sock_recv(sock, 16384)
                try:
                    cmd = json.loads(raw)
                    if isinstance(cmd, dict):
                        await self.command(cmd)
                except (ValueError, TypeError, OverflowError, OSError) as exc:
                    LOG.warning("input rejected: %s", type(exc).__name__)
        finally:
            self.stop.set()
            heartbeat.cancel()
            if self.recording:
                with contextlib.suppress(ProcessLookupError):
                    self.recording.terminate()
            player = self.player
            if self.player:
                with contextlib.suppress(ProcessLookupError):
                    self.player.terminate()
            if self.turn_task:
                self.turn_task.cancel()
            if self.mirror_task:
                self.mirror_task.cancel()
            # Await cleanup before asyncio.run closes TLS and subprocess handles.
            tasks = [heartbeat]
            if self.turn_task:
                tasks.append(self.turn_task)
            if self.mirror_task:
                tasks.append(self.mirror_task)
            if self.record_task:
                tasks.append(self.record_task)
            await asyncio.gather(*tasks, return_exceptions=True)
            if player:
                await player.wait()
            try:
                await asyncio.wait_for(connection, 15)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                connection.cancel()
                await asyncio.gather(connection, return_exceptions=True)
            sock.close()
            path.unlink(missing_ok=True)

    async def heartbeat(self):
        while not self.stop.is_set():
            await asyncio.sleep(0.1)
            self.update_pet_state()


async def main():
    RUNTIME.mkdir(mode=0o755, parents=True, exist_ok=True)
    bridge = Bridge()
    task = asyncio.create_task(bridge.serve())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    with contextlib.suppress(asyncio.CancelledError):
        await task


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main())
