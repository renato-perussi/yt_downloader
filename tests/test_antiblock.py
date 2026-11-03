"""Anti-bloqueio leve: cache, retry 429, dedup download, rate-limit por IP."""

from __future__ import annotations


def _mkraw(fmt_list):
    return {
        "id": "abc123",
        "title": "T",
        "channel": "C",
        "duration": 10,
        "view_count": 5,
        "upload_date": "20240101",
        "thumbnail": "http://img",
        "formats": fmt_list,
    }


def test_info_cache_hit_avoids_second_extraction(monkeypatch):
    from app import yt

    yt.clear_info_cache()
    calls = []

    class FakeYDL:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            calls.append(url)
            return _mkraw([])

    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYDL)
    url = "https://www.youtube.com/watch?v=abc12345678"
    yt.get_info(url)
    yt.get_info(url)
    assert len(calls) == 1


def test_info_retries_on_429_then_succeeds(monkeypatch):
    import yt_dlp

    from app import yt

    yt.clear_info_cache()
    calls = []

    class FakeYDL:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            calls.append(url)
            if len(calls) == 1:
                raise yt_dlp.utils.DownloadError("HTTP Error 429: Too Many Requests")
            return _mkraw([])

    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYDL)
    info = yt.get_info("https://www.youtube.com/watch?v=abc12345678")
    assert info["id"] == "abc123"
    assert len(calls) == 2


def test_yt_dlp_opts_include_retries(monkeypatch):
    from app import yt

    captured = {}

    class FakeYDL:
        def __init__(self, opts):
            captured.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            return _mkraw([])

    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYDL)
    yt.clear_info_cache()
    yt.get_info("https://www.youtube.com/watch?v=abc12345678")
    assert captured.get("retries") == 1
    assert captured.get("fragment_retries") == 3


def test_rate_limit_blocks_after_limit():
    from fastapi import HTTPException

    from app.main import _check_rate_limit, clear_rate_limit_store

    clear_rate_limit_store()
    for _ in range(5):
        _check_rate_limit("1.2.3.4", "info", limit=5, window=60)
    try:
        _check_rate_limit("1.2.3.4", "info", limit=5, window=60)
    except HTTPException as e:
        assert e.status_code == 429
    else:
        raise AssertionError("deveria limitar")


def test_download_reuses_cached_info(monkeypatch):
    import subprocess

    from fastapi.testclient import TestClient

    from app import yt
    from app.main import app

    yt.clear_info_cache()
    calls = []

    class FakeYDL:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            calls.append(url)
            return _mkraw([])

    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYDL)
    monkeypatch.setattr(yt.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    class FakeStdout:
        def __init__(self, data=b"x" * 70000):
            self._data = data
            self._pos = 0

        def read(self, n):
            chunk = self._data[self._pos : self._pos + n]
            self._pos += len(chunk)
            return chunk

        def close(self):
            pass

    class FakePopen:
        def __init__(self, cmd, *a, **k):
            self.stdout = FakeStdout()
            self.returncode = 0
            self.pid = 1

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    c = TestClient(app)
    url = "https://www.youtube.com/watch?v=abc12345678"
    r1 = c.get("/api/info", params={"url": url})
    assert r1.status_code == 200
    r2 = c.get("/api/download", params={"url": url, "format_id": "22"})
    assert r2.status_code == 200
    assert len(calls) == 1


def test_validate_accepts_port_and_uppercase():
    from app import yt

    assert yt.validate_youtube_url("https://www.youtube.com:443/watch?v=abc12345678")
    assert yt.validate_youtube_url("https://WWW.YOUTUBE.COM/watch?v=abc12345678")


def test_cache_returns_copy_not_reference(monkeypatch):
    from app import yt

    class FakeYDL:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            return _mkraw([])

    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYDL)
    yt.clear_info_cache()
    url = "https://www.youtube.com/watch?v=abc12345678"
    a = yt.get_info(url)
    a["title"] = "MUTADO"
    assert yt.get_info(url)["title"] != "MUTADO"


def test_cache_bounded_maxsize(monkeypatch):
    from app import yt

    class FakeYDL:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            vid = url.rsplit("v=", 1)[-1]
            raw = _mkraw([])
            raw["id"] = vid
            return raw

    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYDL)
    monkeypatch.setattr(yt, "_INFO_MAXSIZE", 5)
    yt.clear_info_cache()
    for i in range(10):
        yt.get_info(f"https://www.youtube.com/watch?v=vid{i:08d}")
    assert len(yt._INFO_CACHE) <= 5


def test_security_headers_present():
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    r = c.get("/")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert "Content-Security-Policy" in r.headers


def test_info_generic_error_on_network(monkeypatch):
    import yt_dlp
    from fastapi.testclient import TestClient

    from app.main import app

    class FakeYDL:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            raise yt_dlp.utils.DownloadError("unable to download webpage: http://secret-internal")

    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYDL)
    c = TestClient(app)
    r = c.get("/api/info", params={"url": "https://www.youtube.com/watch?v=abc12345678"})
    assert r.status_code == 502
    assert "secret-internal" not in r.json()["detail"]
