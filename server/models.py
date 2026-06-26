from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class ScanEntry(BaseModel):
    """Tek bir barkod okutma kaydı"""
    id: str                        # uuid
    barcode: str                   # okunan barkod (ST veya PN)
    quantity: int                  # girilen adet
    user: str                      # okuyan kişi
    timestamp: str                 # ISO format
    product_name: str              # SAP listesinden gelen ürün adı
    extra_fields: dict = {}        # SAP CSV'den gelen diğer kolonlar


class HistoryEntry(BaseModel):
    """History paneli için aksiyon kaydı"""
    id: str
    action: str                    # "add" | "edit" | "delete" | "undo"
    user: str
    barcode: str
    product_name: str
    quantity: int
    previous_quantity: Optional[int] = None  # edit geri alımı için eski değer
    timestamp: str
    related_scan_id: str           # hangi ScanEntry'e ait


class CountSession(BaseModel):
    """Aktif sayım oturumu — RAM'de yaşar"""
    is_active: bool = False
    started_at: Optional[str] = None
    finished_at: Optional[str] = None

    # SAP'tan yüklenen ürün listesi: { barcode -> {kolon: deger} }
    product_list: dict = {}

    # Kullanıcının seçtiği kolon eşleştirmeleri
    column_map: dict = {}          # örn: {"barcode_col": "ST", "name_col": "Malzeme Adı"}

    # Aktif sayım verileri: { barcode -> ScanEntry listesi }
    # Aynı barkod birden fazla kez eklenebilir, bunlar toplanır
    scans: dict = {}               # barcode -> list[ScanEntry]

    # History (silme, ekleme, geri alma) — sadece ana makinede görünür
    history: list = []


# ── WebSocket mesaj tipleri ──────────────────────────────────────────────────

class WsMessageType:
    # Terminal → Sunucu
    SCAN       = "scan"           # barkod + adet gönder
    DELETE     = "delete"         # scan_id sil
    # Sunucu → Terminal / Admin
    STATE      = "state"          # tam sayım durumu (ilk bağlanınca)
    SCAN_OK    = "scan_ok"        # başarılı okutma onayı
    SCAN_ERR   = "scan_err"       # hata (bilinmeyen barkod vb.)
    SCAN_NEW   = "scan_new"       # yeni kayıt eklendi (broadcast)
    SCAN_DEL   = "scan_del"       # kayıt silindi (broadcast)
    SCAN_UPD   = "scan_upd"       # kayıt adeti güncellendi (broadcast)
    HISTORY    = "history_update" # history güncellendi (broadcast)
    SESSION    = "session"        # oturum durumu değişti
    USERS      = "users"          # bağlı kullanıcılar güncellendi


# ── HTTP istek gövdeleri ─────────────────────────────────────────────────────

class DeleteRequest(BaseModel):
    scan_id: str
    user: str

class BulkDeleteRequest(BaseModel):
    barcode: str         # hangi ürün
    target_user: str     # kimin kayıtları silinecek
    qty_to_delete: int   # kaç adet silinecek (Field gt=0 değil — validasyon endpoint'te)
    admin_user: str = "admin"

class UndoRequest(BaseModel):
    history_id: str
    user: str