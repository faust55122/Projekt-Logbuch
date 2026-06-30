"""
Bild-Upload/-Verwaltung. Bilder können an einen Eintrag gebunden sein
(entry_id gesetzt) oder frei hinterlegt werden (entry_id = None) -- z.B. für
eine allgemeine Foto-Galerie unabhängig von einzelnen Tagebucheinträgen.

Upload läuft über das Backend (nie direkt vom Frontend zu Supabase Storage),
damit der service_role Key serverseitig bleibt.
"""
import uuid
from pathlib import Path

from flask import Blueprint, request, jsonify, session

from db import supabase
from auth import parent_required, login_required

images_bp = Blueprint("images", __name__)

STORAGE_BUCKET = "logbuch-images"
MAX_IMAGE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB, wie im Konzept festgelegt
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _get_signed_url(storage_key: str, expires_in: int = 86400) -> str:
    """Erzeugt eine zeitlich befristete signierte URL (Bucket ist privat).
    24h Gültigkeit -- lang genug für eine Sitzung, das Frontend fragt bei
    Bedarf (z.B. beim Öffnen der Lightbox) ohnehin frische URLs an."""
    result = supabase.storage.from_(STORAGE_BUCKET).create_signed_url(
        storage_key, expires_in
    )
    return result.get("signedURL") or result.get("signedUrl", "")


@images_bp.route("/api/images", methods=["POST"])
@parent_required
def upload_image():
    if "file" not in request.files:
        return jsonify({"error": "Keine Datei übergeben (Feld 'file')."}), 400

    file = request.files["file"]
    entry_id = request.form.get("entry_id") or None  # leer/None = freies Bild
    caption = (request.form.get("caption") or "").strip()[:60] or None
    image_date = request.form.get("image_date") or None  # nur relevant bei freien Bildern

    original_name = file.filename or ""
    extension = Path(original_name).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        return jsonify({"error": f"Dateityp {extension} nicht erlaubt."}), 400

    file_bytes = file.read()
    if len(file_bytes) > MAX_IMAGE_SIZE_BYTES:
        return jsonify({"error": "Bild ist größer als 10 MB."}), 400

    # Falls entry_id übergeben wurde, prüfen ob der Eintrag existiert
    if entry_id:
        existing = supabase.table("entries").select("id").eq("id", entry_id).execute()
        if not existing.data:
            return jsonify({"error": "Eintrag für entry_id nicht gefunden."}), 404

    storage_key = f"{uuid.uuid4()}{extension}"

    supabase.storage.from_(STORAGE_BUCKET).upload(
        storage_key,
        file_bytes,
        file_options={"content-type": file.mimetype or "application/octet-stream"},
    )

    # Position = aktuelle Anzahl Bilder im selben Kontext (Eintrag oder frei)
    if entry_id:
        position_query = supabase.table("images").select("id").eq("entry_id", entry_id).execute()
    else:
        position_query = supabase.table("images").select("id").is_("entry_id", "null").execute()
    position = len(position_query.data)

    result = (
        supabase.table("images")
        .insert(
            {
                "entry_id": entry_id,
                "storage_key": storage_key,
                "caption": caption,
                "image_date": image_date,
                "position": position,
                "uploaded_by": session["user_id"],
            }
        )
        .execute()
    )

    image_row = result.data[0]
    image_row["url"] = _get_signed_url(storage_key)
    return jsonify(image_row), 201


@images_bp.route("/api/images", methods=["GET"])
@login_required
def list_images():
    """
    Galerie-Ansicht, neueste zuerst.
    ?free=true       -> nur frei hinterlegte Bilder (entry_id ist NULL)
    ?from=YYYY-MM-DD -> nur Bilder mit effective_date >= from
    ?to=YYYY-MM-DD   -> nur Bilder mit effective_date <= to
    Ohne from/to: Default = letzte 12 Monate (spart Ladezeit bei vielen Bildern).
    ?all=true        -> überschreibt den 12-Monats-Default, lädt alles.

    effective_date: image_date falls gesetzt, sonst das entry_date des
    verknüpften Eintrags, sonst created_at-Datum als letzter Fallback.

    Gäste sehen freie Bilder UND Bilder, die zu freigegebenen Einträgen
    gehören -- aber keine Bilder von privaten (nicht freigegebenen) Einträgen.
    """
    is_parent = session.get("role") in ("parent_father", "parent_mother")
    free_only = request.args.get("free") == "true"
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    load_all = request.args.get("all") == "true"

    if not date_from and not date_to and not load_all:
        from datetime import date, timedelta
        date_from = (date.today() - timedelta(days=365)).isoformat()

    query = supabase.table("images").select("*").order("created_at", desc=True)
    if free_only:
        query = query.is_("entry_id", "null")
    result = query.execute()
    images = result.data

    # Entry-Daten nachladen, um effective_date zu bestimmen (nur falls nötig)
    entry_ids = list({img["entry_id"] for img in images if img.get("entry_id")})
    entry_dates: dict[str, str] = {}
    published_entry_ids: set[str] = set()
    if entry_ids:
        entries_result = (
            supabase.table("entries")
            .select("id, entry_date, is_published_for_guests")
            .in_("id", entry_ids)
            .execute()
        )
        entry_dates = {e["id"]: e["entry_date"] for e in entries_result.data}
        published_entry_ids = {
            e["id"] for e in entries_result.data if e.get("is_published_for_guests")
        }

    if not is_parent:
        # Gäste: nur freie Bilder (entry_id None) oder Bilder von freigegebenen Einträgen
        images = [
            img for img in images
            if not img.get("entry_id") or img["entry_id"] in published_entry_ids
        ]

    for img in images:
        img["url"] = _get_signed_url(img["storage_key"])
        if img.get("image_date"):
            img["effective_date"] = img["image_date"]
        elif img.get("entry_id") and img["entry_id"] in entry_dates:
            img["effective_date"] = entry_dates[img["entry_id"]]
        else:
            img["effective_date"] = img["created_at"][:10]

    if date_from:
        images = [img for img in images if img["effective_date"] >= date_from]
    if date_to:
        images = [img for img in images if img["effective_date"] <= date_to]

    images.sort(key=lambda img: img["effective_date"], reverse=True)

    return jsonify(images)


