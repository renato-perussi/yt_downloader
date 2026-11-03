from __future__ import annotations

import copy
import logging
import random
import re
import shutil
import sys
import time
from urllib.parse import parse_qs, urlparse

import yt_dlp
from yt_dlp.utils import YoutubeDLError

log = logging.getLogger("yt_downloader.yt")

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
    "www.youtu.be",
}


class VideoNotFoundError(LookupError):
    """Domínio: vídeo não existe, privado ou indisponível.

    Subclasse de ``LookupError`` para compatibilidade retroativa com callers
    existentes (``pytest.raises(LookupError)`` continua válido). Em
    ``main.py`` capturamos esta subclasse ANTES da base ``LookupError`` para
    que bugs internos (``KeyError``/``IndexError`` — também ``LookupError``)
    NÃO retornem 404 indevido e caiam no fallback genérico 500.
    """


_SHORT_HOSTS = ("youtu.be", "www.youtu.be")

_SAFE_NAME = re.compile(r"[^A-Za-z0-9-_ ]+")
_FORMAT_ID = re.compile(r"\A[A-Za-z0-9_-]+\Z")

# Timeout de socket (s) para o yt-dlp: impede que uma extração travada ocupe
# indefinidamente uma thread do pool do FastAPI (Task 3 usa asyncio.to_thread).
_SOCKET_TIMEOUT = 20

# Flag explícita de testes (L4): conftest.py seta ``TESTING = True`` para
# pular sleeps de backoff. Não usar sniff de ``PYTEST_CURRENT_TEST`` no hot path.
TESTING = False

# Cache TTL em memória p/ get_info: evita re-extrair o mesmo vídeo em
# Buscar → Download (antes eram 2 hits no YouTube por uso).
# M1: bound por maxsize + purga de expirados (evita crescimento monotônico).
_INFO_TTL = 900
_INFO_MAXSIZE = 500
_INFO_CACHE: dict[str, tuple[float, dict]] = {}


def clear_info_cache() -> None:
    _INFO_CACHE.clear()


def _cache_get(url: str) -> dict | None:
    hit = _INFO_CACHE.get(url)
    if not hit:
        return None
    if hit[0] <= time.monotonic():
        _INFO_CACHE.pop(url, None)
        return None
    return copy.deepcopy(hit[1])


def _cache_set(url: str, info: dict) -> None:
    now = time.monotonic()
    # Purga oportunista de expirados quando cheio.
    if len(_INFO_CACHE) >= _INFO_MAXSIZE:
        expired = [k for k, (exp, _) in _INFO_CACHE.items() if exp <= now]
        for k in expired:
            _INFO_CACHE.pop(k, None)
    while len(_INFO_CACHE) >= _INFO_MAXSIZE:
        _INFO_CACHE.pop(next(iter(_INFO_CACHE)))
    _INFO_CACHE[url] = (now + _INFO_TTL, copy.deepcopy(info))


def _height_from_resolution(res: str | None) -> int:
    """Altura em px a partir de ``1920x1080``, ``1280×720`` ou ``720p``."""
    if not res:
        return -1
    m = re.search(r"(\d+)\s*[x×]\s*(\d+)", str(res))
    if m:
        return int(m.group(2))
    m = re.search(r"(\d+)\s*p", str(res), re.IGNORECASE)
    if m:
        return int(m.group(1))
    return -1


def _audio_tier(abr: float | None) -> str:
    """Faixa de qualidade de áudio: Alta (>=120kbps), Média (60-119), Baixa (<60)."""
    abr = abr or 0
    if abr >= 120:
        return "alta"
    if abr >= 60:
        return "media"
    return "baixa"


