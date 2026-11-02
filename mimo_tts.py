"""Optional MiMo speech synthesis; callers fall back to Edge on failure."""
import base64
import io
import json
import os
import urllib.request
import wave

ENDPOINT = "https://api.xiaomimimo.com/v1/chat/completions"


def synthesize(text, api_key, *, opener=urllib.request.urlopen):
    # Pick a native voice when the text is predominantly Chinese; both voices
    # may still speak mixed text, but quality needs a real-device evaluation.
    chinese = any("\u4e00" <= c <= "\u9fff" for c in text)
    voice = os.environ.get("MIMO_VOICE_ZH", "冰糖") if chinese else os.environ.get("MIMO_VOICE_EN", "Mia")
    payload = {"model": "mimo-v2.5-tts", "messages": [
        {"role": "user", "content": "Speak naturally, warm and conversationally."},
        {"role": "assistant", "content": text}], "audio": {"format": "wav", "voice": voice}}
    req = urllib.request.Request(ENDPOINT, data=json.dumps(payload, ensure_ascii=False).encode(),
                                 headers={"Authorization": "Bearer " + api_key,
                                          "Content-Type": "application/json"}, method="POST")
    with opener(req, timeout=45) as response:
        raw = response.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError("MiMo reply too large")
    data = json.loads(raw)["choices"][0]["message"]["audio"]["data"]
    audio = base64.b64decode(data, validate=True)
    with wave.open(io.BytesIO(audio), "rb") as wav:
        if (wav.getnchannels() not in (1, 2) or wav.getsampwidth() != 2 or
                wav.getcomptype() != "NONE" or wav.getnframes() < 1):
            raise ValueError("MiMo reply is not playable audio")
    return audio
