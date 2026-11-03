from __future__ import annotations

import pytest
import yt_dlp

from app import yt


def test_validate_accepts_watch_and_short():
    assert yt.validate_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ").startswith(
        "https://"
    )
    assert yt.validate_youtube_url("https://youtu.be/dQw4w9WgXcQ").startswith("https://")
    assert yt.validate_youtube_url("https://www.youtube.com/shorts/dQw4w9WgXcQ").startswith(
        "https://"
    )


def test_validate_rejects_non_youtube():
    with pytest.raises(ValueError):
        yt.validate_youtube_url("https://vimeo.com/123")


def test_sanitize_filename():
    assert yt.sanitize_filename('Meu Vídeo: teste/01?*<>|"', max_len=80) == "Meu Vdeo teste01"
    assert yt.sanitize_filename("   ", max_len=80) == "video"


def test_format_duration():
    assert yt.format_duration(65) == "1:05"
    assert yt.format_duration(3665) == "1:01:05"
    assert yt.format_duration(None) == "--:--"


def test_build_download_command_video(monkeypatch):
    monkeypatch.setattr(yt.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    url = "https://www.youtube.com/watch?v=abc"
    cmd = yt.build_download_command(url, "22", False)
    assert "--no-playlist" in cmd
    assert "--ignore-config" in cmd
    assert "yt_dlp" in cmd
    # posições exatas: o valor de -f é o seletor com merge e o de -o é stdout ("-")
    assert cmd[cmd.index("-f") + 1] == "22+bestaudio/best"
    assert cmd[cmd.index("-o") + 1] == "-"
    assert cmd[-1] == url


def test_build_download_command_video_calls_ensure_ffmpeg(monkeypatch):
    """Ruling: ensure_ffmpeg é chamado para vídeo também (merge precisa)."""
    called = []
    monkeypatch.setattr(yt.shutil, "which", lambda name: called.append(1) or "/usr/bin/ffmpeg")
    yt.build_download_command("https://www.youtube.com/watch?v=abc", "22", False)
    assert called, "ensure_ffmpeg must be called for video (merge requires ffmpeg)"


def test_build_download_command_video_ffmpeg_missing(monkeypatch):
    """Sem ffmpeg, build_download_command de vídeo levanta RuntimeError (fail-fast)."""
    monkeypatch.setattr(yt.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="ffmpeg"):
        yt.build_download_command("https://www.youtube.com/watch?v=abc", "22", False)


def test_build_download_command_rejects_injection():
    with pytest.raises(ValueError):
        yt.build_download_command("https://www.youtube.com/watch?v=abc", "22; rm -rf /", False)


def test_get_info_normalizes_mocked(monkeypatch):
    fake_raw = {
        "id": "abc123",
        "title": "Titulo Teste",
        "channel": "Canal X",
        "duration": 65,
        "view_count": 1000,
        "upload_date": "20240101",
        "thumbnail": "http://img",
        "formats": [
            {
                "format_id": "22",
                "ext": "mp4",
                "resolution": "720p",
                "filesize": 60000000,
                "vcodec": "avc1",
                "acodec": "mp4a",
            },
            {
                "format_id": "140",
                "ext": "m4a",
                "abr": 128,
                "filesize": 5000000,
                "vcodec": "none",
                "acodec": "mp4a",
            },
        ],
    }

    class FakeYDL:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            return fake_raw

    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYDL)
    info = yt.get_info("https://www.youtube.com/watch?v=abc123")
    assert info["id"] == "abc123"
    assert info["duration_str"] == "1:05"
    assert len(info["formats"]) == 2
    # kind exato: format 22 tem vcodec != none (video), format 140 tem vcodec none (audio)
    assert info["formats"][0]["kind"] == "video"
    assert info["formats"][1]["kind"] == "audio"
    assert info["formats"][0]["format_id"] == "22"
    assert info["formats"][0]["ext"] == "mp4"
    assert info["formats"][0]["resolution"] == "720p"
    assert info["formats"][0]["filesize"] == 60000000
    assert info["formats"][1]["kind"] == "audio"
    assert info["formats"][1]["abr"] == 128
    assert info["title"] == "Titulo Teste"
    assert info["channel"] == "Canal X"
    assert info["views"] == 1000
    assert info["upload_date"] == "20240101"
    assert info["thumbnail"] == "http://img"
    assert info["duration_sec"] == 65


# --- regressões: contrato de validação e mapeamento de erros ---


def _fake_ydl_raising(exc: Exception) -> type:
    """Cria uma classe YoutubeDL falsa que sempre levanta ``exc``."""

    class FakeYDL:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            raise exc

    return FakeYDL


