"""
main.py — FastAPI + WebSocket sunucusu
Çalıştırmak için:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""

import base64
import csv
import io
import json
import socket
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from models import WsMessageType, DeleteRequest, BulkDeleteRequest, UndoRequest
from state import app_state

app = FastAPI(title="Stok Sayım Sistemi", version="1.0.0")

# ── CORS — local ağda tüm cihazlara izin ver ─────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Static dosyalar (client/ klasörü) ────────────────────────────────────────
CLIENT_DIR = Path(__file__).parent.parent / "client"
app.mount("/static", StaticFiles(directory=str(CLIENT_DIR)), name="static")


# ─────────────────────────────────────────────────────────────────────────────
# SAYFA ROUTE'LARI
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_admin():
    """Ana makine arayüzü"""
    html_path = CLIENT_DIR / "admin.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/terminal", response_class=HTMLResponse)
async def serve_terminal():
    """Zebra terminal arayüzü"""
    html_path = CLIENT_DIR / "terminal.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


# ─────────────────────────────────────────────────────────────────────────────
# CSV YÜKLEME & KOLON EŞLEŞTİRME
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/upload-csv")
async def upload_csv(file: UploadFile = File(...)):
    """
    SAP'tan alınan ürün listesi CSV'sini yükle.
    Sadece kolon isimlerini döner — kullanıcı eşleştirmeyi kendisi yapar.
    """
    if not file.filename.endswith(".csv"):
        raise HTTPException(400, "Sadece .csv dosyası yüklenebilir.")

    content = await file.read()
    try:
        text = content.decode("utf-8-sig")  # BOM karakterini temizle
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    delimiter_setting = app_state.settings.get("delimiter", "auto")
    if delimiter_setting == "auto":
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=',;\t|')
            delimiter = dialect.delimiter
        except csv.Error:
            delimiter = ','
    else:
        delimiter = delimiter_setting

    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    columns = reader.fieldnames or []

    if not columns:
        raise HTTPException(400, "CSV dosyasında kolon bulunamadı.")

    # Ham satırları geçici olarak sakla (eşleştirme yapılana kadar)
    rows = list(reader)
    app_state._pending_csv_rows = rows

    return {
        "columns": list(columns),
        "row_count": len(rows),
        "preview": rows[:3],  # ilk 3 satır önizleme
    }


@app.post("/api/set-column-map")
async def set_column_map(body: dict):
    """
    Kullanıcının seçtiği kolon eşleştirmesini uygula ve ürün listesini yükle.
    body: { "barcode_col": "ST Kodu", "name_col": "Malzeme Adı" }
    """
    barcode_col = body.get("barcode_col", "").strip()
    name_col = body.get("name_col", "").strip()

    if not barcode_col:
        raise HTTPException(400, "Barkod kolonu seçilmeli.")
    if not name_col:
        raise HTTPException(400, "Ürün adı kolonu seçilmeli.")

    alt_barcode_col = body.get("alt_barcode_col", "").strip()  # opsiyonel

    rows = getattr(app_state, "_pending_csv_rows", None)
    if rows is None:
        raise HTTPException(400, "Önce CSV yükleyin.")

    # { BARCODE_VALUE -> tüm kolon dict } şeklinde index oluştur
    # Hem birincil hem alternatif barkod kolonu varsa ikisi de eklenir
    products = {}
    alt_to_primary = {}   # { alt_barcode -> primary_barcode }
    skipped = 0
    for row in rows:
        barcode_val = row.get(barcode_col, "").strip().upper()
        if not barcode_val:
            skipped += 1
            continue
        product_data = dict(row)
        products[barcode_val] = product_data

        # Alternatif barkod kolonu seçildiyse onu da ayrı key olarak ekle
        if alt_barcode_col:
            alt_val = row.get(alt_barcode_col, "").strip().upper()
            if alt_val and alt_val != barcode_val:
                products[alt_val] = product_data
                alt_to_primary[alt_val] = barcode_val  # normalizasyon için

    column_map = {
        "barcode_col": barcode_col,
        "name_col": name_col,
        "alt_barcode_col": alt_barcode_col,
    }
    app_state.load_product_list(products, column_map, alt_to_primary)

    # Geçici veriyi temizle
    app_state._pending_csv_rows = None

    alt_loaded = sum(
        1 for row in rows
        if alt_barcode_col and row.get(alt_barcode_col, "").strip()
    ) if alt_barcode_col else 0

    # Terminallere ürün listesinin güncellendiğini bildir (scans/history sıfırlanmış halde)
    state_msg = app_state.get_full_state()
    state_msg["scans"] = {}
    state_msg["history"] = []
    await app_state.broadcast(state_msg)

    return {
        "loaded": len(products),
        "skipped": skipped,
        "column_map": column_map,
        "alt_loaded": alt_loaded,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SUNUCU BİLGİSİ
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/server-info")
async def server_info():
    """Sunucunun yerel ağ IP'sini, terminal URL'ini ve QR kodunu döner."""
    import qrcode
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = "127.0.0.1"

    url = f"http://{ip}:8000/terminal"

    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode()

    return {
        "ip": ip,
        "terminal_url": url,
        "qr_png": qr_b64,
    }


# ─────────────────────────────────────────────────────────────────────────────
# AYARLAR
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings():
    return app_state.settings


@app.post("/api/settings")
async def save_settings(body: dict):
    """
    Kalıcı uygulama ayarlarını güncelle.
    body: { "delimiter": "auto"|";"|","|"\\t"|"|", "barcode_rules": [...] }
    """
    delimiter = body.get("delimiter", "auto")
    if delimiter not in ("auto", ";", ",", "\t", "|"):
        raise HTTPException(400, "Geçersiz delimiter.")

    rules = body.get("barcode_rules", [])
    for r in rules:
        if r.get("type") not in ("prefix", "suffix"):
            raise HTTPException(400, f"Geçersiz kural tipi: {r.get('type')}")
        if not str(r.get("value", "")).strip():
            raise HTTPException(400, "Kural değeri boş olamaz.")

    new_settings = {"delimiter": delimiter, "barcode_rules": rules}
    app_state.save_settings(new_settings)
    await app_state.broadcast({
        "type": "settings_update",
        "settings": new_settings,
    })
    return {"status": "ok", "settings": new_settings}


# ─────────────────────────────────────────────────────────────────────────────
# ÜRÜN LİSTESİ (terminal fuzzy matching için — sadece barkod+isim, tam CSV değil)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/products")
async def get_products():
    """
    Terminal'in ilk bağlanışında bir kez çektiği hafif ürün listesi.
    { barcode -> name } formatında döner — tam CSV satırlarını değil.
    """
    if not app_state.session.product_list:
        return {"products": {}, "loaded": False}

    name_col = app_state.session.column_map.get("name_col", "")
    products = {
        bc: data.get(name_col, "—")
        for bc, data in app_state.session.product_list.items()
    }
    return {"products": products, "loaded": True}


# ─────────────────────────────────────────────────────────────────────────────
# OTURUM YÖNETİMİ
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/session/start")
async def start_session():
    """Sayımı başlat"""
    if not app_state.session.product_list:
        raise HTTPException(400, "Önce ürün listesi CSV'si yüklenmelidir.")
    if app_state.session.is_active:
        raise HTTPException(400, "Zaten aktif bir sayım var.")

    app_state.start_session()

    await app_state.broadcast({
        "type": WsMessageType.SESSION,
        "is_active": True,
        "started_at": app_state.session.started_at,
    })
    return {"status": "started", "started_at": app_state.session.started_at}


@app.post("/api/session/finish")
async def finish_session():
    """Sayımı bitir"""
    if not app_state.session.is_active:
        raise HTTPException(400, "Aktif sayım yok.")

    app_state.finish_session()

    await app_state.broadcast({
        "type": WsMessageType.SESSION,
        "is_active": False,
        "finished_at": app_state.session.finished_at,
    })
    return {"status": "finished", "finished_at": app_state.session.finished_at}


@app.get("/api/session/status")
async def session_status():
    """Oturum durumunu döner"""
    return {
        "is_active": app_state.session.is_active,
        "started_at": app_state.session.started_at,
        "finished_at": app_state.session.finished_at,
        "product_count": len(app_state.session.product_list),
        "scan_count": sum(len(v) for v in app_state.session.scans.values()),
        "connected_users": app_state.get_connected_users(),
    }


@app.patch("/api/scan/{scan_id}")
async def update_scan(scan_id: str, body: dict):
    """Barkod kaydının adetini güncelle"""
    if not app_state.session.is_active:
        raise HTTPException(400, "Sayım aktif değil.")

    new_qty = int(body.get("quantity", 0))
    if new_qty < 1:
        raise HTTPException(400, "Adet en az 1 olmalı.")

    user = body.get("user", "terminal")
    success, message, updated = await app_state.update_scan_qty(scan_id, new_qty, user)

    if not success:
        raise HTTPException(404, message)

    await app_state.broadcast({
        "type": WsMessageType.SCAN_UPD,
        "scan_id": scan_id,
        "barcode": updated["barcode"],
        "new_qty": new_qty,
        "total_for_barcode": sum(
            e["quantity"]
            for e in app_state.session.scans.get(updated["barcode"], [])
        ),
    })
    await app_state.broadcast({
        "type": WsMessageType.HISTORY,
        "history": app_state.session.history[-1],
    })

    return {"status": "ok", "updated": updated}


@app.delete("/api/scan/{scan_id}")
async def delete_scan(scan_id: str, body: DeleteRequest):
    """Barkod kaydını sil"""
    if not app_state.session.is_active:
        raise HTTPException(400, "Sayım aktif değil.")

    success, message, deleted = await app_state.delete_scan(
        scan_id=scan_id,
        user=body.user,
    )

    if not success:
        raise HTTPException(404, message)

    await app_state.broadcast({
        "type": WsMessageType.SCAN_DEL,
        "scan_id": scan_id,
        "barcode": deleted["barcode"],
        "remaining_total": sum(
            e["quantity"]
            for e in app_state.session.scans.get(deleted["barcode"], [])
        ),
    })

    await app_state.broadcast({
        "type": WsMessageType.HISTORY,
        "history": app_state.session.history[-1],
    })

    return {"status": "ok", "deleted": deleted}


@app.post("/api/scan/bulk-delete")
async def bulk_delete_scan(req: BulkDeleteRequest):
    """Admin: belirli kullanıcının belirli barkodundan toplu adet sil"""
    if req.qty_to_delete < 1:
        raise HTTPException(400, "Silinecek adet en az 1 olmalı.")

    success, message, deleted_qty = await app_state.bulk_delete(
        barcode=req.barcode,
        target_user=req.target_user,
        qty_to_delete=req.qty_to_delete,
        admin_user=req.admin_user,
    )
    if not success:
        raise HTTPException(400, message)

    # Tam state broadcast et — scans ve history birlikte güncellenir
    await app_state.broadcast(app_state.get_full_state())

    return {"status": "ok", "deleted_qty": deleted_qty}


@app.post("/api/undo")
async def undo_history(req: UndoRequest):
    """History'den geri al"""
    success, message = await app_state.undo_history(
        history_id=req.history_id,
        user=req.user,
    )

    if not success:
        raise HTTPException(404, message)

    # Tam state'i broadcast et — undo sonrası tüm istemciler güncellensin
    full_state = app_state.get_full_state()
    await app_state.broadcast(full_state)

    return {"status": "ok"}


