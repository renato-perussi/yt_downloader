import pytest

from app import yt


@pytest.fixture(autouse=True)
def _clear_antiblock_stores():
    from app.main import app

    prev_yt_testing = yt.TESTING
    prev_app_testing = getattr(app.state, "testing", False)
    yt.TESTING = True
    app.state.testing = True
    yt.clear_info_cache()
    try:
        from app.main import clear_rate_limit_store

        clear_rate_limit_store()
    except Exception:
        pass
    yield
    yt.clear_info_cache()
    try:
        from app.main import clear_rate_limit_store

        clear_rate_limit_store()
    except Exception:
        pass
    yt.TESTING = prev_yt_testing
    app.state.testing = prev_app_testing