def _curate_formats(formats: list[dict]) -> list[dict]:
    """Agrupa e limpa formatos para o select único de qualidade.

    - Vídeo: só ``mp4`` com ``filesize`` conhecido, 1 por altura (maior
      ``filesize`` vence = melhor qualidade dentro da resolução).
    - Áudio: só com ``abr`` e ``filesize`` conhecidos, 1 por faixa
      (Alta/Media/Baixa, maior ``abr`` vence, desempate por ``filesize``).
    - Entradas sem tamanho/bitrate são ocultadas (regra aprovada: Ocultar).
    """
    video_best: dict[int, dict] = {}
    for f in formats:
        if f.get("kind") != "video" or (f.get("ext") or "").lower() != "mp4":
            continue
        if not f.get("filesize"):
            continue
        h = _height_from_resolution(f.get("resolution"))
        if h <= 0:
            continue
        cur = video_best.get(h)
        if cur is None or (f["filesize"] or 0) > (cur["filesize"] or 0):
            video_best[h] = f
    audio_best: dict[str, dict] = {}
    for f in formats:
        if f.get("kind") != "audio":
            continue
        if not f.get("abr") or not f.get("filesize"):
            continue
        tier = _audio_tier(f.get("abr"))
        cur = audio_best.get(tier)
        if cur is None or ((f["abr"] or 0), (f["filesize"] or 0)) > (
            (cur["abr"] or 0),
            (cur["filesize"] or 0),
        ):
            audio_best[tier] = f
    videos = sorted(
        video_best.values(),
        key=lambda f: _height_from_resolution(f.get("resolution")),
        reverse=True,
    )
    audios = sorted(audio_best.values(), key=lambda f: f.get("abr") or 0, reverse=True)
    return [*videos, *audios]


def validate_youtube_url(url: str) -> str:
    cleaned = (url or "").strip()
    if not cleaned:
        raise ValueError("URL vazia")
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("URL deve começar com http(s)")
    # L1: hostname (sem porta, lower) em vez de netloc — aceita :443 e case.
    host = (parsed.hostname or "").lower()
    if host not in YOUTUBE_HOSTS:
        raise ValueError("URL não é do YouTube")
    if host in _SHORT_HOSTS:
        if len(parsed.path.strip("/")) < 3:
            raise ValueError("Link youtu.be sem id")
        return cleaned
    if parsed.path == "/watch" and _video_id_from_query(parsed.query):
        return cleaned
    if parsed.path.startswith("/shorts/") and parsed.path.split("/")[2]:
        return cleaned
    if parsed.path.startswith("/embed/") and parsed.path.split("/")[2]:
        return cleaned
    raise ValueError("Link do YouTube sem id de vídeo (use watch, youtu.be ou shorts)")


def _video_id_from_query(query: str) -> str:
    """Extrai o parâmetro ``v`` da query string de um link /watch.

    ``parse_qs`` descarta valores vazios (``v=``), portanto um link sem id real
    é rejeitado por :func:`validate_youtube_url`.
    """
    values = parse_qs(query or "").get("v") or []
    return values[0].strip() if values else ""


def sanitize_filename(name: str, max_len: int = 80) -> str:
    cleaned = _SAFE_NAME.sub("", name or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return "video"
    return cleaned[:max_len].rstrip()


def format_duration(sec: int | None) -> str:
    if sec is None:
        return "--:--"
    try:
        s = int(sec)
    except (TypeError, ValueError):
        return "--:--"
    h, rem = divmod(max(0, s), 3600)
    m, sec2 = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec2:02d}"
    return f"{m}:{sec2:02d}"


def ensure_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg não encontrado. Instale com: sudo apt install ffmpeg")


def get_info(url: str) -> dict:
    url = validate_youtube_url(url)
    cached = _cache_get(url)
    if cached is not None:
        return cached
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
        "socket_timeout": _SOCKET_TIMEOUT,
        # M4: uma camada de retry só (aqui). yt-dlp interno em 1 para não
        # multiplicar tentativas (3x externo × 3x interno = 9). Fragmentos
        # mantêm 3 (só relevantes em download, inofensivo no info).
        "retries": 1,
        "fragment_retries": 3,
        "retry_sleep": "exp=1:4",
    }
    raw = None
    for attempt in range(3):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                raw = ydl.extract_info(url, download=False)
            break
        except YoutubeDLError as e:
            msg = str(e).lower()
            retryable = ("429" in msg or "too many requests" in msg) or (
                "timed out" in msg or "timeout" in msg or "unable to download" in msg
            )
            # Erros de domínio não são retryable: falham rápido sem backoff.
            if "private" in msg:
                raise VideoNotFoundError("Vídeo privado") from e
            if "unavailable" in msg or "not found" in msg:
                raise VideoNotFoundError("Vídeo indisponível ou removido") from e
            if "removed" in msg or "no video formats found" in msg:
                raise VideoNotFoundError("Vídeo indisponível ou removido") from e
            if "sign in" in msg or "confirm your age" in msg or "age-restricted" in msg:
                raise PermissionError("Vídeo com restrição de idade/login") from e
            if retryable and attempt < 2:
                if not TESTING:
                    time.sleep(2**attempt + random.uniform(0, 0.5))
                continue
            # L3: detalhe genérico ao cliente; upstream vai só para o log.
            log.warning("get_info falhou (rede/429): %s", e)
            raise ConnectionError("Falha ao ler YouTube") from e
    if not raw or not raw.get("id"):
        raise VideoNotFoundError("Não foi possível extrair infos")
    formats = []
    for f in raw.get("formats") or []:
        ext = (f.get("ext") or "").lower()
        if ext not in ("mp4", "webm", "m4a"):
            continue
        vcodec = f.get("vcodec") or "none"
        acodec = f.get("acodec") or "none"
        kind = "video" if vcodec != "none" else "audio"
        formats.append(
            {
                "format_id": str(f.get("format_id")),
                "ext": ext,
                "resolution": f.get("resolution") or f.get("format_note"),
                "abr": f.get("abr"),
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "vcodec": vcodec,
                "acodec": acodec,
                "kind": kind,
            }
        )
    views = raw.get("view_count")
    info = {
        "id": raw.get("id"),
        "title": raw.get("title") or "Sem título",
        "channel": raw.get("channel") or raw.get("uploader") or "—",
        "duration_sec": raw.get("duration"),
        "duration_str": format_duration(raw.get("duration")),
        "views": views,
        "upload_date": raw.get("upload_date"),
        "thumbnail": raw.get("thumbnail"),
        "formats": _curate_formats(formats),
    }
    _cache_set(url, info)
    return info


