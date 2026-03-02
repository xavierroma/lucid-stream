import argparse
import asyncio
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lucid_stream import runner as app


def make_args(**overrides: object) -> argparse.Namespace:
    args = app.parse_args([])
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def make_env(**overrides: str) -> dict[str, str]:
    env = {
        "REACTOR_API_KEY": "rk_test",
        "LIVEKIT_URL": "wss://unit.livekit.cloud",
        "LIVEKIT_API_KEY": "lk_key",
        "LIVEKIT_API_SECRET": "lk_secret",
        "YOUTUBE_RTMP_URL": "rtmp://a.rtmp.youtube.com/live2/key-123",
    }
    env.update(overrides)
    return env


def make_config(
    max_retries: int = 5,
    start_prompt: str | None = None,
    youtube_api_key: str | None = None,
    youtube_video_id: str | None = None,
) -> app.StreamConfig:
    return app.StreamConfig(
        model_name="livecore",
        fps=30,
        video_bitrate_kbps=2500,
        audio_bitrate_kbps=128,
        max_retries=max_retries,
        track_wait_timeout=1.0,
        frame_timeout=1.0,
        reactor_api_key="rk_test",
        youtube_rtmp_url="rtmp://a.rtmp.youtube.com/live2/key-123",
        start_prompt=start_prompt,
        youtube_api_key=youtube_api_key,
        youtube_video_id=youtube_video_id,
        livekit_url="wss://unit.livekit.cloud",
        livekit_api_url="https://unit.livekit.cloud",
        livekit_api_key="lk_key",
        livekit_api_secret="lk_secret",
        livekit_room_name="reactor-youtube",
        livekit_identity="reactor-bridge",
    )


def test_resolve_youtube_url_prefers_direct_url() -> None:
    env = {
        "YOUTUBE_RTMP_URL": "rtmp://direct/url",
        "YT_STREAM_KEY": "stream-key",
        "YT_RTMP_BASE": "rtmp://ignored/base",
    }
    assert app.resolve_youtube_url(env) == "rtmp://direct/url"


def test_resolve_youtube_url_uses_stream_key_and_base() -> None:
    env = {
        "YT_STREAM_KEY": "stream-key",
        "YT_RTMP_BASE": "rtmp://custom/base/",
    }
    assert app.resolve_youtube_url(env) == "rtmp://custom/base/stream-key"


def test_derive_livekit_api_url_from_wss() -> None:
    assert app.derive_livekit_api_url("wss://abc.livekit.cloud") == "https://abc.livekit.cloud"


def test_derive_livekit_api_url_from_ws() -> None:
    assert app.derive_livekit_api_url("ws://localhost:7880") == "http://localhost:7880"


def test_build_config_requires_livekit_key() -> None:
    args = make_args()
    env = make_env()
    del env["LIVEKIT_API_KEY"]
    with pytest.raises(app.ConfigError):
        app.build_config(args=args, env=env)


def test_build_config_uses_cli_start_prompt_over_env() -> None:
    args = make_args(start_prompt="cli prompt")
    config = app.build_config(
        args=args,
        env=make_env(REACTOR_START_PROMPT="env prompt"),
    )
    assert config.start_prompt == "cli prompt"


def test_build_config_derives_livekit_api_url_when_missing() -> None:
    args = make_args()
    config = app.build_config(
        args=args,
        env=make_env(LIVEKIT_URL="wss://my.livekit.cloud"),
    )
    assert config.livekit_api_url == "https://my.livekit.cloud"


def test_build_track_composite_request_has_rtmp_and_bitrates() -> None:
    request = app.build_track_composite_request(
        config=make_config(),
        width=832,
        height=480,
        video_track_id="TR_VIDEO",
        audio_track_id="TR_AUDIO",
    )
    assert request.room_name == "reactor-youtube"
    assert request.video_track_id == "TR_VIDEO"
    assert request.audio_track_id == "TR_AUDIO"
    assert len(request.stream_outputs) == 1
    assert request.stream_outputs[0].urls[0] == "rtmp://a.rtmp.youtube.com/live2/key-123"
    assert request.advanced.framerate == 30
    assert request.advanced.video_bitrate == 2_500_000
    assert request.advanced.audio_bitrate == 128_000


def test_extract_prompt_command_parses_valid_command() -> None:
    assert app.extract_prompt_command("/prompt  cinematic skyline") == "cinematic skyline"


