import subprocess

import pytest
from fastapi.testclient import TestClient

from app import yt
from app.main import app

client = TestClient(app)


def _pipeline_popen_factory(data: bytes, returncode: int = 0):
    """Factory: retorna uma classe que mocka o pipeline yt-dlp | ffmpeg.

    O endpoint cria DOIS ``Popen``:
    1. yt-dlp (argv contém ``yt_dlp``): stdout=PIPE, produz ``data``.
    2. ffmpeg (argv contém ``ffmpeg``): stdin=yt-dlp.stdout, stdout=PIPE.

    Ambas as instâncias compartilham o mesmo ``data`` no stdout — o teste
    verifica o contrato do endpoint, não o pipe real entre processos.
    """

    class PipelinePopen:
        def __init__(self, cmd, *args, **kwargs):
            self._data = data
            self.returncode = returncode
            self.pid = 12345
            self._done = False

            class _Stdout:
                def __init__(self_outer):
                    self_outer._pos = 0
                    self_outer._data = data

                def read(self_outer, size):
                    end = min(self_outer._pos + size, len(self_outer._data))
                    chunk = self_outer._data[self_outer._pos : end]
                    self_outer._pos = end
                    return chunk

                def close(self_outer):
                    pass

            self.stdout = _Stdout()

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self._done = True
            return self.returncode

        def kill(self):
            self._done = True
            self.returncode = -9
            return None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return PipelinePopen


def test_root_serves_html():
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_info_ok_mocked(monkeypatch):
    """Mocka yt.get_info (não asyncio.to_thread) para exercitar o caminho real."""

    def fake_get_info(url):
        return {
            "id": "x",
            "title": "T",
            "channel": "C",
            "duration_sec": 10,
            "duration_str": "0:10",
            "views": 5,
            "upload_date": "20240101",
            "thumbnail": "http://t",
            "formats": [],
        }

    monkeypatch.setattr(yt, "get_info", fake_get_info)
    r = client.get("/api/info", params={"url": "https://www.youtube.com/watch?v=abc12345678"})
    assert r.status_code == 200
    assert r.json()["id"] == "x"


def test_info_rejects_bad_url():
    r = client.get("/api/info", params={"url": "https://vimeo.com/1"})
    assert r.status_code == 400


@pytest.mark.parametrize(
    ("exception", "expected_status"),
    [
        (yt.VideoNotFoundError("Vídeo privado"), 404),
        (yt.VideoNotFoundError("Vídeo indisponível"), 404),
        (PermissionError("Restrição de idade"), 451),
        (ConnectionError("Falha de rede"), 502),
        (RuntimeError("ffmpeg não encontrado"), 500),
        (KeyError("bug interno"), 500),  # LookupError genérico vira 500
    ],
    ids=[
        "video-not-found-private",
        "video-not-found-unavailable",
        "age-restricted",
        "connection-error",
        "runtime-error",
        "keyerror-bug",
    ],
)
def test_info_error_mapping(monkeypatch, exception, expected_status):
    def fake_get_info(url):
        raise exception

    monkeypatch.setattr(yt, "get_info", fake_get_info)
    r = client.get("/api/info", params={"url": "https://www.youtube.com/watch?v=abc12345678"})
    assert r.status_code == expected_status
    assert "detail" in r.json()


def test_info_passes_validated_url_to_get_info(monkeypatch):
    """Garante que get_info recebe a URL validada (não a raw do query param)."""
    captured = {}

    def fake_get_info(url):
        captured["url"] = url
        return {
            "id": "x",
            "title": "T",
            "channel": "C",
            "duration_sec": 10,
            "duration_str": "0:10",
            "views": 5,
            "upload_date": "20240101",
            "thumbnail": "http://t",
            "formats": [],
        }

    monkeypatch.setattr(yt, "get_info", fake_get_info)
    r = client.get(
        "/api/info",
        params={"url": "  https://www.youtube.com/watch?v=abc12345678  "},
    )
    assert r.status_code == 200
    # get_info recebe a URL (pode ter whitespace; yt.get_info faz strip interno)
    assert captured["url"] == "  https://www.youtube.com/watch?v=abc12345678  "


