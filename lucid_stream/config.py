from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Mapping, Sequence
from urllib.parse import urlparse

from .constants import YOUTUBE_DEFAULT_RTMP_BASE
from .errors import ConfigError


@dataclass(frozen=True)
class StreamConfig:
    model_name: str
    fps: int
    video_bitrate_kbps: int
    audio_bitrate_kbps: int
    max_retries: int
    track_wait_timeout: float
    reactor_message_diagnostics: bool
    reactor_api_key: str
    youtube_rtmp_url: str
    start_prompt: str | None
    youtube_api_key: str | None
    youtube_video_id: str | None


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stream a Reactor remote video track to YouTube Live via reactor-egress.",
    )
    parser.add_argument("--model-name", default="livecore")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--video-bitrate-kbps", type=int, default=2500)
    parser.add_argument("--audio-bitrate-kbps", type=int, default=128)
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument("--track-wait-timeout", type=float, default=30.0)
    parser.add_argument(
        "--reactor-message-diagnostics",
        action="store_true",
        help="Log summarized Reactor state/event messages as they arrive.",
    )
    parser.add_argument(
        "--start-prompt",
        default=None,
        help="Prompt to schedule at frame 0 before sending Reactor start command.",
    )
    parser.add_argument(
        "--youtube-api-key",
        default=None,
        help="YouTube Data API key (or set YOUTUBE_API_KEY) for /prompt chat relay.",
    )
    parser.add_argument(
        "--youtube-video-id",
        default=None,
        help="YouTube live video ID (or set YOUTUBE_VIDEO_ID) for /prompt chat relay.",
    )
    return parser.parse_args(argv)


def validate_youtube_rtmp_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"rtmp", "rtmps"}:
        return

    host = parsed.netloc.lower()
    if "youtube.com" not in host:
        return

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise ConfigError(
            "YOUTUBE_RTMP_URL is missing the stream key. "
            "Expected: rtmp://a.rtmp.youtube.com/live2/<stream_key>",
        )


def resolve_youtube_url(env: Mapping[str, str]) -> str:
    direct_url = env.get("YOUTUBE_RTMP_URL", "").strip()
    if direct_url:
        validate_youtube_rtmp_url(direct_url)
        return direct_url

    stream_key = env.get("YT_STREAM_KEY", "").strip()
    if not stream_key:
        raise ConfigError(
            "Missing YouTube target. Set YOUTUBE_RTMP_URL or YT_STREAM_KEY.",
        )

    base_url = env.get("YT_RTMP_BASE", YOUTUBE_DEFAULT_RTMP_BASE).strip()
    base_url = base_url.rstrip("/")
    resolved_url = f"{base_url}/{stream_key}"
    validate_youtube_rtmp_url(resolved_url)
    return resolved_url


def parse_env_flag(env: Mapping[str, str], key: str) -> bool:
    raw = env.get(key, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def build_config(args: argparse.Namespace, env: Mapping[str, str]) -> StreamConfig:
    reactor_api_key = env.get("REACTOR_API_KEY", "").strip()
    if not reactor_api_key:
        raise ConfigError("Missing REACTOR_API_KEY environment variable.")

    if args.fps <= 0:
        raise ConfigError("--fps must be a positive integer.")
    if args.video_bitrate_kbps <= 0:
        raise ConfigError("--video-bitrate-kbps must be a positive integer.")
    if args.audio_bitrate_kbps <= 0:
        raise ConfigError("--audio-bitrate-kbps must be a positive integer.")
    if args.max_retries <= 0:
        raise ConfigError("--max-retries must be a positive integer.")
    if args.track_wait_timeout <= 0:
        raise ConfigError("--track-wait-timeout must be positive.")

    start_prompt = args.start_prompt
    if start_prompt is None:
        start_prompt = env.get("REACTOR_START_PROMPT")
    if start_prompt is not None:
        start_prompt = start_prompt.strip() or None

    youtube_api_key = args.youtube_api_key
    if youtube_api_key is None:
        youtube_api_key = env.get("YOUTUBE_API_KEY")
    youtube_api_key = youtube_api_key.strip() if youtube_api_key else None

    youtube_video_id = args.youtube_video_id
    if youtube_video_id is None:
        youtube_video_id = env.get("YOUTUBE_VIDEO_ID")
    youtube_video_id = youtube_video_id.strip() if youtube_video_id else None

    reactor_message_diagnostics = args.reactor_message_diagnostics or parse_env_flag(
        env,
        "REACTOR_MESSAGE_DIAGNOSTICS",
    )

    return StreamConfig(
        model_name=args.model_name,
        fps=args.fps,
        video_bitrate_kbps=args.video_bitrate_kbps,
        audio_bitrate_kbps=args.audio_bitrate_kbps,
        max_retries=args.max_retries,
        track_wait_timeout=args.track_wait_timeout,
        reactor_message_diagnostics=reactor_message_diagnostics,
        reactor_api_key=reactor_api_key,
        youtube_rtmp_url=resolve_youtube_url(env),
        start_prompt=start_prompt,
        youtube_api_key=youtube_api_key,
        youtube_video_id=youtube_video_id,
    )