def test_extract_prompt_command_ignores_non_command_text() -> None:
    assert app.extract_prompt_command("hello chat") is None
    assert app.extract_prompt_command("/prompting test") is None
    assert app.extract_prompt_command("/prompt   ") is None


def test_is_generation_reset_event_accepts_data_event_field() -> None:
    message = {"type": "event", "data": {"event": "generation_reset"}}
    assert app.is_generation_reset_event(message) is True


def test_extract_paused_flag_reads_state_message() -> None:
    message = {"type": "state", "data": {"paused": True}}
    assert app.extract_paused_flag(message) is True


def test_run_with_retries_stops_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    async def fake_stream_once(config: app.StreamConfig, stop_event: asyncio.Event) -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("boom")

    async def fake_sleep_with_stop(seconds: float, stop_event: asyncio.Event) -> None:
        return None

    monkeypatch.setattr(app, "stream_once", fake_stream_once)
    monkeypatch.setattr(app, "sleep_with_stop", fake_sleep_with_stop)

    rc = asyncio.run(app.run_with_retries(make_config(max_retries=5), asyncio.Event()))
    assert rc == 1
    assert attempts == 5


def test_retry_backoff_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []

    async def fake_stream_once(config: app.StreamConfig, stop_event: asyncio.Event) -> None:
        raise RuntimeError("retry me")

    async def fake_sleep_with_stop(seconds: float, stop_event: asyncio.Event) -> None:
        delays.append(seconds)

    monkeypatch.setattr(app, "stream_once", fake_stream_once)
    monkeypatch.setattr(app, "sleep_with_stop", fake_sleep_with_stop)

    rc = asyncio.run(app.run_with_retries(make_config(max_retries=5), asyncio.Event()))
    assert rc == 1
    assert delays == [2, 4, 8, 16]


class _FakeArray:
    def __init__(self, size: int) -> None:
        self._size = size

    def tobytes(self) -> bytes:
        return b"\x01" * self._size


class _FakeFrame:
    def __init__(self, width: int, height: int, fmt: str = "rgb24") -> None:
        self.width = width
        self.height = height
        self.format = types.SimpleNamespace(name=fmt)

    def reformat(self, width: int, height: int, format: str) -> "_FakeFrame":
        self.width = width
        self.height = height
        self.format = types.SimpleNamespace(name=format)
        return self

    def to_ndarray(self) -> _FakeArray:
        return _FakeArray(self.width * self.height * 3)