def test_info_short_url_returns_422():
    """Query(min_length=8) retorna 422 para URLs curtas demais."""
    r = client.get("/api/info", params={"url": "https"})
    assert r.status_code == 422


# --- download streaming ---


def test_download_rejects_bad_format_id():
    """format_id com caractere inválido → 400 (ValueError do build_download_command)."""
    r = client.get(
        "/api/download",
        params={
            "url": "https://www.youtube.com/watch?v=abc12345678",
            "format_id": "bad id!",
        },
    )
    assert r.status_code == 400
    assert "detail" in r.json()


def test_download_rejects_non_youtube_url():
    r = client.get(
        "/api/download",
        params={
            "url": "https://vimeo.com/12345678",
            "format_id": "22",
        },
    )
    assert r.status_code == 400


def test_download_requires_format_id():
    """format_id é obrigatório (Query required)."""
    r = client.get(
        "/api/download",
        params={"url": "https://www.youtube.com/watch?v=abc12345678"},
    )
    assert r.status_code == 422


def test_download_streams_video_mocked(monkeypatch):
    """Mocka Popen + get_info para validar contrato de streaming (bytes + headers)."""
    monkeypatch.setattr(
        subprocess, "Popen", _pipeline_popen_factory(b"chunk-0chunk-1chunk-2", returncode=0)
    )
    monkeypatch.setattr(yt.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        yt,
        "get_info",
        lambda url: {"id": "abc12345678", "title": "Meu Vídeo: teste", "formats": []},
    )
    r = client.get(
        "/api/download",
        params={
            "url": "https://www.youtube.com/watch?v=abc12345678",
            "format_id": "22",
        },
    )
    assert r.status_code == 200
    assert r.content == b"chunk-0chunk-1chunk-2"
    assert r.headers["content-type"] == "video/mp4"
    cd = r.headers["content-disposition"]
    assert "attachment" in cd
    assert "Meu Vdeo teste.mp4" in cd


def test_download_streams_audio_mocked(monkeypatch):
    """audio_only=True → media_type audio/mpeg, extensão .mp3."""
    monkeypatch.setattr(subprocess, "Popen", _pipeline_popen_factory(b"audio-bytes"))
    monkeypatch.setattr(yt.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        yt,
        "get_info",
        lambda url: {"id": "abc12345678", "title": "Musica Top", "formats": []},
    )
    r = client.get(
        "/api/download",
        params={
            "url": "https://www.youtube.com/watch?v=abc12345678",
            "format_id": "140",
            "audio_only": "true",
        },
    )
    assert r.status_code == 200
    assert r.content == b"audio-bytes"
    assert r.headers["content-type"] == "audio/mpeg"
    assert "Musica Top.mp3" in r.headers["content-disposition"]


def test_download_ffmpeg_missing_returns_500(monkeypatch):
    """RuntimeError de ensure_ffmpeg → 500."""
    monkeypatch.setattr(yt.shutil, "which", lambda name: None)
    r = client.get(
        "/api/download",
        params={
            "url": "https://www.youtube.com/watch?v=abc12345678",
            "format_id": "22",
        },
    )
    assert r.status_code == 500
    assert "ffmpeg" in r.json()["detail"].lower()


