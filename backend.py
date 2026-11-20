#!/usr/bin/env python3
"""Live Muse chat/audio bridge for the production LVGL gadget UI.

Keeps the existing Linux link daemon and its device identity intact. Opens a
separate encrypted chat connection, as the ESP32 firmware does. Pairing is read
in place; this process never rotates tokens or writes the SDK's state.
"""
from __future__ import annotations

import asyncio
from array import array
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
import sys
import time
import uuid
from urllib.parse import quote
import unicodedata
import wave

from musegadget import muse_api
from musegadget.identity import Identity
from musegadget.link_client import DeviceDescription, LinkSession, _Request
from musegadget.noise import Header
from musegadget.muse_turn import MuseTurn, ReplayScope, Reply
from call_chat import device_chat_id
from pet import PetEngine

LOG = logging.getLogger("muse-ui")
RUNTIME = Path(os.environ.get("MUSE_UI_RUNTIME", "/run/muse-ui"))
PAIRING = Path("/var/lib/musegadget/pairing.json")
IDENTITY = Path("/var/lib/musegadget/identity.json")


class Subscription:
    def __init__(self, on_event, ready, allow_missing=False):
        self.allow_missing = allow_missing
        self.on_event = on_event
        self.ready = ready
        self.done = asyncio.get_running_loop().create_future()
        self.done.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        self.buffer = bytearray()

    def on_frame(self, frame):
        if self.done.done():
            return
        if frame.kind == "reset":
            raise ConnectionError("chat subscription reset")
        if frame.kind == "response":
            if self.allow_missing and frame.value.status == 404:
                self.done.set_result(404)
                return
            if frame.value.status >= 400:
                raise ConnectionError(f"chat subscription HTTP {frame.value.status}")
            self.ready.set()
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
            self.on_event(event)
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
        self.early_events = []
        self.turn_done = None
        self.last_seq = 0
        self.voice_chat = device_chat_id(identity.node_id, vm["vm_id"])
        self.voice_stream = None
        self.voice_tracker = None
        self.voice_replay = ReplayScope()

    async def _open_control_stream(self):
        # The paired Linux daemon owns /link-control and executes board commands.
        self._stream_id = -1
        await self.subscribe({}, self.on_event, self.ready)

    async def subscribe(self, scope, on_event, ready):
        encrypted = self._transport.encrypt_http_request(
            "POST", "/chat/subscribe", json.dumps(scope).encode(),
            headers=[Header("Content-Type", "application/json"),
                     Header("Accept", "application/x-ndjson")],
        )
        self._requests[encrypted.stream_id] = Subscription(on_event, ready, allow_missing=bool(scope))
        await self._send_frames(encrypted.frames)
        return encrypted.stream_id

    async def request(self, method, path, body, headers, *, limit=1024 * 1024, timeout=60):
        encrypted = self._transport.encrypt_http_request(method, path, body, headers=headers)
        request = _Request(asyncio.get_running_loop().create_future(), max_bytes=limit)
        self._requests[encrypted.stream_id] = request
        try:
            await self._send_frames(encrypted.frames)
            return await asyncio.wait_for(request.done, timeout)
        except (asyncio.CancelledError, Exception):
            if self._requests.get(encrypted.stream_id) is request:
                with contextlib.suppress(Exception):
                    await self._send_frames(self._transport.encrypt_reset(encrypted.stream_id))
            raise
        finally:
            self._requests.pop(encrypted.stream_id, None)

    async def native_audio(self, message_id):
        for prefix in ("/api/voice/tts-stream", "/voice/tts-stream"):
            path = f"{prefix}?message_id={quote(message_id, safe='')}"
            status, data = await self.request("GET", path, b"", [
                Header("Accept", "audio/mpeg"), Header("x-request-id", str(uuid.uuid4())),
                Header("x-app-id", "musegadget"),
            ], limit=8 * 1024 * 1024, timeout=30)
            if status == 404 and prefix.startswith("/api/"):
                continue
            if not 200 <= status < 300 or not data:
                raise ConnectionError(f"Muse audio HTTP {status} ({len(data)} bytes)")
            return data

    async def send_message(self, payload):
        payload = {**payload, "device_id": self._device.node_id}
        status, raw = await self.request("POST", "/chat/stream", json.dumps(payload).encode(), [
            Header("Content-Type", "application/json"), Header("x-request-id", str(uuid.uuid4())),
            Header("x-app-id", "musegadget"),
        ])
        if not 200 <= status < 300:
            raise ConnectionError(f"chat message HTTP {status}")
        ack = json.loads(raw)
        return ack.get("result", ack)

    def handle_voice_updates(self, updates):
        for update in updates:
            if not isinstance(update, Reply) or not update.bound or self.turn_done.done():
                continue
            if update.text and not update.done:
                self.bridge.set_state(caption=update.text[:380], face="thinking")

    async def final_voice_reply(self):
        # A completed message can be an acknowledgement while its task still
        # runs. Wait for the turn to settle before choosing the final message.
        tracker = self.voice_tracker
        while not self.turn_done.done():
            now = time.monotonic()
            finished = tracker.settled(now) if tracker.task_finished else tracker.complete(now)
            if finished:
                for reply in reversed(list(tracker.replies.values())):
                    if reply.bound and reply.done:
                        return reply
                if tracker.task_finished:
                    raise ValueError("Muse finished without a reply")
            await asyncio.wait((self.turn_done,), timeout=0.25)
        return self.turn_done.result()

    async def open_voice_stream(self):
        if self.voice_stream is not None:
            return True
        ready = asyncio.Event()
        sid = await self.subscribe({"session_id": self.voice_chat}, self.on_voice_event, ready)
        subscription = self._requests[sid]
        opened = asyncio.create_task(ready.wait())
        try:
            await asyncio.wait((opened, subscription.done), timeout=12,
                               return_when=asyncio.FIRST_COMPLETED)
            if subscription.done.done() and subscription.done.result() == 404:
                self._requests.pop(sid, None)
                return False
            if not ready.is_set():
                raise TimeoutError("Muse chat subscription did not open")
            self.voice_stream = sid
            return True
        finally:
            opened.cancel()
            if sid != self.voice_stream and self._requests.pop(sid, None) is not None:
                await self._send_frames(self._transport.encrypt_reset(sid))

    def on_voice_event(self, event):
        data = event.get("payload")
        if not isinstance(data, dict) or (data.get("session_id") or event.get("session_id")) != self.voice_chat:
            return
        if self.voice_tracker is None or self.turn_done.done():
            self.voice_replay.observe_idle(event, self.voice_chat)
            return
        if not self.voice_tracker.acknowledged:
            if len(self.early_events) >= 64:
                self.turn_done.set_exception(ValueError("Too many chat events before acknowledgement"))
            else:
                self.early_events.append(event)
            return
        name = event.get("event")
        mid = data.get("message_id") or data.get("id") or event.get("message_id")
        if name == "message.user":
            row = data.get("seq", event.get("seq"))
            if not isinstance(row, int) or row < self.user_row:
                return
            self.user_row = row
            self.after_note = mid in self.user_ids
            return
        parent = data.get("reply_to_message_id") or data.get("parent_message_id")
        linked = parent in self.user_ids or parent in self.voice_tracker.replies
        if not self.after_note and not linked and mid not in self.voice_tracker.replies:
            return
        # Delta seq is per message. Its message_seq and the seq on stored
        # message rows share the chat ordering, including late history replays.
        row = (data.get("seq", event.get("seq")) if name == "message.assistant"
               else data.get("message_seq"))
        if isinstance(row, int) and row <= self.user_row:
            return
        self.handle_voice_updates(self.voice_tracker.feed(event))

    async def voice_turn(self, payload):
        # An existing chat must be followed before sending. Only a new chat
        # needs the first POST before a subscription is possible.
        exists = await self.open_voice_stream()
        self.user_ids = set()
        self.user_row = -1
        self.after_note = not exists
        self.early_events = []
        self.voice_tracker = MuseTurn(self.voice_chat, owns_chat=True, replay_scope=self.voice_replay)
        self.turn_done = asyncio.get_running_loop().create_future()
        try:
            ack = await self.send_message({**payload, "session_id": self.voice_chat})
            self.user_ids = {ack[k] for k in ("message_id", "reply_to_message_id") if ack.get(k)}
            self.voice_tracker.acknowledge(ack)
            for event in self.early_events:
                self.on_voice_event(event)
            if not exists and not await self.open_voice_stream():
                raise ConnectionError("Muse did not open the newly created chat")
            return await asyncio.wait_for(self.final_voice_reply(), 90)
        finally:
            self.voice_tracker.retire()
            self.voice_tracker = None
            self.early_events.clear()
            self.turn_done = None

    def on_event(self, event):
        if not isinstance(event, dict) or event.get("type") != "event":
            return
        payload = event.get("payload")
        if self.voice_tracker is not None or (isinstance(payload, dict) and payload.get("session_id") == self.voice_chat):
            return
        seq = event.get("seq", 0)
        if isinstance(seq, int) and seq > 0:
            if seq <= self.last_seq:
                return
            self.last_seq = seq
        self.bridge.mirror_event(event)

