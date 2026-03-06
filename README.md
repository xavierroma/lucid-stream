# lucid-stream

Stream Reactor remote video output to YouTube Live using `reactor-egress` (Reactor source + RTMP sink).

## Requirements

- Python 3.11+
- `uv`
- `ffmpeg` in `PATH`
- Reactor API key
- YouTube Live RTMP URL (or stream key)

## Install

This project is configured to use the local editable package at:

`/Users/xavierroma/projects/reactor-egress`

Sync dependencies:

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

If you want this script to send `schedule_prompt` + `start` to Reactor before streaming:

```bash
uv run python main.py --start-prompt "A neon cyberpunk city at night"
```

Alternative YouTube target (stream key + optional base URL):

```bash
REACTOR_API_KEY="rk_..." \
YT_STREAM_KEY="xxxx-xxxx-xxxx-xxxx-xxxx" \
uv run python main.py
```

## Environment Variables

Required:

- `REACTOR_API_KEY`
- one of:
  - `YOUTUBE_RTMP_URL` (preferred full ingest URL), or
  - `YT_STREAM_KEY` (+ optional `YT_RTMP_BASE`)

Optional:

- `REACTOR_START_PROMPT` (used if `--start-prompt` is not provided)
- `YOUTUBE_API_KEY` + `YOUTUBE_VIDEO_ID` (enable chat `/prompt` relay)
- `REACTOR_MESSAGE_DIAGNOSTICS` (`1`/`true` to log summarized Reactor state/event messages)

## CLI Options

```bash
uv run python main.py \
  --model-name livecore \
  --start-prompt "A calm ocean at sunrise" \
  --fps 30 \
  --video-bitrate-kbps 2500 \
  --audio-bitrate-kbps 128 \
  --youtube-api-key "AIza..." \
  --youtube-video-id "abc123xyz00" \
  --max-retries 5 \
  --track-wait-timeout 30 \
  --reactor-message-diagnostics
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
- Reactor readiness, source, and RTMP sink failures are retried.

## Troubleshooting

- `Missing REACTOR_API_KEY`: export `REACTOR_API_KEY`.
- `Missing YouTube target`: set either `YOUTUBE_RTMP_URL` or `YT_STREAM_KEY`.
- `YOUTUBE_RTMP_URL is missing the stream key`: include `/live2/<stream_key>` at the end.
- `ffmpeg not found in PATH`: install ffmpeg and ensure the binary is discoverable.
- `Timed out ... waiting for Reactor remote track`: Reactor session is connected but no video track yet.
- `Failed opening RTMP sink`: verify YouTube RTMP URL/key and outbound network connectivity.

### Reactor-only Probe

Use this to isolate Reactor output from the egress path:

```bash
uv run python scripts/reactor_probe.py
```
