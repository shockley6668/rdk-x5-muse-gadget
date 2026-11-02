#!/usr/bin/env python3
"""Local Hello Muse keyword detection and hands-free turn control."""
import logging
import os
from pathlib import Path
import select
import socket
import subprocess
import time
import json

import numpy as np
import sherpa_onnx

LOG = logging.getLogger("muse-wakeword")
RATE = 16000
CHUNK = 1280  # 80 ms, 16-bit mono
RUNTIME = Path(os.environ.get("MUSE_UI_RUNTIME", "/run/muse-ui"))
MODEL = Path(os.environ.get("MUSE_WAKE_MODEL", "/opt/muse-ui/wakeword-assets/model"))
STEM = "epoch-13-avg-2-chunk-16-left-64"
KWS_GAIN = float(os.environ.get("MUSE_WAKE_GAIN", "16"))


def state():
    try:
        return dict(line.split("=", 1) for line in (RUNTIME / "state").read_text().splitlines() if "=" in line)
    except OSError:
        return {}


def send(action):
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.sendto(json.dumps({"action": action}).encode(), str(RUNTIME / "input.sock"))


class TurnEnd:
    """Use speech activity and a pause, with no-speech and long-turn limits."""
    def __init__(self, started, vad):
        self.started, self.vad = started, vad
        self.pending = np.empty(0, dtype=np.float32)
        self.heard_speech = False
        self.vad.reset()

    def update(self, samples, now):
        audio = np.clip(samples.astype(np.float32) * (KWS_GAIN / 32768.0), -1, 1)
        self.pending = np.concatenate((self.pending, audio))
        while self.pending.size >= 512:
            self.vad.accept_waveform(self.pending[:512])
            self.pending = self.pending[512:]
            if self.vad.is_speech_detected():
                self.heard_speech = True
            if not self.vad.empty():
                self.vad.pop()
                return "stop"
        if not self.heard_speech and now - self.started >= 7:
            return "cancel"
        if self.heard_speech and now - self.started >= 25:
            return "stop"
        return None


def make_spotter():
    return sherpa_onnx.KeywordSpotter(
        tokens=str(MODEL / "tokens.txt"),
        encoder=str(MODEL / f"encoder-{STEM}.int8.onnx"),
        decoder=str(MODEL / f"decoder-{STEM}.onnx"),
        joiner=str(MODEL / f"joiner-{STEM}.int8.onnx"),
        keywords_file=str(MODEL / "hello-muse.txt"), num_threads=1,
        keywords_threshold=0.25, provider="cpu")


def make_vad():
    config = sherpa_onnx.VadModelConfig()
    config.silero_vad.model = str(MODEL / "silero_vad.onnx")
    config.silero_vad.threshold = 0.4
    config.silero_vad.min_speech_duration = 0.25
    config.silero_vad.min_silence_duration = 2.0
    config.silero_vad.max_speech_duration = 25.0
    config.sample_rate = RATE
    config.num_threads = 1
    return sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=30)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    spotter = make_spotter()
    vad = make_vad()
    proc = subprocess.Popen(["parecord", "--raw", "--rate=16000", "--channels=1",
                             "--format=s16le", "--latency-msec=80"], stdout=subprocess.PIPE,
                            bufsize=0)
    stream = spotter.create_stream()
    idle_since = None
    turn = None
    turn_ack_deadline = None
    pending = bytearray()
    LOG.info("Listening for Hello Muse")
    try:
        while True:
            ready, _, _ = select.select([proc.stdout], [], [], 3)
            if not ready:
                raise RuntimeError("Microphone stopped delivering audio")
            block = os.read(proc.stdout.fileno(), CHUNK * 2 - len(pending))
            if not block:
                raise RuntimeError("Microphone stream closed")
            pending.extend(block)
            if len(pending) < CHUNK * 2:
                continue
            raw = bytes(pending)
            pending.clear()
            samples = np.frombuffer(raw, dtype="<i2")
            now = time.monotonic()
            current = state()
            face = current.get("face")
            if turn is not None:
                if face == "listening":
                    turn_ack_deadline = None
                    result = turn.update(samples, now)
                    if result:
                        send(result)
                        LOG.info("Wake turn %s", result)
                        turn = None
                elif turn_ack_deadline is not None and now < turn_ack_deadline:
                    pass
                else:
                    # Another UI action may already have ended this turn.
                    turn = None
                continue
            eligible = current.get("link") == "online" and face == "idle"
            if not eligible:
                idle_since = None
                spotter.reset_stream(stream)
                continue
            if idle_since is None:
                idle_since = now
            if now - idle_since < 1.0:
                continue
            # The analog microphone is quiet on the assembled X5. Amplification
            # here only changes the local detector's input, not the voice note.
            audio = np.clip(samples.astype(np.float32) * (KWS_GAIN / 32768.0), -1, 1)
            stream.accept_waveform(RATE, audio)
            while spotter.is_ready(stream):
                spotter.decode_stream(stream)
            if spotter.get_result(stream):
                spotter.reset_stream(stream)
                # Pause detection during the tone and subsequent reply.
                tone = MODEL / "wake.wav"
                if tone.exists() and current.get("speaker") == "true":
                    subprocess.run(["paplay", str(tone)], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=3, check=False)
                # Discard audio accumulated during the cue so it cannot count
                # as the user's answer. The capture stream is local only.
                for _ in range(20):
                    if not select.select([proc.stdout], [], [], 0)[0]:
                        break
                    os.read(proc.stdout.fileno(), CHUNK * 2)
                send("start")
                turn = TurnEnd(time.monotonic(), vad)
                turn_ack_deadline = time.monotonic() + 2
                idle_since = None
                LOG.info("Hello Muse detected; listening for a request")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


if __name__ == "__main__":
    main()