def build_download_command(url: str, format_id: str, audio_only: bool) -> list[str]:
    """Monta o comando yt-dlp para download via stdout (raw, sem pós-processo).

    Regras (Ruling do projeto):
    - ``format_id`` é validado por regex estrita (sem ``+``/``/`` do cliente).
    - Vídeo: seletor ``{format_id}+bestaudio/best`` — YouTube não serve mais
      progressivos; sem o merge o subprocess sai mudo.
    - Áudio: ``-f {format_id}`` — download do stream bruto para stdout.
    - ``ensure_ffmpeg()`` é chamado em ambos os caminhos: o container correto
      (MP4 para vídeo, MP3 para áudio) é garantido pelo pipe ffmpeg externo
      (``build_ffmpeg_command``), não pelo yt-dlp.
    """
    url = validate_youtube_url(url)
    if not _FORMAT_ID.match(format_id or ""):
        raise ValueError("format_id inválido")
    # ffmpeg é obrigatório tanto para remux de vídeo (MKV→MP4) quanto para
    # transcode de áudio (M4A→MP3). Falhar cedo evita subprocess mudo.
    ensure_ffmpeg()
    cmd = [sys.executable, "-m", "yt_dlp", "--ignore-config"]
    if audio_only:
        # Download do stream de áudio bruto. A conversão para MP3 é feita
        # pelo ffmpeg em ``build_ffmpeg_command`` — yt-dlp NÃO aplica
        # ``--extract-audio`` quando escreve em stdout (``-o -``).
        cmd += ["-f", format_id]
    else:
        # Seletor com fallback: tenta video+bestaudio; se falhar, usa o melhor
        # stream único disponível. O merge gera Matroska por default — o
        # remux para MP4 é feito pelo ffmpeg em ``build_ffmpeg_command``.
        cmd += ["-f", f"{format_id}+bestaudio/best"]
    cmd += ["--no-playlist", "--quiet", "--no-warnings", "-o", "-", url]
    return cmd


def build_ffmpeg_command(audio_only: bool) -> list[str]:
    """Monta o comando ffmpeg para pós-processamento via pipe.

    - Vídeo: ``-c copy -f mp4 -movflags frag_keyframe+empty_moov`` — remux
      MKV→MP4 fragmentado sem re-encoding. Fragmentado porque ``pipe:1`` é
      non-seekable; o MP4 tradicional precisa escrever o ``moov`` atom no
      final (seek), o que falha em pipes. Fragmented MP4 é compatível com
      todos os browsers modernos e players.
    - Áudio: ``-f mp3 -ab 192k`` — transcode M4A/AAC→MP3 CBR 192 kbps.
      192 kbps é o padrão de qualidade para MP3 (compatibilidade universal
      vs. 128 kbps baixo ou 320 kbps pesado).
    """
    if audio_only:
        return [
            "ffmpeg",
            "-i",
            "pipe:0",
            "-vn",
            "-f",
            "mp3",
            "-ab",
            "192k",
            "pipe:1",
        ]
    return [
        "ffmpeg",
        "-i",
        "pipe:0",
        "-c",
        "copy",
        "-f",
        "mp4",
        "-movflags",
        "frag_keyframe+empty_moov",
        "pipe:1",
    ]