def test_download_info_failure_uses_default_filename(monkeypatch):
    """Se get_info falha, filename default 'video.mp4' (download não é bloqueado)."""
    monkeypatch.setattr(subprocess, "Popen", _pipeline_popen_factory(b"ok"))
    monkeypatch.setattr(yt.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(yt, "get_info", lambda url: (_ for _ in ()).throw(ConnectionError("fail")))
    r = client.get(
        "/api/download",
        params={
            "url": "https://www.youtube.com/watch?v=abc12345678",
            "format_id": "22",
        },
    )
    assert r.status_code == 200
    assert "video.mp4" in r.headers["content-disposition"]


# --- fix round 1: fail-fast antes dos headers + cleanup robusto ---


def test_download_popen_oserror_returns_500_before_headers(monkeypatch):
    """Popen raising OSError (ex.: binário ausente) → 500 ANTES dos headers.

    Sem esse fix, o endpoint retornaria 200 e truncaria no meio — cliente não
    distinguiria de download incompleto.
    """

    def raising_popen(*args, **kwargs):
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(subprocess, "Popen", raising_popen)
    monkeypatch.setattr(yt.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(yt, "get_info", lambda url: {"title": "T", "formats": []})
    r = client.get(
        "/api/download",
        params={
            "url": "https://www.youtube.com/watch?v=abc12345678",
            "format_id": "22",
        },
    )
    assert r.status_code == 500
    assert "detail" in r.json()


def test_download_subprocess_dies_before_first_chunk_returns_502(monkeypatch):
    """Pipeline morre na largada com stdout vazio → 502 ANTES dos headers.

    yt-dlp pode sair com rc!=0 imediatamente (formato indisponível, DRM, etc.).
    O endpoint faz ``await to_thread(proc.wait, 5)`` para obter o rc real.
    rc != 0 ⇒ 502 antes dos headers.
    """
    monkeypatch.setattr(subprocess, "Popen", _pipeline_popen_factory(b"", returncode=1))
    monkeypatch.setattr(yt.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(yt, "get_info", lambda url: {"title": "T", "formats": []})
    r = client.get(
        "/api/download",
        params={
            "url": "https://www.youtube.com/watch?v=abc12345678",
            "format_id": "22",
        },
    )
    assert r.status_code == 502
    assert "detail" in r.json()


def test_download_empty_stdout_wait_nonzero_returns_502(monkeypatch):
    """Regressão F1: pipe fechado + wait()=1 ⇒ 502 (não 200 vazio).

    Cenário: ffmpeg fecha stdout antes de ``waitpid()`` atualizar.
    Agora usa ``wait()`` diretamente para obter o rc real.
    """
    import subprocess as sp

    class RaceConditionFake:
        """Fake onde ``wait()`` retorna 1 (ffmpeg falhou)."""

        def __init__(self, *args, **kwargs):
            self.returncode = 1
            self.pid = 55555

            class _Stdout:
                def read(self_outer, size):
                    return b""

                def close(self_outer):
                    pass

            self.stdout = _Stdout()

        def poll(self):
            return None

        def wait(self, timeout=None):
            self.returncode = 1
            return 1

        def kill(self):
            self.returncode = -9
            return None

    monkeypatch.setattr(sp, "Popen", RaceConditionFake)
    monkeypatch.setattr(yt.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(yt, "get_info", lambda url: {"title": "T", "formats": []})
    r = client.get(
        "/api/download",
        params={
            "url": "https://www.youtube.com/watch?v=abc12345678",
            "format_id": "22",
        },
    )
    assert r.status_code == 502
    assert "detail" in r.json()


def test_download_ffmpeg_exits_with_partial_header_returns_502(monkeypatch):
    """Regressão: ffmpeg escreve header parcial (ftyp...) e sai rc=1 ⇒ 502, não 200.

    Cenário: ffmpeg encontra codec incompatível, escreve alguns bytes de header
    (ex: "ftyp" MP4 box) e sai com rc!=0. Sem fail-fast, first_chunk não-vazio
    faria o endpoint retornar 200 com arquivo corrompido. Com fail-fast, para
    output <64KB, o endpoint faz wait(5) para obter o rc real e retorna 502.
    """

    class PartialHeaderFake:
        """Fake onde ffmpeg escreve ~20 bytes (header) e sai rc=1."""

        def __init__(self, cmd, *args, **kwargs):
            self.returncode = 1  # rc!=0
            self.pid = 77777
            # Simula header MP4 parcial: "ftyp" box + alguns bytes
            self._data = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2"  # 20 bytes

            class _Stdout:
                def __init__(self_outer):
                    self_outer._pos = 0
                    self_outer._data = self._data

                def read(self_outer, size):
                    end = min(self_outer._pos + size, len(self_outer._data))
                    chunk = self_outer._data[self_outer._pos : end]
                    self_outer._pos = end
                    return chunk

                def close(self_outer):
                    pass

            self.stdout = _Stdout()

        def poll(self):
            # ffmpeg já terminou (rc=1)
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            return None

    monkeypatch.setattr(subprocess, "Popen", PartialHeaderFake)
    monkeypatch.setattr(yt.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(yt, "get_info", lambda url: {"title": "T", "formats": []})
    r = client.get(
        "/api/download",
        params={
            "url": "https://www.youtube.com/watch?v=abc12345678",
            "format_id": "22",
        },
    )
    assert r.status_code == 502, "Expected 502 for partial header with rc!=0"
    assert "detail" in r.json()


def test_download_ffmpeg_poll_none_wait_nonzero_returns_502(monkeypatch):
    """Race guard: poll()=None (stdout fechado, proc não reaped) + wait()=1 ⇒ 502.

    Cenário real: pipe fechado pelo ffmpeg mas ``waitpid()`` não atualizou ainda.
    Sem race guard, poll() retornaria None ⇒ endpoint retornaria 200 com arquivo
    corrompido. Agora, para output <64KB, usa wait(5) para obter o rc real.
    """

    class RaceConditionFake:
        """Fake onde poll() retorna None mas wait() retorna 1."""

        def __init__(self, cmd, *args, **kwargs):
            self.returncode = None  # Ainda não reaped
            self.pid = 88888
            self._data = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00"  # 16 bytes

            class _Stdout:
                def __init__(self_outer):
                    self_outer._pos = 0
                    self_outer._data = self._data

                def read(self_outer, size):
                    end = min(self_outer._pos + size, len(self_outer._data))
                    chunk = self_outer._data[self_outer._pos : end]
                    self_outer._pos = end
                    return chunk

                def close(self_outer):
                    pass

            self.stdout = _Stdout()

        def poll(self):
            # Race: pipe fechado mas processo ainda não reaped.
            return None

        def wait(self, timeout=None):
            # wait() resolve o race e retorna o rc real.
            self.returncode = 1
            return 1

        def kill(self):
            self.returncode = -9
            return None

    monkeypatch.setattr(subprocess, "Popen", RaceConditionFake)
    monkeypatch.setattr(yt.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(yt, "get_info", lambda url: {"title": "T", "formats": []})
    r = client.get(
        "/api/download",
        params={
            "url": "https://www.youtube.com/watch?v=abc12345678",
            "format_id": "22",
        },
    )
    assert r.status_code == 502, "Expected 502 when poll()=None but wait()=1"
    assert "detail" in r.json()


def test_download_subprocess_timeout_triggers_kill_and_logs_warning(monkeypatch, caplog):
    """wait(timeout=10) estoura → kill() + re-wait + log warning. Evita órfão."""
    import subprocess as sp

    captured_instances: list = []
    # Dados >= 64KB para NÃO entrar no fail-fast do header parcial
    # (que usa wait(5) para output < 64KB). Este teste exercita o timeout
    # no finally do generator (wait(10) após streaming completo).
    _payload = b"x" * (64 * 1024 + 10)  # 64KB + 10 bytes

    class TimeoutFakePopen:
        def __init__(self, *args, **kwargs):
            self._data = _payload
            self._pos = 0
            self.returncode = None
            self.pid = 99999
            self.killed = False
            self._wait_call_count = 0
            captured_instances.append(self)

            class _Stdout:
                def __init__(self_outer):
                    self_outer._pos = 0
                    self_outer._data = _payload

                def read(self_outer, size):
                    end = min(self_outer._pos + size, len(self_outer._data))
                    chunk = self_outer._data[self_outer._pos : end]
                    self_outer._pos = end
                    return chunk

                def close(self_outer):
                    pass

            self.stdout = _Stdout()

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self._wait_call_count += 1
            if self._wait_call_count == 1:
                # 1ª chamada (no finally do generator): simula subprocess travado.
                self.returncode = -9
                raise sp.TimeoutExpired(cmd="fake", timeout=timeout)
            # Chamadas subsequentes (após kill via _safe_terminate).
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9
            return None

    monkeypatch.setattr(sp, "Popen", TimeoutFakePopen)
    monkeypatch.setattr(yt.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(yt, "get_info", lambda url: {"title": "T", "formats": []})
    with caplog.at_level("WARNING", logger="yt_downloader"):
        r = client.get(
            "/api/download",
            params={
                "url": "https://www.youtube.com/watch?v=abc12345678",
                "format_id": "22",
            },
        )
    assert r.status_code == 200
    assert r.content == _payload
    # F3: assert kill() foi efetivamente chamado na instância capturada
    assert captured_instances, "TimeoutFakePopen deve ter sido instanciado"
    fake = captured_instances[0]
    assert fake.killed is True, "kill() deve ser chamado após wait() timeout"
    # Log warning com pid deve ter sido emitido
    msgs = [rec.getMessage() for rec in caplog.records]
    assert any("killing pid=99999" in m for m in msgs), msgs


# --- pipeline fix: containers corretos (MP4/MKV e M4A/MP3) ---


def test_download_pipeline_creates_two_popen_calls(monkeypatch):
    """Endpoint cria yt-dlp + ffmpeg (2 Popen calls, não 1)."""
    popen_calls: list[list] = []

    class TrackingPopen:
        def __init__(self, cmd, *args, **kwargs):
            popen_calls.append(list(cmd))
            self.returncode = 0
            self.pid = 42

            class _Stdout:
                def __init__(self_outer):
                    self_outer._data = b"ok"
                    self_outer._pos = 0

                def read(self_outer, size):
                    end = min(self_outer._pos + size, len(self_outer._data))
                    chunk = self_outer._data[self_outer._pos : end]
                    self_outer._pos = end
                    return chunk

                def close(self_outer):
                    pass

            self.stdout = _Stdout()

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return 0

        def kill(self):
            return None

    monkeypatch.setattr(subprocess, "Popen", TrackingPopen)
    monkeypatch.setattr(yt.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(yt, "get_info", lambda url: {"title": "T", "formats": []})
    r = client.get(
        "/api/download",
        params={
            "url": "https://www.youtube.com/watch?v=abc12345678",
            "format_id": "22",
        },
    )
    assert r.status_code == 200
    assert len(popen_calls) == 2, f"Expected 2 Popen calls, got {len(popen_calls)}"
    # 1ª chamada: yt-dlp
    yt_cmd = popen_calls[0]
    assert "yt_dlp" in " ".join(yt_cmd)
    # 2ª chamada: ffmpeg com pipe:0 input
    ff_cmd = popen_calls[1]
    assert ff_cmd[0] == "ffmpeg"
    assert "pipe:0" in ff_cmd
    assert "pipe:1" in ff_cmd


def test_download_pipeline_audio_uses_ffmpeg_mp3(monkeypatch):
    """audio_only=True → ffmpeg transcode para MP3."""
    popen_calls: list[list] = []

    class TrackingPopen:
        def __init__(self, cmd, *args, **kwargs):
            popen_calls.append(list(cmd))
            self.returncode = 0
            self.pid = 42

            class _Stdout:
                def __init__(self_outer):
                    self_outer._data = b"mp3data"
                    self_outer._pos = 0

                def read(self_outer, size):
                    end = min(self_outer._pos + size, len(self_outer._data))
                    chunk = self_outer._data[self_outer._pos : end]
                    self_outer._pos = end
                    return chunk

                def close(self_outer):
                    pass

            self.stdout = _Stdout()

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return 0

        def kill(self):
            return None

    monkeypatch.setattr(subprocess, "Popen", TrackingPopen)
    monkeypatch.setattr(yt.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(yt, "get_info", lambda url: {"title": "T", "formats": []})
    r = client.get(
        "/api/download",
        params={
            "url": "https://www.youtube.com/watch?v=abc12345678",
            "format_id": "140",
            "audio_only": "true",
        },
    )
    assert r.status_code == 200
    ff_cmd = popen_calls[1]
    assert "mp3" in ff_cmd
    assert "-vn" in ff_cmd


def test_health_returns_ok():
    """Sonda de monitoramento (uptime/Docker) precisa de 200, não 404."""
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