def test_validate_rejects_url_without_video_id():
    with pytest.raises(ValueError):
        yt.validate_youtube_url("https://www.youtube.com/watch?v=")
    with pytest.raises(ValueError):
        yt.validate_youtube_url("https://www.youtube.com/shorts/")


def test_validate_rejects_non_http_scheme():
    with pytest.raises(ValueError):
        yt.validate_youtube_url("file:///etc/passwd")


def test_validate_rejects_host_suffix_spoofing():
    with pytest.raises(ValueError):
        yt.validate_youtube_url("https://youtube.com.evil.example/watch?v=dQw4w9WgXcQ")


def test_get_info_rejects_non_youtube_before_touching_network():
    with pytest.raises(ValueError):
        yt.get_info("https://vimeo.com/123")


def test_get_info_maps_private_to_lookup_error(monkeypatch):
    monkeypatch.setattr(
        "yt_dlp.YoutubeDL",
        _fake_ydl_raising(yt_dlp.utils.DownloadError("ERROR: [youtube] x: Private video.")),
    )
    with pytest.raises(yt.VideoNotFoundError):
        yt.get_info("https://www.youtube.com/watch?v=abc123")


def test_get_info_maps_unavailable_to_lookup_error(monkeypatch):
    monkeypatch.setattr(
        "yt_dlp.YoutubeDL",
        _fake_ydl_raising(
            yt_dlp.utils.DownloadError("ERROR: [youtube] x: This video is unavailable")
        ),
    )
    with pytest.raises(yt.VideoNotFoundError):
        yt.get_info("https://www.youtube.com/watch?v=abc123")


def test_get_info_maps_age_restricted_to_permission_error(monkeypatch):
    monkeypatch.setattr(
        "yt_dlp.YoutubeDL",
        _fake_ydl_raising(
            yt_dlp.utils.DownloadError("ERROR: [youtube] x: Sign in to confirm your age.")
        ),
    )
    with pytest.raises(PermissionError):
        yt.get_info("https://www.youtube.com/watch?v=abc123")


def test_get_info_maps_extractor_error_without_escaping(monkeypatch):
    """ExtractorError NÃO é subclasse de DownloadError: precisa ser mapeado também."""
    assert not issubclass(yt_dlp.utils.ExtractorError, yt_dlp.utils.DownloadError)
    monkeypatch.setattr(
        "yt_dlp.YoutubeDL",
        _fake_ydl_raising(yt_dlp.utils.ExtractorError("No video formats found!")),
    )
    with pytest.raises(yt.VideoNotFoundError):
        yt.get_info("https://www.youtube.com/watch?v=abc123")


def test_get_info_maps_unknown_error_to_connection_error(monkeypatch):
    monkeypatch.setattr(
        "yt_dlp.YoutubeDL",
        _fake_ydl_raising(yt_dlp.utils.DownloadError("unable to download webpage: timed out")),
    )
    with pytest.raises(ConnectionError):
        yt.get_info("https://www.youtube.com/watch?v=abc123")


