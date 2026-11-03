from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
STATIC_ROOT = Path(__file__).resolve().parent.parent / "static"


def test_frontend_hooks_exist():
    root = STATIC_ROOT
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "app.js").read_text(encoding="utf-8")
    hooks = [
        "url-input",
        "fetch-btn",
        "status",
        "hero",
        "result",
        "type-select",
        "quality-select",
        "download-btn",
    ]
    for hook in hooks:
        assert hook in html, hook
    # Fidelidade ao FormFlow: classes exatas do design system, nada inventado
    for cls in ["gradient-swirl", "tracing-beam", "card-glow", "theme-toggle", "text-transition"]:
        assert cls in html, cls
    assert "<select" in html, "formatos devem usar campo select"
    assert "/api/info" in js
    assert "/api/download" in js
    assert "MP3" in js, "áudio deve ser rotulado como MP3 (saída do ffmpeg)"
    assert "visualiza" in js, "views devem ser localizadas em pt-BR"
    assert "dockHero" in js, "busca deve recolher o hero para o topo"
    assert "min-h-[calc(100vh-4rem)]" in html, "hero deve ocupar a viewport no estado inicial"


def test_static_assets_are_served():
    """Os assets do frontend precisam ser servidos pelo mount /static."""
    for asset in ["index.html", "app.js", "styles.css"]:
        r = client.get(f"/static/{asset}")
        assert r.status_code == 200, asset
        assert r.content, asset


def test_index_html_references_static_assets():
    """O HTML precisa referenciar os assets pelo caminho servido pelo mount."""
    html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    assert "/static/styles.css" in html
    assert "/static/app.js" in html
