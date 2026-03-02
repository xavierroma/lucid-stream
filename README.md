# lucid-stream

Stream a Reactor remote video track to YouTube Live using LiveKit track publish + LiveKit egress.

## Requirements

- Python 3.11+
- `uv`
- Reactor API key
- LiveKit project/server credentials
- YouTube Live RTMP URL (or stream key)

## Install

```bash
uv sync
```

Create local env file:

```bash
cp .env.example .env
```

## Run

Preferred (full YouTube RTMP URL):

```bash
uv run python main.py
```

The script automatically loads `.env` (if present).

If you want this script to send `schedule_prompt` + `start` to Reactor before streaming:

```bash
uv run python main.py --start-prompt "A neon cyberpunk city at night"
```

Alternative YouTube target (stream key + optional base URL):

```bash
REACTOR_API_KEY="rk_..." \
LIVEKIT_URL="wss://your-project.livekit.cloud" \
LIVEKIT_API_KEY="..." \
LIVEKIT_API_SECRET="..." \
YT_STREAM_KEY="xxxx-xxxx-xxxx-xxxx-xxxx" \
uv run python main.py
```

## Environment Variables

Required:

- `REACTOR_API_KEY`
- `LIVEKIT_URL`
- `LIVEKIT_API_KEY`
- `LIVEKIT_API_SECRET`
- one of:
  - `YOUTUBE_RTMP_URL` (preferred full ingest URL), or
  - `YT_STREAM_KEY` (+ optional `YT_RTMP_BASE`)

Optional:

- `LIVEKIT_ROOM_NAME` (default: `reactor-youtube`)
- `LIVEKIT_PARTICIPANT_IDENTITY` (default: `reactor-bridge`)
- `LIVEKIT_API_URL` (defaults from `LIVEKIT_URL`, mapping `wss->https`, `ws->http`)
- `REACTOR_START_PROMPT` (used if `--start-prompt` is not provided)
- `YOUTUBE_API_KEY` + `YOUTUBE_VIDEO_ID` (enable chat `/prompt` relay)

## CLI Options

```bash
uv run python main.py \
  --model-name livecore \
  --start-prompt "A calm ocean at sunrise" \
  --fps 30 \
  --video-bitrate-kbps 2500 \
  --audio-bitrate-kbps 128 \
  --livekit-room reactor-youtube \
  --livekit-identity reactor-bridge \
  --youtube-api-key "AIza..." \
  --youtube-video-id "abc123xyz00" \
  --max-retries 5 \
  --track-wait-timeout 30 \
  --frame-timeout 10
```

## YouTube Chat Prompt Relay

If `YOUTUBE_API_KEY` and `YOUTUBE_VIDEO_ID` are configured (or equivalent CLI flags),
the streamer polls live chat and processes commands in this format:

```text
/prompt <new prompt text>
```

Behavior:

- The command content is scheduled 30 frames ahead of the current frame.
- On `generation_reset`, the latest prompt is re-scheduled at frame `0` and `start` is sent.
- If LiveCore state telemetry is missing, chat prompts force `reset -> schedule_prompt(0) -> start`.

## Retry Behavior

- Retries up to `--max-retries` attempts (default `5`).
- Backoff schedule: `2s`, `4s`, `8s`, `16s`, `32s` for attempts 1-5.
- Frame timeouts, dimension changes, and LiveKit/egress failures are retried.

## Notes on Audio

Reactor currently exposes video remote track output in this flow. The script publishes a
generated silent stereo audio track into LiveKit so YouTube ingest stays stable and avoids
no-audio ingest warnings.

## Troubleshooting

- `Missing REACTOR_API_KEY`: export `REACTOR_API_KEY`.
- `Missing LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET`: set LiveKit credentials.
- `Missing YouTube target`: set either `YOUTUBE_RTMP_URL` or `YT_STREAM_KEY`.
- `YOUTUBE_RTMP_URL is missing the stream key`: include `/live2/<stream_key>` at the end.
- `Timed out ... waiting for Reactor remote track`: Reactor session is connected but no video track yet.
- Egress start failures: verify LiveKit server egress availability and YouTube RTMP URL/key.
- Retries exhausted: check Reactor connectivity, LiveKit credentials, and YouTube ingest key/URL.