def test_build_download_command_always_disables_playlist(monkeypatch):
    monkeypatch.setattr(yt.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    cmd = yt.build_download_command("https://www.youtube.com/watch?v=abc", "22", False)
    assert "--no-playlist" in cmd


def test_build_download_command_audio_only(monkeypatch):
    monkeypatch.setattr(yt.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    cmd = yt.build_download_command("https://www.youtube.com/watch?v=abc", "140", True)
    # Audio path: yt-dlp apenas baixa o stream bruto. A conversão MP3 é feita
    # pelo ffmpeg (build_ffmpeg_command). yt-dlp com -o - não executa
    # --extract-audio, por isso removemos as flags do comando.
    assert "--extract-audio" not in cmd
    assert "--audio-format" not in cmd
    assert cmd[cmd.index("-f") + 1] == "140"
    assert cmd[cmd.index("-o") + 1] == "-"
    assert cmd[-1] == "https://www.youtube.com/watch?v=abc"


def test_build_ffmpeg_command_audio():
    """Áudio: transcode M4A→MP3 192k via pipe."""
    cmd = yt.build_ffmpeg_command(audio_only=True)
    assert cmd[0] == "ffmpeg"
    assert "-i" in cmd
    assert cmd[cmd.index("-i") + 1] == "pipe:0"
    assert "-f" in cmd
    assert "mp3" in cmd
    assert "pipe:1" in cmd
    assert "-vn" in cmd


def test_build_ffmpeg_command_video():
    """Vídeo: remux MKV→MP4 fragmentado sem re-encoding (-c copy).

    Fragmentado (-movflags frag_keyframe+empty_moov) porque pipe:1 é
    non-seekable — MP4 tradicional falha com "muxer does not support
    non seekable output".
    """
    cmd = yt.build_ffmpeg_command(audio_only=False)
    assert cmd[0] == "ffmpeg"
    assert cmd[cmd.index("-i") + 1] == "pipe:0"
    assert "-c" in cmd
    assert cmd[cmd.index("-c") + 1] == "copy"
    assert "-f" in cmd
    assert "mp4" in cmd
    assert "-movflags" in cmd
    assert "frag_keyframe+empty_moov" in cmd
    assert "pipe:1" in cmd


def test_build_download_command_rejects_trailing_newline_in_format_id():
    with pytest.raises(ValueError):
        yt.build_download_command("https://www.youtube.com/watch?v=abc", "22\n", False)


def test_ensure_ffmpeg_raises_when_missing(monkeypatch):
    monkeypatch.setattr(yt.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError):
        yt.ensure_ffmpeg()


# --- round 1: opções de segurança do yt-dlp e URL validada/normalizada ---


def _fake_ydl_capturing(captured: dict, payload: dict) -> type:
    """Fake YoutubeDL que registra as opts recebidas e a URL extraída."""

    class FakeYDL:
        def __init__(self, opts):
            captured["opts"] = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            captured["url"] = url
            return payload

    return FakeYDL


def test_get_info_opts_enforce_noplaylist_and_socket_timeout(monkeypatch):
    """noplaylist=True e socket_timeout são invariantes de segurança/robustez."""
    captured: dict = {}
    monkeypatch.setattr(
        "yt_dlp.YoutubeDL",
        _fake_ydl_capturing(
            captured, {"id": "abc123", "title": "T", "duration": 10, "formats": []}
        ),
    )
    yt.get_info("https://www.youtube.com/watch?v=abc123")
    opts = captured["opts"]
    assert opts["noplaylist"] is True
    assert opts["skip_download"] is True
    assert opts["quiet"] is True
    assert opts["no_warnings"] is True
    assert isinstance(opts["socket_timeout"], (int, float))
    assert opts["socket_timeout"] > 0


def test_get_info_passes_validated_and_stripped_url(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        "yt_dlp.YoutubeDL",
        _fake_ydl_capturing(
            captured, {"id": "abc123", "title": "T", "duration": 10, "formats": []}
        ),
    )
    yt.get_info("  https://www.youtube.com/watch?v=abc123  ")
    assert captured["url"] == "https://www.youtube.com/watch?v=abc123"


def test_build_download_command_uses_validated_and_stripped_url(monkeypatch):
    monkeypatch.setattr(yt.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    cmd = yt.build_download_command("  https://www.youtube.com/watch?v=abc  ", "22", False)
    assert cmd[-1] == "https://www.youtube.com/watch?v=abc"


def test_get_info_rejects_empty_video_id(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        "yt_dlp.YoutubeDL",
        _fake_ydl_capturing(captured, {"id": "", "title": "T", "duration": 10, "formats": []}),
    )
    with pytest.raises(yt.VideoNotFoundError):
        yt.get_info("https://www.youtube.com/watch?v=abc123")


def test_video_not_found_is_lookup_error():
    """VideoNotFoundError é subclasse de LookupError (compat retroativa)."""
    assert issubclass(yt.VideoNotFoundError, LookupError)


def _mk(fid, ext, res, abr, size, kind):
    vc = "none" if kind == "audio" else "avc1"
    ac = "none" if kind == "video" else "opus"
    return {
        "format_id": fid,
        "ext": ext,
        "resolution": res,
        "abr": abr,
        "filesize": size,
        "vcodec": vc,
        "acodec": ac,
        "kind": kind,
    }


def test_curate_groups_video_by_height_and_hides_unknown_size():
    fmts = [
        _mk("399", "mp4", "1920x1080", 0, 21803317, "video"),
        _mk("137", "mp4", "1920x1080", 0, 10794746, "video"),
        _mk("270", "mp4", "1920x1080", 0, None, "video"),
        _mk("136", "mp4", "1280x720", 0, 3162514, "video"),
    ]
    cur = yt._curate_formats(fmts)
    vids = [f for f in cur if f["kind"] == "video"]
    assert len(vids) == 2
    assert vids[0]["format_id"] == "399"
    assert vids[1]["format_id"] == "136"


def test_curate_groups_audio_in_tiers_and_hides_unknown():
    fmts = [
        _mk("251", "webm", "audio only", 141.675, 7192533, "audio"),
        _mk("140", "m4a", "audio only", 129.486, 6574469, "audio"),
        _mk("250", "webm", "audio only", 71.831, 3646726, "audio"),
        _mk("249", "webm", "audio only", 54.01, 2742000, "audio"),
        _mk("233", "mp4", "audio only", None, None, "audio"),
    ]
    cur = yt._curate_formats(fmts)
    auds = [f for f in cur if f["kind"] == "audio"]
    assert [f["format_id"] for f in auds] == ["251", "250", "249"]