def _install_fake_livekit(
    monkeypatch: pytest.MonkeyPatch,
    *,
    started_egress: list[str],
    stopped_egress: list[str],
    room_disconnect_calls: list[bool],
) -> None:
    class FakeTrackPublication:
        def __init__(self, sid: str) -> None:
            self.sid = sid

    class FakeLocalParticipant:
        async def publish_track(self, track: object, options: object) -> FakeTrackPublication:
            if getattr(track, "_kind", "") == "video":
                return FakeTrackPublication("TR_VIDEO")
            return FakeTrackPublication("TR_AUDIO")

    class FakeRoom:
        def __init__(self) -> None:
            self.local_participant = FakeLocalParticipant()

        async def connect(self, url: str, token: str) -> None:
            return None

        async def disconnect(self) -> None:
            room_disconnect_calls.append(True)

    class FakeVideoSource:
        def __init__(self, width: int, height: int) -> None:
            self.width = width
            self.height = height

        def capture_frame(self, frame: object, timestamp_us: int = 0) -> None:
            return None

    class FakeAudioSource:
        def __init__(self, sample_rate: int, num_channels: int, queue_size_ms: int = 1000) -> None:
            self.sample_rate = sample_rate
            self.num_channels = num_channels
            self.closed = False

        async def capture_frame(self, frame: object) -> None:
            return None

        async def aclose(self) -> None:
            self.closed = True

    class FakeLocalVideoTrack:
        @staticmethod
        def create_video_track(name: str, source: object) -> object:
            return types.SimpleNamespace(_kind="video", name=name, source=source)

    class FakeLocalAudioTrack:
        @staticmethod
        def create_audio_track(name: str, source: object) -> object:
            return types.SimpleNamespace(_kind="audio", name=name, source=source)

    class FakeTrackPublishOptions:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    class FakeVideoEncoding:
        def __init__(self, max_framerate: float, max_bitrate: int) -> None:
            self.max_framerate = max_framerate
            self.max_bitrate = max_bitrate

    class FakeAudioEncoding:
        def __init__(self, max_bitrate: int) -> None:
            self.max_bitrate = max_bitrate

    class FakeVideoFrame:
        def __init__(self, width: int, height: int, type: object, data: bytes) -> None:
            self.width = width
            self.height = height
            self.type = type
            self.data = data

    fake_rtc = types.SimpleNamespace(
        Room=FakeRoom,
        VideoSource=FakeVideoSource,
        AudioSource=FakeAudioSource,
        LocalVideoTrack=FakeLocalVideoTrack,
        LocalAudioTrack=FakeLocalAudioTrack,
        TrackPublishOptions=FakeTrackPublishOptions,
        VideoEncoding=FakeVideoEncoding,
        AudioEncoding=FakeAudioEncoding,
        VideoFrame=FakeVideoFrame,
        AudioFrame=object,
        VideoBufferType=types.SimpleNamespace(RGB24="RGB24"),
        TrackSource=types.SimpleNamespace(
            SOURCE_CAMERA="camera",
            SOURCE_MICROPHONE="microphone",
        ),
    )

    class FakeToken:
        def with_identity(self, identity: str) -> "FakeToken":
            return self

        def with_name(self, name: str) -> "FakeToken":
            return self

        def with_grants(self, grants: object) -> "FakeToken":
            return self

        def to_jwt(self) -> str:
            return "jwt-token"

    class FakeVideoGrants:
        def __init__(self, room_join: bool, room: str) -> None:
            self.room_join = room_join
            self.room = room

    class FakeStreamOutput:
        def __init__(self, protocol: object, urls: list[str]) -> None:
            self.protocol = protocol
            self.urls = urls

    class FakeEncodingOptions:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    class FakeTrackCompositeEgressRequest:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    class FakeStopEgressRequest:
        def __init__(self, egress_id: str) -> None:
            self.egress_id = egress_id

    class FakeEgressService:
        async def start_track_composite_egress(self, request: object) -> object:
            started_egress.append("EG_TEST")
            return types.SimpleNamespace(egress_id="EG_TEST")

        async def stop_egress(self, request: FakeStopEgressRequest) -> object:
            stopped_egress.append(request.egress_id)
            return types.SimpleNamespace()

    class FakeLiveKitAPI:
        def __init__(self, url: str, api_key: str, api_secret: str) -> None:
            self.url = url
            self.api_key = api_key
            self.api_secret = api_secret
            self.egress = FakeEgressService()
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    fake_lk_api = types.SimpleNamespace(
        AccessToken=lambda api_key, api_secret: FakeToken(),
        VideoGrants=FakeVideoGrants,
        StreamOutput=FakeStreamOutput,
        StreamProtocol=types.SimpleNamespace(RTMP="rtmp"),
        EncodingOptions=FakeEncodingOptions,
        TrackCompositeEgressRequest=FakeTrackCompositeEgressRequest,
        StopEgressRequest=FakeStopEgressRequest,
        LiveKitAPI=FakeLiveKitAPI,
    )

    monkeypatch.setattr(app, "rtc", fake_rtc)
    monkeypatch.setattr(app, "lk_api", fake_lk_api)


def _install_fake_reactor(
    monkeypatch: pytest.MonkeyPatch,
    *,
    frames: list[_FakeFrame],
    sent_commands: list[tuple[str, dict[str, object]]],
    disconnected: list[bool],
) -> None:
    class FakeTrack:
        def __init__(self, seq: list[_FakeFrame]) -> None:
            self._seq = seq
            self._idx = 0

        async def recv(self) -> _FakeFrame:
            frame = self._seq[self._idx]
            if self._idx < len(self._seq) - 1:
                self._idx += 1
            return frame

    class FakeReactor:
        def __init__(self, model_name: str, api_key: str) -> None:
            self.track = FakeTrack(frames)
            self.handlers: dict[str, object] = {}

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            disconnected.append(True)

        def get_remote_track(self) -> FakeTrack:
            return self.track

        def get_status(self) -> str:
            return "ready"

        async def send_command(self, command: str, data: dict[str, object]) -> None:
            sent_commands.append((command, data))

        def on(self, event: str, callback: object) -> None:
            self.handlers[event] = callback

        def off(self, event: str, callback: object) -> None:
            if self.handlers.get(event) is callback:
                del self.handlers[event]

    monkeypatch.setattr(app, "Reactor", FakeReactor)


