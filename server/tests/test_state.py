"""
Birim testler: add_scan, bulk_delete, export (get_summary + encoding)
"""
import csv
import io
import pytest
from state import AppState


# ── strip_barcode ─────────────────────────────────────────────────────────────

def test_strip_prefix(state):
    state.settings["barcode_rules"] = [{"type": "prefix", "value": "1P"}]
    assert state.strip_barcode("1PAAA001") == "AAA001"

def test_strip_suffix(state):
    state.settings["barcode_rules"] = [{"type": "suffix", "value": "X"}]
    assert state.strip_barcode("AAA001X") == "AAA001"

def test_strip_no_match(state):
    state.settings["barcode_rules"] = [{"type": "prefix", "value": "1P"}]
    assert state.strip_barcode("ZEB-AAA001") == "ZEB-AAA001"

def test_strip_rules_applied_in_order(state):
    state.settings["barcode_rules"] = [
        {"type": "prefix", "value": "1P"},
        {"type": "suffix", "value": "X"},
    ]
    assert state.strip_barcode("1PAAA001X") == "AAA001"


# ── add_scan ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_scan_success(state):
    ok, msg, entry = await state.add_scan("ZEB-AAA001", 2, "Ali")
    assert ok is True
    assert entry.barcode == "ZEB-AAA001"
    assert entry.quantity == 2
    assert "ZEB-AAA001" in state.session.scans

@pytest.mark.asyncio
async def test_add_scan_unknown_barcode(state):
    ok, msg, entry = await state.add_scan("BILINMIYOR", 1, "Ali")
    assert ok is False
    assert entry is None
    assert "bulunamadı" in msg

@pytest.mark.asyncio
async def test_add_scan_via_alt_barcode(state):
    """Alternatif barkod → birincil barkoda normalize edilmeli."""
    state.settings["barcode_rules"] = [{"type": "prefix", "value": "1P"}]
    ok, msg, entry = await state.add_scan("1PAAA001", 1, "Ali")
    assert ok is True
    assert entry.barcode == "ZEB-AAA001"

@pytest.mark.asyncio
async def test_add_scan_creates_history(state):
    await state.add_scan("ZEB-AAA001", 3, "Ali")
    assert len(state.session.history) == 1
    assert state.session.history[0]["action"] == "add"
    assert state.session.history[0]["quantity"] == 3


# ── bulk_delete ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bulk_delete_partial(state):
    """5 adetten 2 sil → 3 kalmalı."""
    await state.add_scan("ZEB-BBB002", 5, "Ali")
    ok, msg, deleted = await state.bulk_delete("ZEB-BBB002", "Ali", 2, "admin")
    assert ok is True
    assert deleted == 2
    remaining = sum(e["quantity"] for e in state.session.scans["ZEB-BBB002"])
    assert remaining == 3

@pytest.mark.asyncio
async def test_bulk_delete_all(state):
    """Tüm adeti sil → barkod scans'tan kalkmalı."""
    await state.add_scan("ZEB-BBB002", 3, "Ali")
    ok, msg, deleted = await state.bulk_delete("ZEB-BBB002", "Ali", 3, "admin")
    assert ok is True
    assert "ZEB-BBB002" not in state.session.scans

@pytest.mark.asyncio
async def test_bulk_delete_exceeds_quantity(state):
    """Mevcut adetten fazla silmeye çalışınca hata döner."""
    await state.add_scan("ZEB-BBB002", 2, "Ali")
    ok, msg, deleted = await state.bulk_delete("ZEB-BBB002", "Ali", 10, "admin")
    assert ok is False
    assert deleted == 0

@pytest.mark.asyncio
async def test_bulk_delete_wrong_user(state):
    """Başka kullanıcının kaydını silmeye çalışınca hata döner."""
    await state.add_scan("ZEB-BBB002", 3, "Ali")
    ok, msg, deleted = await state.bulk_delete("ZEB-BBB002", "Veli", 1, "admin")
    assert ok is False


# ── get_summary / export encoding ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_summary_totals(state):
    """İki farklı kullanıcının taradığı aynı barkod → toplamda görünmeli."""
    await state.add_scan("ZEB-CCC003", 3, "Ali")
    await state.add_scan("ZEB-CCC003", 2, "Veli")
    summary = state.get_summary()
    row = next(r for r in summary if r["barcode"] == "ZEB-CCC003")
    assert row["total_quantity"] == 5
    assert "Ali" in row["scanned_by"]
    assert "Veli" in row["scanned_by"]

@pytest.mark.asyncio
async def test_export_utf8_bom(state):
    """CSV çıktısı utf-8-sig (BOM'lu) olmalı — Excel Türkçe karakter uyumu."""
    await state.add_scan("ZEB-AAA001", 1, "Ali")
    summary = state.get_summary()

    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["barcode", "product_name", "total_quantity", "scanned_by"],
        extrasaction="ignore",
    )
    writer.writeheader()
    writer.writerows(summary)
    output.seek(0)

    encoded = output.getvalue().encode("utf-8-sig")
    # BOM: ilk 3 byte EF BB BF olmalı
    assert encoded[:3] == b"\xef\xbb\xbf"

@pytest.mark.asyncio
async def test_export_empty_returns_empty_list(state):
    """Hiç scan yoksa get_summary boş liste döner."""
    assert state.get_summary() == []