@images_bp.route("/api/images/quarters", methods=["GET"])
@login_required
def list_image_quarters():
    """
    Gibt alle Quartale zurück, in denen mindestens ein Bild existiert
    (basierend auf effective_date), neueste zuerst. Für die
    Quartals-Navigationsleiste in der Galerie. Berücksichtigt für Gäste
    dieselbe Sichtbarkeitsregel wie list_images.
    Format: [{ "year": 2026, "quarter": 2, "label": "2026 Q2", "count": 5 }]
    """
    is_parent = session.get("role") in ("parent_father", "parent_mother")
    result = supabase.table("images").select("*").execute()
    images = result.data

    entry_ids = list({img["entry_id"] for img in images if img.get("entry_id")})
    entry_dates: dict[str, str] = {}
    published_entry_ids: set[str] = set()
    if entry_ids:
        entries_result = (
            supabase.table("entries")
            .select("id, entry_date, is_published_for_guests")
            .in_("id", entry_ids)
            .execute()
        )
        entry_dates = {e["id"]: e["entry_date"] for e in entries_result.data}
        published_entry_ids = {
            e["id"] for e in entries_result.data if e.get("is_published_for_guests")
        }

    if not is_parent:
        images = [
            img for img in images
            if not img.get("entry_id") or img["entry_id"] in published_entry_ids
        ]

    counts: dict[tuple, int] = {}
    for img in images:
        if img.get("image_date"):
            eff_date = img["image_date"]
        elif img.get("entry_id") and img["entry_id"] in entry_dates:
            eff_date = entry_dates[img["entry_id"]]
        else:
            eff_date = img["created_at"][:10]

        year = int(eff_date[:4])
        month = int(eff_date[5:7])
        quarter = (month - 1) // 3 + 1
        key = (year, quarter)
        counts[key] = counts.get(key, 0) + 1

    quarters = [
        {"year": y, "quarter": q, "label": f"{y} Q{q}", "count": c}
        for (y, q), c in counts.items()
    ]
    quarters.sort(key=lambda x: (x["year"], x["quarter"]), reverse=True)

    return jsonify(quarters)


@images_bp.route("/api/images/<image_id>", methods=["PUT"])
@parent_required
def update_image(image_id):
    """Erlaubt Eltern, Datum und/oder Beschriftung eines Bildes nachträglich
    zu ändern. image_date kann auch für an Einträge gebundene Bilder gesetzt
    werden -- überschreibt dann effective_date (Spezialfall, z.B. Foto wurde
    an einem anderen Tag aufgenommen als der Eintrag verfasst wurde)."""
    existing = supabase.table("images").select("id").eq("id", image_id).execute()
    if not existing.data:
        return jsonify({"error": "Bild nicht gefunden."}), 404

    data = request.get_json(silent=True) or {}
    update_fields = {}
    if "image_date" in data:
        update_fields["image_date"] = data["image_date"] or None
    if "caption" in data:
        update_fields["caption"] = (data["caption"] or "").strip()[:60] or None

    if not update_fields:
        return jsonify({"error": "Keine Änderungen übergeben."}), 400

    result = (
        supabase.table("images").update(update_fields).eq("id", image_id).execute()
    )
    image_row = result.data[0]
    image_row["url"] = _get_signed_url(image_row["storage_key"])
    return jsonify(image_row)


@images_bp.route("/api/images/<image_id>", methods=["DELETE"])
@parent_required
def delete_image(image_id):
    existing = supabase.table("images").select("storage_key").eq("id", image_id).execute()
    if not existing.data:
        return jsonify({"error": "Bild nicht gefunden."}), 404

    storage_key = existing.data[0]["storage_key"]
    supabase.storage.from_(STORAGE_BUCKET).remove([storage_key])
    supabase.table("images").delete().eq("id", image_id).execute()

    return jsonify({"ok": True})
