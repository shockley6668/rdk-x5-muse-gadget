# Voice setup

`Hello Muse` is detected locally using sherpa-onnx. After the beep, a local
Silero voice detector waits for speech and two seconds of silence, then sends
the recording to Muse. A seven-second timeout cancels an empty turn; a turn
is limited to 25 seconds. Press-to-talk still works. Wake detection pauses while
Muse is processing or speaking. This detects speech and pauses, not the meaning
of a sentence; long pauses can still end a turn early.

To enable it after installing the UI:

```sh
sudo bash scripts/install-wakeword.sh
```

The installer downloads the Sherpa 3M model and Silero VAD and enables
`muse-wakeword.service`. Set `MUSE_WAKE_GAIN` in a systemd service override to
tune the detector for your microphone. Wake detection runs only while the
backend is connected. Muse dialogue and the default Edge TTS require internet.

## Optional MiMo TTS

By default, replies use Edge TTS. To try Xiaomi MiMo's online TTS, create
`/etc/default/muse-ui-tts` with root-only permissions and add:

```text
MIMO_API_KEY=your-key-here
```

Restart `muse-ui.service` after editing. Do not commit the key. The backend
uses MiMo for replies when a key is configured and falls back to Edge on errors.
It sends reply text to Xiaomi. Chinese and English default to the MiMo voices
`冰糖` and `Mia`; override with `MIMO_VOICE_ZH` and `MIMO_VOICE_EN` in the
same file. MiMo's availability and pricing are subject to Xiaomi's terms.
