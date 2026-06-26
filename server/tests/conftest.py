"""
Test fixtures — her test için temiz bir AppState örneği ve
ürün listesi yüklenmiş hazır state sağlar.
"""
import sys
from pathlib import Path
import pytest

# server/ klasörünü import path'e ekle
sys.path.insert(0, str(Path(__file__).parent.parent))

from state import AppState

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
    # session.json / settings.json tmp dizininde oluşsun
    monkeypatch.chdir(tmp_path)

    s = AppState.__new__(AppState)
    s.session = __import__("models").CountSession()
    s.settings = {"delimiter": "auto", "barcode_rules": []}
    s._lock = __import__("asyncio").Lock()
    s._connections = {}
    s._all_ws = set()
    s._alt_to_primary = {}

    s.load_product_list(PRODUCTS, COLUMN_MAP, ALT_TO_PRIMARY)
    s.start_session()
    s._persist = lambda: None   # diske yazmayı kapat
    return s
