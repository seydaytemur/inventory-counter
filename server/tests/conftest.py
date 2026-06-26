"""
Test fixtures — her test için temiz bir AppState örneği ve
ürün listesi yüklenmiş hazır state sağlar.
"""
import sys
import asyncio
from pathlib import Path
import pytest

# server/ klasörünü import path'ine ekle (models, state buradan gelir)
SERVER_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SERVER_DIR))

import state as state_module
from state import AppState
from models import CountSession

PRODUCTS = {
    "ZEB-AAA001": {"URUN_ADI": "Ürün A", "SAP_KODU": "ZEB-AAA001"},
    "ZEB-BBB002": {"URUN_ADI": "Ürün B", "SAP_KODU": "ZEB-BBB002"},
    "ZEB-CCC003": {"URUN_ADI": "Ürün C", "SAP_KODU": "ZEB-CCC003"},
}

ALT_TO_PRIMARY = {
    "1PAAA001": "ZEB-AAA001",
    "1PBBB002": "ZEB-BBB002",
}

COLUMN_MAP = {"barcode_col": "SAP_KODU", "name_col": "URUN_ADI"}


@pytest.fixture
def state(tmp_path, monkeypatch):
    """Dosyaya yazmayan, ürün listesi yüklenmiş temiz AppState."""
    # SESSION_FILE ve SETTINGS_FILE'ı tmp dizinine yönlendir
    monkeypatch.setattr(state_module, "SESSION_FILE", tmp_path / "session.json")
    monkeypatch.setattr(state_module, "SESSION_TMP",  tmp_path / "session.tmp")
    monkeypatch.setattr(state_module, "SETTINGS_FILE", tmp_path / "settings.json")

    s = AppState()
    s.load_product_list(PRODUCTS, COLUMN_MAP, ALT_TO_PRIMARY)
    s.start_session()
    s._persist = lambda: None   # diske yazmayı kapat
    return s