# ─────────────────────────────────────────────────────────────────────────────
# CSV EXPORT
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/export")
async def export_csv():
    """Sayım sonucunu CSV olarak indir"""
    summary = app_state.get_summary()

    if not summary:
        raise HTTPException(404, "Henüz sayım verisi yok.")

    output = io.StringIO()

    # Dinamik kolon başlıkları — extra_fields'dan gelir
    base_cols = ["barcode", "product_name", "total_quantity", "scanned_by"]
    extra_cols = [k for k in summary[0].keys() if k not in base_cols]
    fieldnames = base_cols + extra_cols

    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(summary)

    output.seek(0)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"sayim_{timestamp}.csv"

    return StreamingResponse(
        iter([output.getvalue().encode("utf-8-sig")]),  # BOM ile — Excel uyumlu
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ─────────────────────────────────────────────────────────────────────────────
# WEBSOCKET
# ─────────────────────────────────────────────────────────────────────────────

@app.websocket("/ws/{user}")
async def websocket_endpoint(websocket: WebSocket, user: str):
    """
    Her terminal ve admin bu endpoint'e bağlanır.
    URL: ws://<server-ip>:8000/ws/<kullanici-adi>
    """
    await websocket.accept()
    app_state.register_ws(user, websocket)

    # Bağlanan kullanıcıya tam mevcut state'i gönder
    await websocket.send_json(app_state.get_full_state(for_admin=(user == "admin")))

    # Diğer herkese yeni kullanıcı bildirimi
    await app_state.broadcast({
        "type": WsMessageType.USERS,
        "connected_users": app_state.get_connected_users(),
    })

    try:
        while True:
            # Terminal'den gelen mesajları dinle
            data = await websocket.receive_json()
            msg_type = data.get("type")

            if msg_type == WsMessageType.SCAN:
                # { type: "scan", barcode: "...", quantity: 5, user: "Ali" }
                if not app_state.session.is_active:
                    await websocket.send_json({
                        "type": WsMessageType.SCAN_ERR,
                        "message": "Sayım aktif değil.",
                    })
                    continue

                success, message, entry = await app_state.add_scan(
                    barcode=data.get("barcode", ""),
                    quantity=int(data.get("quantity", 1)),
                    user=data.get("user", user),
                )

                if not success:
                    await websocket.send_json({
                        "type": WsMessageType.SCAN_ERR,
                        "message": message,
                    })
                else:
                    # Gönderen terminale onay
                    await websocket.send_json({
                        "type": WsMessageType.SCAN_OK,
                        "entry": entry.model_dump(),
                    })

                    # Herkese yeni kayıt bildir
                    await app_state.broadcast({
                        "type": WsMessageType.SCAN_NEW,
                        "entry": entry.model_dump(),
                        "total_for_barcode": sum(
                            e["quantity"]
                            for e in app_state.session.scans.get(entry.barcode, [])
                        ),
                    })
                    await app_state.broadcast({
                        "type": WsMessageType.HISTORY,
                        "history": app_state.session.history[-1],
                    })

            elif msg_type == WsMessageType.DELETE:
                # { type: "delete", scan_id: "...", user: "Ali" }
                success, message, deleted = await app_state.delete_scan(
                    scan_id=data.get("scan_id", ""),
                    user=data.get("user", user),
                )

                if not success:
                    await websocket.send_json({
                        "type": WsMessageType.SCAN_ERR,
                        "message": message,
                    })
                else:
                    await app_state.broadcast({
                        "type": WsMessageType.SCAN_DEL,
                        "scan_id": data.get("scan_id"),
                        "barcode": deleted["barcode"],
                        "remaining_total": sum(
                            e["quantity"]
                            for e in app_state.session.scans.get(deleted["barcode"], [])
                        ),
                    })
                    await app_state.broadcast({
                        "type": WsMessageType.HISTORY,
                        "history": app_state.session.history[-1],
                    })

    except WebSocketDisconnect:
        app_state.unregister_ws(user, websocket)
        await app_state.broadcast({
            "type": WsMessageType.USERS,
            "connected_users": app_state.get_connected_users(),
        })