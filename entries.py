"""
CRUD für Tagebucheinträge. Nur Eltern dürfen schreiben/ändern/löschen.
Lesen ist für alle eingeloggten Nutzer offen (auch Gäste) -- Gäste sehen
zwar keine ai_generations standardmäßig, aber rohe Tagebucheinträge sind in
diesem Konzept ohnehin nie für Gäste vorgesehen; daher schützen wir GET hier
zusätzlich auf Eltern-Rollen, damit Gäste nicht versehentlich über die API
private Einträge lesen können (auch wenn das Frontend es ihnen nie anzeigen würde).
"""
from flask import Blueprint, request, jsonify, session

from db import supabase
from auth import parent_required
from images import _get_signed_url

entries_bp = Blueprint("entries", __name__)


def _attach_image_previews(entries: list[dict]) -> list[dict]:
    """Holt zu jedem Entry die zugehörigen Bilder (für Vorschau in der Liste)
    und die Anzahl der Kommentare (für das Badge in der Listenkarte)."""
    if not entries:
        return entries

    entry_ids = [e["id"] for e in entries]
    images_result = (
        supabase.table("images")
        .select("*")
        .in_("entry_id", entry_ids)
        .order("position")
        .execute()
    )
    images_by_entry: dict[str, list[dict]] = {}
    for img in images_result.data:
        img["url"] = _get_signed_url(img["storage_key"])
        images_by_entry.setdefault(img["entry_id"], []).append(img)

    comments_result = (
        supabase.table("comments").select("entry_id").in_("entry_id", entry_ids).execute()
    )
    comment_counts: dict[str, int] = {}
    for c in comments_result.data:
        comment_counts[c["entry_id"]] = comment_counts.get(c["entry_id"], 0) + 1

    for entry in entries:
        entry["images"] = images_by_entry.get(entry["id"], [])
        entry["comment_count"] = comment_counts.get(entry["id"], 0)
    return entries


@entries_bp.route("/api/entries", methods=["GET"])
@parent_required
def list_entries():
    date_from = request.args.get("from")
    date_to = request.args.get("to")

    query = supabase.table("entries").select("*").order("entry_date", desc=True)
    if date_from:
        query = query.gte("entry_date", date_from)
    if date_to:
        query = query.lte("entry_date", date_to)

    result = query.execute()
    entries = _attach_image_previews(result.data)
    return jsonify(entries)


@entries_bp.route("/api/entries/<entry_id>", methods=["GET"])
@parent_required
def get_entry(entry_id):
    result = supabase.table("entries").select("*").eq("id", entry_id).execute()
    if not result.data:
        return jsonify({"error": "Eintrag nicht gefunden."}), 404

    entry = result.data[0]
    images_result = (
        supabase.table("images")
        .select("*")
        .eq("entry_id", entry_id)
        .order("position")
        .execute()
    )
    for img in images_result.data:
        img["url"] = _get_signed_url(img["storage_key"])
    entry["images"] = images_result.data
    return jsonify(entry)


@entries_bp.route("/api/entries", methods=["POST"])
@parent_required
def create_entry():
    data = request.get_json(silent=True) or {}
    entry_date = data.get("entry_date")
    title = data.get("title", "").strip()
    text_content = data.get("text_content", "").strip()
    mood = data.get("mood")

    if not entry_date or not title or not text_content:
        return jsonify({"error": "entry_date, title und text_content sind Pflichtfelder."}), 400

    result = (
        supabase.table("entries")
        .insert(
            {
                "author_id": session["user_id"],
                "entry_date": entry_date,
                "title": title,
                "text_content": text_content,
                "mood": mood,
            }
        )
        .execute()
    )
    return jsonify(result.data[0]), 201


@entries_bp.route("/api/entries/<entry_id>", methods=["PUT"])
@parent_required
def update_entry(entry_id):
    data = request.get_json(silent=True) or {}

    existing = supabase.table("entries").select("id").eq("id", entry_id).execute()
    if not existing.data:
        return jsonify({"error": "Eintrag nicht gefunden."}), 404

    update_fields = {}
    for field in ("entry_date", "title", "text_content", "mood", "is_published_for_guests"):
        if field in data:
            update_fields[field] = data[field]

    if not update_fields:
        return jsonify({"error": "Keine Änderungen übergeben."}), 400

    from datetime import datetime, timezone
    update_fields["updated_at"] = datetime.now(timezone.utc).isoformat()

    result = (
        supabase.table("entries").update(update_fields).eq("id", entry_id).execute()
    )
    return jsonify(result.data[0])


@entries_bp.route("/api/entries/<entry_id>", methods=["DELETE"])
@parent_required
def delete_entry(entry_id):
    existing = supabase.table("entries").select("id").eq("id", entry_id).execute()
    if not existing.data:
        return jsonify({"error": "Eintrag nicht gefunden."}), 404

    # Zugehörige Bilder zuerst aus dem Storage entfernen, dann DB-Eintrag
    # (Cascade löscht die DB-Zeilen, aber nicht die Storage-Objekte selbst)
    images_result = supabase.table("images").select("storage_key").eq("entry_id", entry_id).execute()
    storage_keys = [img["storage_key"] for img in images_result.data]
    if storage_keys:
        supabase.storage.from_("logbuch-images").remove(storage_keys)

    supabase.table("entries").delete().eq("id", entry_id).execute()
    return jsonify({"ok": True})
