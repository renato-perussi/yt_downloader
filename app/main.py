from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import yt

log = logging.getLogger("yt_downloader")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="YT Downloader")

# Rate-limit em memória por IP: protege o IP contra rajadas que disparam
# 429 no YouTube. Leve e sem dependências (suficiente p/ Docker single-node).
# M1: bound por maxsize + purga (evita crescimento monotônico).
# L4: flag explícita app.state.testing / yt.TESTING (conftest seta em testes).
_RATE_STORE: dict[tuple[str, str], list[float]] = {}
_RATE_LIMITS = {"info": 20, "download": 10}
_RATE_MAXKEYS = 5000


def clear_rate_limit_store() -> None:
    _RATE_STORE.clear()


def _check_rate_limit(ip: str, endpoint: str, limit: int, window: int = 60) -> None:
    now = time.monotonic()
    key = (ip or "unknown", endpoint)
    hits = [t for t in _RATE_STORE.get(key, []) if now - t < window]
    if len(hits) >= limit:
        retry_after = int(window - (now - hits[0])) + 1
        raise HTTPException(
            status_code=429,
            detail="Muitas requisições, tente novamente",
            headers={"Retry-After": str(retry_after)},
        )
    hits.append(now)
    _RATE_STORE[key] = hits
    # M1: purga chaves expiradas + cap FIFO.
    if len(_RATE_STORE) > _RATE_MAXKEYS:
        for k, v in list(_RATE_STORE.items()):
            if not v or now - v[-1] >= window:
                _RATE_STORE.pop(k, None)
            if len(_RATE_STORE) <= _RATE_MAXKEYS:
                break
        while len(_RATE_STORE) > _RATE_MAXKEYS:
            _RATE_STORE.pop(next(iter(_RATE_STORE)))


def _maybe_rate_limit(request: Request, endpoint: str) -> None:
    # L4: flags explícitas em vez de sniff de env no hot path.
    if getattr(app.state, "testing", False) or yt.TESTING:
        return
    ip = request.client.host if request.client else "unknown"
    _check_rate_limit(ip, endpoint, _RATE_LIMITS[endpoint])


def _safe_terminate(proc: subprocess.Popen) -> None:
    """Terminate subprocess with kill fallback. Idempotent, never raises.

    Usado quando detectamos falha antes/durante streaming — garante que o
    processo filho não vire órfão, mesmo que ``wait()`` timeout.
    """
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        pass


# M2: headers de segurança (mitiga XSS via CDN de terceiros).
@app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://unpkg.com "
        "https://code.iconify.design; "
        "style-src 'self' 'unsafe-inline' https:; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' https: data:; "
        "connect-src 'self' https://api.iconify.design https://api.unisvg.com "
        "https://api.simplesvg.com; "
        "frame-ancestors 'self'"
    )
    resp.headers.setdefault("Content-Security-Policy", csp)
    return resp


# M5: cap de downloads concorrentes (fase de setup do pipeline).
# 503 com Retry-After quando lotado — protege CPU/banda do single-node.
_DOWNLOAD_SEM = threading.Semaphore(3)
_INFO_TIMEOUT = 30


# Observação de contrato: ``Query(..., min_length=8)`` faz o FastAPI retornar
# 422 (Unprocessable Entity) automaticamente para URLs vazias ou curtas demais,
# antes mesmo de entrar no handler. Não sobrescrevemos esse comportamento.
@app.get("/api/info")
async def api_info(request: Request, url: str = Query(..., min_length=8)):
    _maybe_rate_limit(request, "info")
    # Validação rápida (síncrona) antes de despachar para a thread pool:
    # evita custo de to_thread para URLs inválidas óbvias.
    try:
        yt.validate_youtube_url(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        # M4: timeout global — não segurar thread do pool por minutos.
        info = await asyncio.wait_for(asyncio.to_thread(yt.get_info, url), timeout=_INFO_TIMEOUT)
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail="Tempo esgotado ao ler YouTube") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    # Ordem crítica: VideoNotFoundError (domínio) ANTES de LookupError (base).
    # LookupError genérico inclui KeyError/IndexError de bugs internos — esses
    # devem resultar 500, não 404.
    except yt.VideoNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except PermissionError as e:
        raise HTTPException(status_code=451, detail=str(e)) from e
    except ConnectionError as e:
        # L3: detalhe genérico (upstream já logado em yt.get_info).
        log.warning("info failed (rede/429)")
        raise HTTPException(status_code=502, detail="Falha ao ler YouTube") from e
    except RuntimeError as e:
        log.exception("info failed: runtime")
        detail = str(e) if "ffmpeg" in str(e).lower() else "Erro interno"
        raise HTTPException(status_code=500, detail=detail) from e
    except LookupError as e:
        # Bug interno (KeyError/IndexError) — não expor detalhe ao cliente.
        log.exception("info failed: unexpected LookupError")
        raise HTTPException(status_code=500, detail="Erro interno") from e
    except Exception as e:  # noqa: BLE001
        log.exception("info failed")
        raise HTTPException(status_code=500, detail="Erro interno") from e
    return info


