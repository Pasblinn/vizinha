# Contributing

Thanks for taking a look. This project exists because cheap cameras are sold with a
paid cloud attached, and the most valuable contribution is usually **a report from a
camera model nobody has tested yet**.

## Reporting a camera

Open an issue with the output of:

```bash
python3 rtsp_probe.py --describe
```

It prints the RTSP handshake and the SDP, which is exactly what is needed to tell
whether a model works, needs a different `CAMERA_PATH`, or needs a new workaround.
Include the brand, the model number printed on the device, and the app it pairs with.

**Redact before pasting.** The output contains your camera's realm and nonce; do not
include your password.

## Bugs

Include:

- what you expected and what happened
- the relevant part of `yoosee-nvr.log` (it redacts passwords, but read it before pasting)
- your OS, Python version, and `ffmpeg -version`
- your `.env` **with every secret removed**

## Pull requests

```bash
git checkout -b fix/short-description
```

Before opening the PR:

- **Run it.** Recording, backup and the dashboard involve a real camera, a real disk
  and a real browser — an import that succeeds proves none of them. Say in the PR
  what you actually exercised, and what you could not.
- Match the surrounding style: standard library first, no new dependency unless it
  earns its place, comments that explain *why* rather than restating the code.
- Keep secrets out of logs. Use `config.rtsp_url(redacted=True)` in anything that
  gets logged or printed.
- Fail loudly on bad configuration. A missing setting should stop startup, not
  produce a recorder that quietly records nothing.

Conventional Commits for messages (`fix:`, `feat:`, `docs:`, `chore:`).

## Things that would genuinely help

- Compatibility reports and fixes for other camera models
- Tests — there is no suite yet; `scheduler.is_recording_hour` and
  `audio_level.samples_to_db` are pure functions and the natural place to start
- Motion detection as a recording trigger, to cut storage further
- A Docker image, so the whole thing is one `compose up`
- Documentation fixes, including from people for whom English is a second language

## Code of conduct

Be decent. Assume good faith. Someone filing an unclear issue is trying to help.
