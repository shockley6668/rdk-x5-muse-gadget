#!/usr/bin/env python3
"""One Muse realtime call. Stdout carries only local UI events."""
import asyncio
import contextlib
import json
import logging
import math
import os
import signal
import time
from array import array

import numpy as np
import sounddevice as sd
from musegadget import config, identity, muse_api
from musegadget.link_client import DeviceDescription, LinkSession
from musegadget.streaming import HTTPStatusError
from musegadget.voice_audio import LiveAudio
from musegadget.voice_call import (FRAME_SAMPLES, SAMPLE_RATE, SETTLE_SECONDS, TranscriptDelta,
                                   TranscriptDone, VoiceCall, Working, pcm_rms)

from call_chat import device_chat_id


def event(**fields):
    print(json.dumps(fields, ensure_ascii=True), flush=True)


class CallAudio:
    def __init__(self, audio):
        self.audio = audio
        self.working = False
        self.playing_until = 0.
        self.last_state = None
        self.caption = ""
        self.response_id = None
        self.gain = float(os.environ.get("MUSE_CALL_GAIN", "12"))

    def observe(self, update):
        if isinstance(update, Working):
            self.working = update.working
        elif isinstance(update, (TranscriptDelta, TranscriptDone)):
            if update.response_id != self.response_id:
                self.caption = ""
                self.response_id = update.response_id
            self.caption = (update.text if isinstance(update, TranscriptDone)
                            else self.caption + update.text)[-380:]

    def write(self, pcm):
        queued = self.audio.write(pcm)
        if pcm_rms(pcm) >= 100:
            self.playing_until = time.monotonic() + queued / (SAMPLE_RATE * 2) + SETTLE_SECONDS
        return queued

    def flush(self):
        self.playing_until = 0.
        return self.audio.flush()

    async def frames(self):
        # VoiceCall first iterates the source after the server confirms admission.
        # LiveAudio.frames discards pre-admission audio and the local cue.
        for block in range(3):
            frame = array("h")
            for i in range(FRAME_SAMPLES):
                t = (block * FRAME_SAMPLES + i) / SAMPLE_RATE
                frame.append(round(4500 * math.sin(2 * math.pi * 800 * t)
                                   * min(1, t / .01, max(0, (.12 - t) / .01))))
            self.audio.write(frame.tobytes())
            await asyncio.sleep(FRAME_SAMPLES / SAMPLE_RATE)
        await asyncio.sleep(.25)
        async for pcm in self.audio.frames():
            speaking = time.monotonic() < self.playing_until
            face = "speaking" if speaking else "thinking" if self.working else "listening"
            state = (face, self.caption)
            if state != self.last_state:
                event(call="active", face=face, caption=self.caption)
                self.last_state = state
            samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
            yield np.clip(samples * self.gain, -32768, 32767).astype("<i2").tobytes()


async def run():
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    pairing = config.load_json(config.PAIRING_FILE)
    node = identity.load_or_create().node_id
    vms, status = await asyncio.wait_for(asyncio.to_thread(
        muse_api.fetch_vms_with_status, pairing["access_token"],
        muse_api.api_root(pairing.get("api_url_v2", ""))), muse_api.FETCH_TIMEOUT_S)
    if status != 200 or not vms:
        raise ConnectionError("paired VM unavailable")
    vm = next((vm for vm in vms if vm.get("is_default")), vms[0])
    session = LinkSession(noise_host=pairing.get("noise_host") or "hatch.metaaivm.com",
                          vm_id=vm["vm_id"], vm_auth_token=vm["vm_auth_token"],
                          device=DeviceDescription(node, "RDK X5 desktop pet", "1", {}),
                          register_device=False, app_id="hatch-web", run_command=lambda *_: {})
    stop = asyncio.Event()
    connection = asyncio.create_task(session.run(stop))
    try:
        await session.wait_transport_ready(15)
        thread = device_chat_id(node, vm["vm_id"])
        if not (await session.open_side_chat(thread)).exists:
            result = await session.send_chat("RDK X5 desktop pet voice chat.", thread)
            if not result["ok"]:
                event(error="Device chat unavailable")
                return 1
        device = next((i for i, d in enumerate(sd.query_devices())
                       if d["name"] == "pulse" and d["max_input_channels"] and d["max_output_channels"]), None)
        if device is None:
            raise OSError("PulseAudio device unavailable")
        sd.default.device = (device, device)
        with contextlib.closing(LiveAudio()) as live:
            audio = CallAudio(live)
            call = VoiceCall(session, device_id=node, thread_id=thread,
                             half_duplex=True, mic_threshold=850, idle_timeout=45,
                             overall_timeout=300, on_event=audio.observe)
            try:
                reason = await call.run(audio.frames(), audio, realtime_input=True)
                logging.info("Voice call ended (%s)", reason)
            finally:
                logging.info("Voice call admitted=%s end_ack=%s", call.ready.is_set(),
                             call.end_call_acknowledged)
    finally:
        stop.set()
        try:
            await asyncio.wait_for(connection, 3)
        except asyncio.TimeoutError:
            pass
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    try:
        raise SystemExit(asyncio.run(run()))
    except asyncio.CancelledError:
        pass
    except HTTPStatusError as exc:
        logging.warning("Call admission HTTP %s", exc.status)
        event(error="Muse is on another call" if exc.status == 409 else "Call unavailable")
        raise SystemExit(1)
    except Exception as exc:
        logging.warning("Call failed (%s)", type(exc).__name__)
        event(error="Audio unavailable" if isinstance(exc, OSError) and not isinstance(exc, ConnectionError)
              else "Call unavailable")
        raise SystemExit(1)
