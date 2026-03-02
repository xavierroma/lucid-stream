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
    frame_timeout: float
    reactor_api_key: str
    youtube_rtmp_url: str
    start_prompt: str | None
    youtube_api_key: str | None
    youtube_video_id: str | None
    livekit_url: str
    livekit_api_url: str
    livekit_api_key: str
    livekit_api_secret: str
    livekit_room_name: str
    livekit_identity: str


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stream a Reactor remote video track to YouTube Live via LiveKit egress.",
    )
    parser.add_argument("--model-name", default="livecore")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--video-bitrate-kbps", type=int, default=2500)
    parser.add_argument("--audio-bitrate-kbps", type=int, default=128)
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument("--track-wait-timeout", type=float, default=30.0)
    parser.add_argument("--frame-timeout", type=float, default=10.0)
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
    parser.add_argument(
        "--livekit-room",
        default=None,
        help="LiveKit room name (or set LIVEKIT_ROOM_NAME).",
    )
    parser.add_argument(
        "--livekit-identity",
        default=None,
        help="LiveKit participant identity (or set LIVEKIT_PARTICIPANT_IDENTITY).",
    )
    parser.add_argument(
        "--livekit-api-url",
        default=None,
        help=(
            "LiveKit server API base URL (or set LIVEKIT_API_URL). "
            "Defaults to LIVEKIT_URL with wss->https and ws->http."
        ),
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


def derive_livekit_api_url(livekit_url: str) -> str:
    parsed = urlparse(livekit_url)
    if not parsed.netloc:
        raise ConfigError("LIVEKIT_URL must include scheme and host.")

    if parsed.scheme == "wss":
        scheme = "https"
    elif parsed.scheme == "ws":
        scheme = "http"
    elif parsed.scheme in {"https", "http"}:
        scheme = parsed.scheme
    else:
        raise ConfigError(
            "LIVEKIT_URL must use ws, wss, http, or https scheme.",
        )

    return f"{scheme}://{parsed.netloc}"


def build_config(args: argparse.Namespace, env: Mapping[str, str]) -> StreamConfig:
    reactor_api_key = env.get("REACTOR_API_KEY", "").strip()
    if not reactor_api_key:
        raise ConfigError("Missing REACTOR_API_KEY environment variable.")

    livekit_url = env.get("LIVEKIT_URL", "").strip()
    if not livekit_url:
        raise ConfigError("Missing LIVEKIT_URL environment variable.")

    livekit_api_key = env.get("LIVEKIT_API_KEY", "").strip()
    if not livekit_api_key:
        raise ConfigError("Missing LIVEKIT_API_KEY environment variable.")

    livekit_api_secret = env.get("LIVEKIT_API_SECRET", "").strip()
    if not livekit_api_secret:
        raise ConfigError("Missing LIVEKIT_API_SECRET environment variable.")

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
    if args.frame_timeout <= 0:
        raise ConfigError("--frame-timeout must be positive.")

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

    livekit_room_name = args.livekit_room or env.get("LIVEKIT_ROOM_NAME") or "reactor-youtube"
    livekit_room_name = livekit_room_name.strip()
    if not livekit_room_name:
        raise ConfigError("LiveKit room name cannot be empty.")

    livekit_identity = (
        args.livekit_identity
        or env.get("LIVEKIT_PARTICIPANT_IDENTITY")
        or "reactor-bridge"
    )
    livekit_identity = livekit_identity.strip()
    if not livekit_identity:
        raise ConfigError("LiveKit participant identity cannot be empty.")

    livekit_api_url = args.livekit_api_url or env.get("LIVEKIT_API_URL")
    if livekit_api_url:
        livekit_api_url = livekit_api_url.strip()
    if not livekit_api_url:
        livekit_api_url = derive_livekit_api_url(livekit_url)

    return StreamConfig(
        model_name=args.model_name,
        fps=args.fps,
        video_bitrate_kbps=args.video_bitrate_kbps,
        audio_bitrate_kbps=args.audio_bitrate_kbps,
        max_retries=args.max_retries,
        track_wait_timeout=args.track_wait_timeout,
        frame_timeout=args.frame_timeout,
        reactor_api_key=reactor_api_key,
        youtube_rtmp_url=resolve_youtube_url(env),
        start_prompt=start_prompt,
        youtube_api_key=youtube_api_key,
        youtube_video_id=youtube_video_id,
        livekit_url=livekit_url,
        livekit_api_url=livekit_api_url,
        livekit_api_key=livekit_api_key,
        livekit_api_secret=livekit_api_secret,
        livekit_room_name=livekit_room_name,
        livekit_identity=livekit_identity,
    )