def make_voice_payload(pcm):
    # The assembled X5's analogue mic can peak below -35 dBFS. Raise quiet
    # recordings while leaving loud speech untouched and reserving headroom.
    samples = array("h")
    samples.frombytes(pcm)
    peak = max(map(abs, samples), default=0)
    if 128 <= peak < 16384:
        gain = min(16.0, 16384 / peak)
        pcm = array("h", (round(v * gain) for v in samples)).tobytes()
    wav = io.BytesIO()
    with wave.open(wav, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(pcm)
    return {"message": "", "output_modality": "voice", "device": "mcu-wearable",
            "items": [{
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
        self.call_sdk = Path(__file__).resolve().parent / "call-sdk"
        self.native_calls = (self.call_sdk / "musegadget" / "voice_call.py").is_file()
        self.call_task = None
        self.call_process = None
        self.call_stopping = False
        self.mirror_enabled = os.environ.get("MUSE_UI_MIRROR", "0") == "1"
        self.state = dict(face="boot", caption="", level="0",
                          call="off", wake_mode="call" if self.native_calls else "note",
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
        if not self.mirror_enabled or self.call_task:
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
                    self.set_state(wifi="connected", link="online",
                                   **({} if self.call_task else dict(face="idle", caption="")))
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
            self.set_state(link="offline", wifi="connected",
                           **({} if self.call_task else dict(face="idle", caption="", level="0")))
            try:
                await asyncio.wait_for(self.stop.wait(), 5)
            except asyncio.TimeoutError:
                pass

    def hangup(self):
        if self.call_task and not self.call_stopping:
            self.call_stopping = True
            self.set_state(call="ending", face="thinking", caption="", level="0")
            if self.call_process and self.call_process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    self.call_process.terminate()

    async def realtime_call(self):
        error = ""
        proc = None
        try:
            env = {key: os.environ[key] for key in (
                "PATH", "HOME", "LANG", "MUSE_CHAT_NAMESPACE", "PULSE_SOURCE", "PULSE_SINK",
                "MUSE_CALL_GAIN", "SSL_CERT_FILE", "SSL_CERT_DIR") if key in os.environ}
            env.update(PYTHONPATH=str(self.call_sdk), PYTHONUNBUFFERED="1",
                       PULSE_SERVER="unix:/run/user/1000/pulse/native",
                       PULSE_COOKIE="/home/sunrise/.config/pulse/cookie")
            if os.environ.get("MUSE_CALL_CA_FILE"):
                env.update(SSL_CERT_FILE=os.environ["MUSE_CALL_CA_FILE"],
                           SSL_CERT_DIR=str(self.call_sdk / "empty-cadir"))
            if self.call_stopping:
                return
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(Path(__file__).with_name("native_call.py")), env=env,
                stdout=asyncio.subprocess.PIPE)
            self.call_process = proc
            if self.call_stopping:
                proc.terminate()
            async for line in proc.stdout:
                msg = json.loads(line)
                if self.call_stopping:
                    continue
                if "error" in msg:
                    error = str(msg["error"])[:100]
                elif msg.get("call") == "active" and msg.get("face") in ("listening", "thinking", "speaking"):
                    self.set_state(call="active", face=msg["face"], caption=str(msg.get("caption",""))[:380],
                                   level="0.3" if msg["face"] == "speaking" else "0")
            if await proc.wait() and not self.call_stopping and not error:
                error = "Call unavailable"
        except Exception as exc:
            LOG.warning("Realtime call failed (%s)", type(exc).__name__)
            error = "Call unavailable"
        finally:
            if proc and proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 15)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            self.call_process = self.call_task = None
            self.call_stopping = False
            self.set_state(call="off", face="idle", caption=error, level="0")

    async def command(self, cmd):
        action = cmd.get("action")
        if action == "call":
            if (self.native_calls and self.audio_enabled and self.chat_enabled and not self.muted
                    and not self.call_task and not self.recording
                    and not (self.turn_task and not self.turn_task.done())):
                if self.mirror_task:
                    self.mirror_task.cancel()
                self.set_state(call="connecting", face="thinking", caption="Connecting call", level="0")
                self.call_task = asyncio.create_task(self.realtime_call())
            return
        if action == "hangup" or (self.call_task and action == "start"):
            self.hangup()
            return
        if self.call_task and action in ("text", "stop", "cancel"):
            return
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
                self.set_state(face="idle", caption="Muse is offline")
                return
            source = os.environ.get("PULSE_SOURCE")
            device = [f"--device={source}"] if source else []
            self.recording = await asyncio.create_subprocess_exec(
                *audio_command("parecord", *device, "--raw", "--rate=16000", "--channels=1",
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
            if self.muted:
                self.hangup()
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
                self.set_state(face="idle", caption="Muse is offline", level="0")
                return
            self.set_state(face="thinking", caption="", level="0")
            reply = await session.voice_turn({"output_modality": "text", **payload})
            text = reply.text
            native_id = reply.message_id if payload.get("output_modality") == "voice" else None
            self.set_state(face="speaking", caption=text[:380], level="0.25")
            LOG.info("reply received (%d characters)", len(text))
            if not self.muted:
                await self.speak(text, session=session, native_id=native_id)
            self.set_state(face="idle", level="0")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("turn failed (%s): %s", type(exc).__name__, exc)
            self.set_state(face="error", caption=f"{type(exc).__name__}: {exc}"[:300], level="0")

    async def decode_mp3(self, mp3, wav):
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "mp3", "-i", str(mp3),
            "-c:a", "pcm_s16le", str(wav), stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        try:
            if await proc.wait():
                raise ValueError("invalid reply MP3")
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    async def speak(self, text, *, session=None, native_id=None):
        mp3 = RUNTIME / "reply.mp3"
        wav = RUNTIME / "reply.wav"
        try:
            text = text[:12000]
            pages = caption_pages(text)
            boundaries = []
            mimo_key = os.environ.get("MIMO_API_KEY", "").strip()
            source = None
            if native_id:
                try:
                    mp3.write_bytes(await session.native_audio(native_id))
                    await self.decode_mp3(mp3, wav)
                    source = "Muse"
                except Exception as exc:
                    LOG.warning("Muse reply audio unavailable (%s)", exc)
                    if not text.strip():
                        raise
            if self.muted:
                return
            if not source and mimo_key:
                try:
                    from mimo_tts import synthesize
                    audio = await asyncio.to_thread(synthesize, text, mimo_key)
                    wav.write_bytes(audio)
                    source = "MiMo"
                    LOG.info("MiMo TTS synthesized reply")
                except Exception as exc:
                    # Avoid logging credentials, request bodies or service errors.
                    LOG.warning("MiMo TTS failed (%s); using Edge TTS", type(exc).__name__)
            async def synthesize():
                import edge_tts
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
            if not source:
                await asyncio.wait_for(synthesize(), 90)
                await self.decode_mp3(mp3, wav)
                source = "Edge"
            if self.muted:
                return
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
            if not self.muted:
                LOG.info("%s reply playback completed (%.2f s)", source, duration)
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
            if self.call_task:
                task = self.call_task
                self.hangup()
                try:
                    await asyncio.wait_for(task, 16)
                except asyncio.TimeoutError:
                    pass
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