@pytest.mark.filterwarnings("ignore:coroutine 'run_silent_audio_publisher' was never awaited")
def test_stream_once_dimension_change_triggers_retryable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent_commands: list[tuple[str, dict[str, object]]] = []
    disconnected: list[bool] = []
    started_egress: list[str] = []
    stopped_egress: list[str] = []
    room_disconnect_calls: list[bool] = []

    _install_fake_reactor(
        monkeypatch,
        frames=[_FakeFrame(64, 64), _FakeFrame(32, 32)],
        sent_commands=sent_commands,
        disconnected=disconnected,
    )
    _install_fake_livekit(
        monkeypatch,
        started_egress=started_egress,
        stopped_egress=stopped_egress,
        room_disconnect_calls=room_disconnect_calls,
    )

    async def fake_silent_audio(*, audio_source: object, stop_event: asyncio.Event) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(app, "run_silent_audio_publisher", fake_silent_audio)

    with pytest.raises(app.RetryableError):
        asyncio.run(app.stream_once(make_config(), asyncio.Event()))

    assert started_egress == ["EG_TEST"]
    assert stopped_egress == ["EG_TEST"]
    assert disconnected == [True]
    assert room_disconnect_calls == [True]


def test_stream_once_graceful_stop_cleans_up(monkeypatch: pytest.MonkeyPatch) -> None:
    sent_commands: list[tuple[str, dict[str, object]]] = []
    disconnected: list[bool] = []
    started_egress: list[str] = []
    stopped_egress: list[str] = []
    room_disconnect_calls: list[bool] = []
    stop_event = asyncio.Event()

    _install_fake_reactor(
        monkeypatch,
        frames=[_FakeFrame(64, 64)],
        sent_commands=sent_commands,
        disconnected=disconnected,
    )
    _install_fake_livekit(
        monkeypatch,
        started_egress=started_egress,
        stopped_egress=stopped_egress,
        room_disconnect_calls=room_disconnect_calls,
    )

    publish_calls = 0

    async def fake_publish_video_frame(
        video_source: object,
        frame: object,
        width: int,
        height: int,
        *,
        timestamp_us: int,
    ) -> None:
        nonlocal publish_calls
        publish_calls += 1
        stop_event.set()

    async def fake_silent_audio(*, audio_source: object, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await asyncio.sleep(0.01)

    monkeypatch.setattr(app, "publish_video_frame", fake_publish_video_frame)
    monkeypatch.setattr(app, "run_silent_audio_publisher", fake_silent_audio)

    with pytest.raises(app.GracefulStop):
        asyncio.run(app.stream_once(make_config(), stop_event))

    assert publish_calls >= 1
    assert started_egress == ["EG_TEST"]
    assert stopped_egress == ["EG_TEST"]
    assert disconnected == [True]
    assert room_disconnect_calls == [True]


def test_stream_once_sends_start_prompt_and_start(monkeypatch: pytest.MonkeyPatch) -> None:
    sent_commands: list[tuple[str, dict[str, object]]] = []
    disconnected: list[bool] = []
    started_egress: list[str] = []
    stopped_egress: list[str] = []
    room_disconnect_calls: list[bool] = []
    stop_event = asyncio.Event()

    _install_fake_reactor(
        monkeypatch,
        frames=[_FakeFrame(64, 64)],
        sent_commands=sent_commands,
        disconnected=disconnected,
    )
    _install_fake_livekit(
        monkeypatch,
        started_egress=started_egress,
        stopped_egress=stopped_egress,
        room_disconnect_calls=room_disconnect_calls,
    )

    async def fake_publish_video_frame(
        video_source: object,
        frame: object,
        width: int,
        height: int,
        *,
        timestamp_us: int,
    ) -> None:
        stop_event.set()

    async def fake_silent_audio(*, audio_source: object, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await asyncio.sleep(0.01)

    monkeypatch.setattr(app, "publish_video_frame", fake_publish_video_frame)
    monkeypatch.setattr(app, "run_silent_audio_publisher", fake_silent_audio)

    with pytest.raises(app.GracefulStop):
        asyncio.run(app.stream_once(make_config(start_prompt="A cinematic ocean"), stop_event))

    assert sent_commands[:2] == [
        ("schedule_prompt", {"new_prompt": "A cinematic ocean", "timestamp": 0}),
        ("start", {}),
    ]
