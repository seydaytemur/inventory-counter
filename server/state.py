"""
state.py — Tüm sayım verisi RAM'de burada yaşar.
Eş zamanlı barkod yazma işlemleri asyncio.Lock ile sıralanır.
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone, timedelta

TZ_TR = timezone(timedelta(hours=3))
from pathlib import Path
from typing import Optional
from models import CountSession, ScanEntry, HistoryEntry, WsMessageType


SETTINGS_FILE = Path(__file__).parent / "settings.json"
SESSION_FILE  = Path(__file__).parent / "session.json"
SESSION_TMP   = Path(__file__).parent / "session.tmp"

DEFAULT_SETTINGS: dict = {
    "delimiter": "auto",          # auto | ; | , | \t | |
    "barcode_rules": [
        {"type": "prefix", "value": "1P"},
    ],
}


def _now() -> str:
    return datetime.now(TZ_TR).isoformat()


def _uid() -> str:
    return str(uuid.uuid4())


class AppState:
    def __init__(self):
        self.session = CountSession()
        self.settings: dict = self._load_settings()
        self._lock = asyncio.Lock()
        self._connections: dict[str, set] = {}
        self._all_ws: set = set()
        self._alt_to_primary: dict = {}  # alt barkod → birincil barkod
        self._load_session()             # crash recovery: varsa önceki oturumu yükle

    # ── Ayarlar ──────────────────────────────────────────────────────────────

    def _load_settings(self) -> dict:
        if SETTINGS_FILE.exists():
            try:
                return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return dict(DEFAULT_SETTINGS)

    # ── Oturum kalıcılığı ────────────────────────────────────────────────────

    def _load_session(self):
        """Sunucu başlarken session.json varsa yükle (crash recovery)."""
        if not SESSION_FILE.exists():
            return
        try:
            data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            self.session.is_active    = data.get("is_active", False)
            self.session.started_at   = data.get("started_at")
            self.session.finished_at  = data.get("finished_at")
            self.session.scans        = data.get("scans", {})
            self.session.history      = data.get("history", [])
            self.session.column_map   = data.get("column_map", {})
            self.session.product_list = data.get("product_list", {})
            self._alt_to_primary      = data.get("alt_to_primary", {})
        except Exception:
            pass  # bozuk dosya → temiz başlangıç

    def _persist(self):
        """Mevcut oturum verisini diske atomik olarak yaz."""
        data = {
            "is_active":      self.session.is_active,
            "started_at":     self.session.started_at,
            "finished_at":    self.session.finished_at,
            "scans":          self.session.scans,
            "history":        self.session.history,
            "column_map":     self.session.column_map,
            "product_list":   self.session.product_list,
            "alt_to_primary": self._alt_to_primary,
        }
        SESSION_TMP.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        SESSION_TMP.replace(SESSION_FILE)  # atomik rename

    def save_settings(self, new_settings: dict):
        self.settings = new_settings
        SETTINGS_FILE.write_text(
            json.dumps(new_settings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def strip_barcode(self, raw: str) -> str:
        """Ayarlardaki barkod temizleme kurallarını sırayla uygular."""
        s = raw.strip().upper()
        for rule in self.settings.get("barcode_rules", []):
            val = rule.get("value", "").upper()
            if not val:
                continue
            if rule.get("type") == "prefix" and s.startswith(val):
                s = s[len(val):]
            elif rule.get("type") == "suffix" and s.endswith(val):
                s = s[:-len(val)]
        return s

    # ── Ürün listesi ──────────────────────────────────────────────────────────

    def load_product_list(self, products: dict, column_map: dict, alt_to_primary: dict = None):
        """CSV parse sonrası ürün listesini yükle. { barcode -> {col:val} }"""
        self.session.product_list = products
        self.session.column_map = column_map
        # alternatif barkod → birincil barkod eşlemesi (export normalizasyonu için)
        self._alt_to_primary: dict = alt_to_primary or {}
        self._persist()  # slim product_list'i diske kaydet

    def lookup(self, barcode: str) -> Optional[tuple[str, dict]]:
        """
        Barkodu ürün listesinde ara.
        Returns: (eşleşen_csv_barkodu, ürün_verisi) veya None
        Çoklu eşleşmede ("multi", count) döner.

        Arama sırası:
        1. Tam eşleşme
        2. CSV barkodu tarananla bitiyor  →  önek eksik (ZT62063 → ZEB-ZT62063)
        3. Taranan kod CSV barkodunun içinde  →  kısmi okuma
        """
        barcode = barcode.strip().upper()

        if barcode in self.session.product_list:
            return barcode, self.session.product_list[barcode]

        # Önek eksik durumu: csv_key.endswith(barcode)
        candidates = [
            (k, v) for k, v in self.session.product_list.items()
            if k.endswith(barcode)
        ]
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            return "multi", len(candidates)

        # Substring: taranan kod csv_key içinde geçiyor
        candidates = [
            (k, v) for k, v in self.session.product_list.items()
            if barcode in k
        ]
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            return "multi", len(candidates)

        return None

    # ── Sayım başlat / bitir ─────────────────────────────────────────────────

    def start_session(self):
        self.session.is_active = True
        self.session.started_at = _now()
        self.session.finished_at = None
        self.session.scans = {}
        self.session.history = []
        self._persist()

    def finish_session(self):
        self.session.is_active = False
        self.session.finished_at = _now()
        self._persist()

    # ── Barkod işlemleri (Lock altında çalışır) ───────────────────────────────

    async def add_scan(self, barcode: str, quantity: int, user: str) -> tuple[bool, str, Optional[ScanEntry]]:
        """
        Barkod ekle.
        Returns: (success, message, entry)
        """
        original = self.strip_barcode(barcode)

        result = self.lookup(original)
        if result is None:
            return False, "Barkod ürün listesinde bulunamadı.", None
        if result[0] == "multi":
            return False, f"Birden fazla ürün eşleşti ({result[1]} adet) — barkodu tam okutun.", None

        matched_barcode, product = result
        # Alternatif barkodla eşleştiyse birincil barkoda normalize et
        matched_barcode = self._alt_to_primary.get(matched_barcode, matched_barcode)
        name_col = self.session.column_map.get("name_col", "")
        product_name = product.get(name_col, "—")

        async with self._lock:
            entry = ScanEntry(
                id=_uid(),
                barcode=matched_barcode,
                quantity=quantity,
                user=user,
                timestamp=_now(),
                match_key=original,
                product_name=product_name,
                extra_fields=product,
            )

            if matched_barcode not in self.session.scans:
                self.session.scans[matched_barcode] = []
            self.session.scans[matched_barcode].append(entry.model_dump())

            hist = HistoryEntry(
                id=_uid(),
                action="add",
                user=user,
                barcode=matched_barcode,
                product_name=product_name,
                quantity=quantity,
                timestamp=entry.timestamp,
                related_scan_id=entry.id,
            )
            self.session.history.append(hist.model_dump())

        self._persist()
        return True, "OK", entry

    async def update_scan_qty(self, scan_id: str, new_qty: int, user: str) -> tuple[bool, str, Optional[dict]]:
        """Belirli bir scan kaydının adetini güncelle."""
        async with self._lock:
            for barcode, entries in self.session.scans.items():
                for e in entries:
                    if e["id"] == scan_id:
                        old_qty = e["quantity"]
                        e["quantity"] = new_qty
                        hist = HistoryEntry(
                            id=_uid(),
                            action="edit",
                            user=user,
                            barcode=barcode,
                            product_name=e["product_name"],
                            quantity=new_qty,
                            previous_quantity=old_qty,
                            timestamp=_now(),
                            related_scan_id=scan_id,
                        )
                        self.session.history.append(hist.model_dump())
                        self._persist()
                        return True, "OK", e
        return False, "Kayıt bulunamadı.", None

    async def bulk_delete(self, barcode: str, target_user: str, qty_to_delete: int, admin_user: str) -> tuple[bool, str, int]:
        """
        Belirli bir kullanıcının belirli bir barkodundaki kayıtlarından
        toplam qty_to_delete adet sil. En yeni kayıtlardan başlar.
        Returns: (success, message, actually_deleted_qty)
        """
        async with self._lock:
            entries = self.session.scans.get(barcode, [])
            user_entries = [e for e in entries if e["user"] == target_user]
            if not user_entries:
                return False, "Bu kullanıcıya ait kayıt bulunamadı.", 0

            user_total = sum(e["quantity"] for e in user_entries)
            if qty_to_delete > user_total:
                return False, f"Silinecek adet ({qty_to_delete}), mevcut adetten ({user_total}) fazla.", 0

            remaining = qty_to_delete
            deleted_qty = 0
            product_name = user_entries[0]["product_name"]

            # En yeni kayıtlardan başlayarak sil / azalt
            for e in reversed(user_entries):
                if remaining <= 0:
                    break
                if e["quantity"] <= remaining:
                    remaining -= e["quantity"]
                    deleted_qty += e["quantity"]
                    entries.remove(e)
                else:
                    e["quantity"] -= remaining
                    deleted_qty += remaining
                    remaining = 0

            if not entries:
                del self.session.scans[barcode]

            hist = HistoryEntry(
                id=_uid(),
                action="delete",
                user=admin_user,
                barcode=barcode,
                product_name=product_name,
                quantity=deleted_qty,
                timestamp=_now(),
                related_scan_id="bulk",
            )
            self.session.history.append(hist.model_dump())

        self._persist()
        return True, "OK", deleted_qty

    async def delete_scan(self, scan_id: str, user: str) -> tuple[bool, str, Optional[dict]]:
        """
        Belirli bir scan kaydını sil.
        Returns: (success, message, deleted_entry)
        """
        deleted = None
        async with self._lock:
            for barcode, entries in self.session.scans.items():
                for i, e in enumerate(entries):
                    if e["id"] == scan_id:
                        deleted = entries.pop(i)
                        if not entries:
                            del self.session.scans[barcode]

                        hist = HistoryEntry(
                            id=_uid(),
                            action="delete",
                            user=user,
                            barcode=barcode,
                            product_name=deleted["product_name"],
                            quantity=deleted["quantity"],
                            timestamp=_now(),
                            related_scan_id=scan_id,
                        )
                        self.session.history.append(hist.model_dump())
                        break
                if deleted:
                    break

        if deleted:
            self._persist()
            return True, "OK", deleted
        return False, "Kayıt bulunamadı.", None

    async def undo_history(self, history_id: str, user: str) -> tuple[bool, str]:
        """
        History'den bir aksiyonu geri al.
        - 'add' aksiyonu → ilgili scan'i sil
        - 'delete' aksiyonu → ilgili scan'i geri ekle
        """
        async with self._lock:
            target = next((h for h in self.session.history if h["id"] == history_id), None)
            if target is None:
                return False, "History kaydı bulunamadı."

            action = target["action"]
            scan_id = target["related_scan_id"]
            barcode = target["barcode"]

            if action == "add":
                # eklemeyi geri al → scan'i sil
                for entries in self.session.scans.values():
                    for i, e in enumerate(entries):
                        if e["id"] == scan_id:
                            entries.pop(i)
                            break

            elif action == "edit":
                # düzenlemeyi geri al → eski adete döndür
                prev_qty = target.get("previous_quantity")
                if prev_qty is not None:
                    for entries in self.session.scans.values():
                        for e in entries:
                            if e["id"] == scan_id:
                                e["quantity"] = prev_qty
                                break

            elif action == "delete":
                # silmeyi geri al → scan'i yeniden ekle
                result = self.lookup(barcode)
                name_col = self.session.column_map.get("name_col", "")
                if result and result[0] != "multi":
                    _, product = result
                    product_name = product.get(name_col, "—")
                else:
                    product = {}
                    product_name = target["product_name"]

                restored = ScanEntry(
                    id=scan_id,           # orijinal id'yi koru
                    barcode=barcode,
                    quantity=target["quantity"],
                    user=target["user"],
                    timestamp=target["timestamp"],
                    match_key=barcode,
                    product_name=product_name,
                    extra_fields=product or {},
                )
                if barcode not in self.session.scans:
                    self.session.scans[barcode] = []
                self.session.scans[barcode].append(restored.model_dump())

            # undo kaydını history'e ekle
            hist = HistoryEntry(
                id=_uid(),
                action="undo",
                user=user,
                barcode=barcode,
                product_name=target["product_name"],
                quantity=target["quantity"],
                timestamp=_now(),
                related_scan_id=scan_id,
            )
            self.session.history.append(hist.model_dump())

        self._persist()
        return True, "OK"

    # ── Özet / export ────────────────────────────────────────────────────────

    def get_summary(self) -> list[dict]:
        """
        Her ürün için toplam adet ve katkıda bulunan kullanıcıları döner.
        CSV export için kullanılır.
        """
        summary = []
        for barcode, entries in self.session.scans.items():
            total_qty = sum(e["quantity"] for e in entries)
            users = list({e["user"] for e in entries})
            first = entries[0]
            row = {
                "barcode": barcode,
                "product_name": first["product_name"],
                "total_quantity": total_qty,
                "scanned_by": ", ".join(users),
                **first.get("extra_fields", {}),
            }
            summary.append(row)
        return summary

    def get_full_state(self, for_admin: bool = False) -> dict:
        """WebSocket STATE mesajı için tam snapshot.
        Sayım aktif değilse terminal scans/history boş alır —
        bitmiş sayımın verisi terminalde görünmemeli.
        Admin her zaman tam veriyi alır.
        """
        active = self.session.is_active
        include_data = active or for_admin
        return {
            "type": WsMessageType.STATE,
            "session": {
                "is_active": active,
                "started_at": self.session.started_at,
                "finished_at": self.session.finished_at,
            },
            "scans":   self.session.scans if include_data else {},
            "history": self.session.history if include_data else [],
            "connected_users": list(self._connections.keys()),
            "column_map": self.session.column_map,
            "alt_to_primary": self._alt_to_primary,
        }

    # ── WebSocket bağlantı yönetimi ──────────────────────────────────────────

    def register_ws(self, user: str, ws):
        self._all_ws.add(ws)
        self._connections.setdefault(user, set()).add(ws)

    def unregister_ws(self, user: str, ws):
        self._all_ws.discard(ws)
        if user in self._connections:
            self._connections[user].discard(ws)
            if not self._connections[user]:
                del self._connections[user]

    def get_connected_users(self) -> list[str]:
        return list(self._connections.keys())

    async def broadcast(self, message: dict):
        """Tüm bağlı istemcilere mesaj gönder"""
        dead = set()
        for ws in list(self._all_ws):
            try:
                await ws.send_json(message)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self._all_ws.discard(ws)


# Singleton — main.py buradan import eder
app_state = AppState()