@app.get("/api/download")
async def api_download(
    request: Request,
    url: str = Query(..., min_length=8),
    format_id: str = Query(..., min_length=1, max_length=32),
    audio_only: bool = False,
):
    """Streaming de mídia (vídeo/áudio) via pipeline subprocess → ``StreamingResponse``.

    Pipeline:
    1. ``build_download_command`` valida URL + format_id (síncrono, rápido).
    2. ``get_info`` via ``to_thread`` obtém o título para o filename seguro.
    3. ``Popen`` yt-dlp com ``-o -`` (stdout) → pipe → ``Popen`` ffmpeg com
       ``pipe:0``/``pipe:1`` (remux MKV→MP4 para vídeo, transcode M4A→MP3
       para áudio). O generator lê do stdout do ffmpeg em chunks.
    4. ``StreamingResponse`` retorna attachment com Content-Disposition.

    Por que dois processos?
    - yt-dlp ``--extract-audio`` com ``-o -`` não funciona: escreve o stream
      bruto (M4A/AAC) em stdout, ignorando o pós-processo.
    - yt-dlp merge com ``-o -`` produz Matroska por default, não MP4.
    - ffmpeg resolve ambos: remux ``-c copy -f mp4`` (vídeo) e transcode
      ``-f mp3`` (áudio).

    Erros:
    - ``ValueError`` (URL/format_id inválidos) → 400.
    - ``RuntimeError`` (ffmpeg ausente) → 500.
    - ``get_info`` falha → filename default (não bloqueia o download).
    """
    _maybe_rate_limit(request, "download")
    # M5: recusa rápida sob lotação (evita acumular ffmpeg/CPU).
    if not _DOWNLOAD_SEM.acquire(blocking=False):
        raise HTTPException(
            status_code=503,
            detail="Servidor ocupado, tente novamente",
            headers={"Retry-After": "10"},
        )
    try:
        yt_cmd = yt.build_download_command(url, format_id, audio_only)
    except ValueError as e:
        _DOWNLOAD_SEM.release()
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        log.exception("download failed: runtime")
        _DOWNLOAD_SEM.release()
        detail = str(e) if "ffmpeg" in str(e).lower() else "Erro interno"
        raise HTTPException(status_code=500, detail=detail) from e
    except Exception as e:  # noqa: BLE001
        log.exception("download failed: unexpected build error")
        _DOWNLOAD_SEM.release()
        raise HTTPException(status_code=500, detail="Erro interno") from e
    ff_cmd = yt.build_ffmpeg_command(audio_only)
    # get_info serve apenas para obter o título como filename legível.
    # Falha aqui NÃO deve bloquear o download — usa título default.
    # M4: timeout curto; cache (TTL 15min) faz a maioria acertar sem rede.
    try:
        info = await asyncio.wait_for(asyncio.to_thread(yt.get_info, url), timeout=15)
    except Exception:  # noqa: BLE001
        log.warning("get_info failed for download filename, using default", exc_info=True)
        info = {"title": "video"}
    title = info.get("title", "video") if isinstance(info, dict) else "video"
    safe = yt.sanitize_filename(title)
    ext = "mp3" if audio_only else "mp4"
    filename = f"{safe}.{ext}"

    # Pipeline: yt-dlp (stdout) → ffmpeg (stdout → nosso reader).
    # Abrir AMBOS os Popen ANTES de montar o StreamingResponse: se falhar no
    # start (OSError / binário ausente), retornamos 5xx antes dos headers,
    # evitando status 200 truncado.
    #
    # Try/except ``BaseException`` cobre cancelamento assíncrono
    # (``CancelledError`` é ``BaseException`` em Python 3.8+): se a task for
    # cancelada entre Popen e StreamingResponse, os processos filhos vazam.
    # _safe_terminate é idempotente — chamá-lo em duplicidade não faz mal.
    yt_proc: subprocess.Popen | None = None
    ff_proc: subprocess.Popen | None = None
    sem_held = True
    try:
        try:
            # yt-dlp: stdout=PIPE conecta ao stdin do ffmpeg.
            yt_proc = await asyncio.to_thread(
                subprocess.Popen,
                yt_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            if yt_proc.stdout is None:
                _safe_terminate(yt_proc)
                raise HTTPException(status_code=500, detail="Falha interna ao abrir stream")
            # ffmpeg: stdin=yt-dlp stdout, stdout=PIPE para nosso reader.
            ff_proc = await asyncio.to_thread(
                subprocess.Popen,
                ff_cmd,
                stdin=yt_proc.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            # Pipeline clássico: após passar yt_proc.stdout como stdin do ffmpeg,
            # fechar no pai. Se não fechar, o pai mantém o write-end aberto e
            # yt-dlp não recebe SIGPIPE quando ffmpeg termina (causa órfão/hang).
            # O yt_proc.stdout.close() aqui não afeta o pipe já passado ao ffmpeg.
            yt_proc.stdout.close()
        except OSError as e:
            log.exception("failed to spawn download subprocess")
            raise HTTPException(status_code=500, detail="Falha ao iniciar download") from e
        if ff_proc.stdout is None:
            _safe_terminate(yt_proc)
            _safe_terminate(ff_proc)
            raise HTTPException(status_code=500, detail="Falha interna ao abrir stream")

        # 1º chunk bloqueante curto (via to_thread): detecta falha ANTES dos
        # headers. Se a pipeline morrer na largada, stdout será vazio — fazemos
        # wait() para obter o rc real. rc != 0 ⇒ 502 antes dos headers.
        try:
            first_chunk: bytes = await asyncio.to_thread(ff_proc.stdout.read, 64 * 1024)
        except Exception as e:  # noqa: BLE001
            log.exception("failed reading first chunk from subprocess")
            _safe_terminate(yt_proc)
            _safe_terminate(ff_proc)
            raise HTTPException(status_code=502, detail="Falha ao ler stream") from e

        if not first_chunk:
            try:
                rc = await asyncio.to_thread(ff_proc.wait, 5)
            except subprocess.TimeoutExpired:
                _safe_terminate(yt_proc)
                _safe_terminate(ff_proc)
                log.warning(
                    "ffmpeg hung with empty output: yt_cmd=%s ff_cmd=%s",
                    yt_cmd,
                    ff_cmd,
                )
                raise HTTPException(status_code=502, detail="Download falhou (timeout)")
            if rc != 0:
                log.warning(
                    "ffmpeg exited before producing data: rc=%s yt_cmd=%s ff_cmd=%s",
                    rc,
                    yt_cmd,
                    ff_cmd,
                )
                raise HTTPException(status_code=502, detail="Download falhou")
        else:
            # Fail-fast para header parcial inválido: ffmpeg pode escrever alguns
            # bytes (ex: "ftyp" header) e sair com rc!=0 (codec incompatível,
            # muxer error). first_chunk não-vazio NÃO implica sucesso.
            #
            # Race guard: poll() imediatamente após read() pode retornar None
            # mesmo com stdout fechado (pipe fechado mas processo ainda não
            # reaped pelo kernel). Portanto, para output <64KB (primeiro read
            # completo não encheu o buffer), usamos wait(5) para obter o rc
            # real. TimeoutExpired ⇒ _safe_terminate + 502.
            #
            # Se rc!=0 e output pequeno (provavelmente só header), retornar 502
            # antes dos headers evita 200 com arquivo corrompido.
            if len(first_chunk) < 64 * 1024:
                try:
                    ff_rc = await asyncio.to_thread(ff_proc.wait, 5)
                except subprocess.TimeoutExpired:
                    _safe_terminate(yt_proc)
                    _safe_terminate(ff_proc)
                    log.warning(
                        "ffmpeg hung after %d bytes: yt_cmd=%s ff_cmd=%s",
                        len(first_chunk),
                        yt_cmd,
                        ff_cmd,
                    )
                    raise HTTPException(status_code=502, detail="Download falhou (timeout)")
                if ff_rc != 0:
                    log.warning(
                        "ffmpeg exited with rc=%s after %d bytes: yt_cmd=%s ff_cmd=%s",
                        ff_rc,
                        len(first_chunk),
                        yt_cmd,
                        ff_cmd,
                    )
                    raise HTTPException(
                        status_code=502, detail="Download falhou (formato inválido)"
                    )

    except BaseException:
        if yt_proc is not None:
            _safe_terminate(yt_proc)
        if ff_proc is not None:
            _safe_terminate(ff_proc)
        if sem_held:
            try:
                _DOWNLOAD_SEM.release()
            except Exception:  # noqa: BLE001
                pass
        raise

    def _iter():
        """Generator sync — Starlette executa em threadpool automaticamente.

        Observação de contrato: após o 1º chunk, se a pipeline falhar
        mid-stream, o status HTTP 200 já foi enviado — não há como retroagir.
        O ``finally`` loga os returncodes para troubleshooting, mas o cliente
        vê download truncado (trade-off intencional vs. buffering total, que
        mataria streaming).
        """
        try:
            if first_chunk:
                yield first_chunk
            while True:
                chunk = ff_proc.stdout.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                _DOWNLOAD_SEM.release()
            except Exception:  # noqa: BLE001
                pass
            try:
                ff_proc.stdout.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                yt_proc.stdout.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                ff_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                log.warning("ffmpeg did not exit in 10s, killing pid=%s", ff_proc.pid)
                _safe_terminate(ff_proc)
            try:
                yt_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                log.warning("yt-dlp did not exit in 10s, killing pid=%s", yt_proc.pid)
                _safe_terminate(yt_proc)
            if ff_proc.returncode not in (0, None):
                log.warning(
                    "ffmpeg exited non-zero during stream: rc=%s url=%s",
                    ff_proc.returncode,
                    url,
                )

    media_type = "audio/mpeg" if audio_only else "video/mp4"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(_iter(), media_type=media_type, headers=headers)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def root():
    index = STATIC_DIR / "index.html"
    if not index.exists():
        raise HTTPException(
            status_code=500, detail="Frontend ainda não construído (static/index.html ausente)"
        )
    return FileResponse(index)


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
