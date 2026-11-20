# Voice setup

Say **Hello Muse**, wait for the beep, then speak. Muse stays on the call for
follow-up questions. Wait until it finishes speaking before your next question.
Tap the microphone icon to hang up. Muting or returning to the desktop also
ends the call. The device disconnects after 45 seconds of inactivity or five
minutes total.

After installing the UI, enable hands-free calling on the X5:

```sh
sudo bash scripts/install-wakeword.sh
```

Sherpa detects the keyword locally. During a call the device streams its
microphone to Muse and plays Muse’s audio through the configured PulseAudio
sink. Muse handles language detection and turn-taking. The live call uses
the X5’s dedicated chat. If Muse reports another active call, hang up on the
other device and say the wake word again.

For a button-operated voice note, hold the microphone icon while the device
is idle and release to send. The X5 plays the returned MP3. Text-only replies
use the TTS fallback below.

## Optional MiMo TTS

Text-only replies use Edge TTS by default. To use Xiaomi MiMo as the first
text-to-speech fallback, create
`/etc/default/muse-ui-tts` with root-only permissions and add:

```text
MIMO_API_KEY=your-key-here
```

Restart `muse-ui.service` after editing. Do not commit the key. The backend
uses MiMo when text needs to be read aloud and falls back to Edge on errors.
It sends reply text to Xiaomi. Chinese and English default to the MiMo voices
`冰糖` and `Mia`; override with `MIMO_VOICE_ZH` and `MIMO_VOICE_EN` in the
same file. MiMo's availability and pricing are subject to Xiaomi's terms.
