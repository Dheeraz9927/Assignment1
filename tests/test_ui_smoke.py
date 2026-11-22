"""Renders every Streamlit page headlessly against a running API (skipped when the API is not up)."""

from pathlib import Path

import httpx
import pytest

UI = Path(__file__).resolve().parent.parent / "ui"


def _api_up() -> bool:
    try:
        return httpx.get("http://localhost:8000/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(not _api_up(), reason="API not running on :8000")


@pytest.mark.parametrize("page", ["app.py", "pages/1_Ask.py", "pages/2_Index.py", "pages/3_Evaluation.py",
                                  "pages/4_Agent.py", "pages/5_Audit_Log.py"])
def test_page_renders_without_exceptions(page, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.syspath_prepend(str(UI))
    at = AppTest.from_file(str(UI / page), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
