# lucid-stream

Stream a Reactor remote video track to YouTube Live using Python (`uv`) and `ffmpeg`.

## Requirements

- Python 3.11+
- `uv`
- `ffmpeg` available on `PATH`
- Reactor API key
- YouTube Live stream URL or stream key

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

Alternative (stream key + optional base URL):

```bash
REACTOR_API_KEY="rk_..." \
YT_STREAM_KEY="xxxx-xxxx-xxxx-xxxx-xxxx" \
uv run python main.py
```

Optional environment variable:

- `YT_RTMP_BASE` (default: `rtmp://a.rtmp.youtube.com/live2`)
- `REACTOR_START_PROMPT` (used if `--start-prompt` is not provided)
- `YOUTUBE_API_KEY` + `YOUTUBE_VIDEO_ID` (enable chat `/prompt` relay)

## CLI Options

```bash
uv run python main.py \
  --model-name livecore \
  --start-prompt "A calm ocean at sunrise" \
  --fps 30 \
  --video-bitrate-kbps 2500 \
  --youtube-api-key "AIza..." \
  --youtube-video-id "abc123xyz00" \
  --max-retries 5 \
  --track-wait-timeout 30 \
  --frame-timeout 10
```

## YouTube Chat Prompt Relay

If `YOUTUBE_API_KEY` and `YOUTUBE_VIDEO_ID` are configured (or equivalent CLI flags),
the streamer will poll live chat and process commands in this format:

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
- Frame timeouts and ffmpeg failures are retried.
- Incoming frames are reformatted to a stable output size for ffmpeg.
- Default video bitrate is CBR `2500 kbps` (`--video-bitrate-kbps`) for YouTube stability.

## Troubleshooting

- `Missing REACTOR_API_KEY`: export `REACTOR_API_KEY`.
- `Missing YouTube target`: set either `YOUTUBE_RTMP_URL` or `YT_STREAM_KEY`.
- `YOUTUBE_RTMP_URL is missing the stream key`: include `/live2/<stream_key>` at the end.
- `Timed out ... waiting for Reactor remote track`: Reactor session is connected but not yet publishing a remote output.
- `ffmpeg ... not found`: install ffmpeg and ensure it is on your `PATH`.
- Retries exhausted: check Reactor connectivity, stream health, and YouTube ingest key/URL.
