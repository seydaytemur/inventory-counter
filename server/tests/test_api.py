"""
API testleri: HTTP endpoint'leri gerçek FastAPI uygulaması üzerinde test eder.
Sunucu başlatılmaz — httpx AsyncClient ile hafıza içinde çalışır.
"""
import pytest
import state as state_module
from httpx import AsyncClient, ASGITransport
from main import app, app_state

PRODUCTS = {
    "ZEB-AAA001": {"URUN_ADI": "Ürün A", "SAP_KODU": "ZEB-AAA001"},
    "ZEB-BBB002": {"URUN_ADI": "Ürün B", "SAP_KODU": "ZEB-BBB002"},
}
COLUMN_MAP = {"barcode_col": "SAP_KODU", "name_col": "URUN_ADI"}


@pytest.fixture(autouse=True)
def reset_state(tmp_path, monkeypatch):
    """Her testten önce app_state'i sıfırla, dosyalara yazma."""
    monkeypatch.setattr(state_module, "SESSION_FILE", tmp_path / "session.json")
    monkeypatch.setattr(state_module, "SESSION_TMP",  tmp_path / "session.tmp")
    monkeypatch.setattr(state_module, "SETTINGS_FILE", tmp_path / "settings.json")

    app_state.__init__()
    app_state._persist = lambda: None
    app_state.save_settings = lambda s: setattr(app_state, "settings", s)
    yield


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ── /api/settings ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_settings_returns_defaults(client):
    async with client as c:
        res = await c.get("/api/settings")
    assert res.status_code == 200
    assert "barcode_rules" in res.json()


@pytest.mark.asyncio
async def test_post_settings_valid(client):
    async with client as c:
        res = await c.post("/api/settings", json={
            "delimiter": ";",
            "barcode_rules": [{"type": "prefix", "value": "1P"}],
        })
    assert res.status_code == 200
    assert res.json()["status"] == "ok"
    assert app_state.settings["delimiter"] == ";"


@pytest.mark.asyncio
async def test_post_settings_invalid_delimiter(client):
    async with client as c:
        res = await c.post("/api/settings", json={
            "delimiter": "YANLIS",
            "barcode_rules": [],
        })
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_post_settings_invalid_rule_type(client):
    async with client as c:
        res = await c.post("/api/settings", json={
            "delimiter": "auto",
            "barcode_rules": [{"type": "middle", "value": "1P"}],
        })
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_post_settings_empty_rule_value(client):
    async with client as c:
        res = await c.post("/api/settings", json={
            "delimiter": "auto",
            "barcode_rules": [{"type": "prefix", "value": "   "}],
        })
    assert res.status_code == 400


# ── /api/session ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_session_status_initially_inactive(client):
    async with client as c:
        res = await c.get("/api/session/status")
    assert res.status_code == 200
    assert res.json()["is_active"] is False


@pytest.mark.asyncio
async def test_start_session_without_products_fails(client):
    async with client as c:
        res = await c.post("/api/session/start")
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_start_and_finish_session(client):
    app_state.load_product_list(PRODUCTS, COLUMN_MAP, {})
    async with client as c:
        res = await c.post("/api/session/start")
        assert res.status_code == 200
        assert app_state.session.is_active is True

        res = await c.post("/api/session/finish")
        assert res.status_code == 200
        assert app_state.session.is_active is False


@pytest.mark.asyncio
async def test_finish_session_when_not_active_fails(client):
    async with client as c:
        res = await c.post("/api/session/finish")
    assert res.status_code == 400


# ── /api/export ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_export_empty_returns_404(client):
    async with client as c:
        res = await c.get("/api/export")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_export_returns_csv_when_scans_exist(client):
    app_state.load_product_list(PRODUCTS, COLUMN_MAP, {})
    app_state.start_session()
    await app_state.add_scan("ZEB-AAA001", 3, "Ali")

    async with client as c:
        res = await c.get("/api/export")

    assert res.status_code == 200
    assert "text/csv" in res.headers["content-type"]
    # UTF-8 BOM kontrolü
    assert res.content[:3] == b"\xef\xbb\xbf"
