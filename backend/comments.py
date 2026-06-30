"""
Kommentarfunktion für Tagebucheinträge.

Konzept (abweichend von der ursprünglichen Idee, KI-Geschichten zu
kommentieren): Eltern geben einzelne Tagebucheinträge gezielt für Gäste frei
(is_published_for_guests). Nur freigegebene Einträge sind für Gäste lesbar
und kommentierbar. Kommentare sind für alle sichtbar (auch andere Gäste).
"""
from flask import Blueprint, request, jsonify, session

from db import supabase
from auth import login_required, parent_required

comments_bp = Blueprint("comments", __name__)


def _is_parent() -> bool:
    return session.get("role") in ("parent_father", "parent_mother")


@comments_bp.route("/api/entries/<entry_id>/publish", methods=["PUT"])
@parent_required
def set_entry_published(entry_id):
    """Eltern schalten die Gast-Sichtbarkeit eines Eintrags an/aus."""
    data = request.get_json(silent=True) or {}
    published = bool(data.get("is_published_for_guests", False))

    existing = supabase.table("entries").select("id").eq("id", entry_id).execute()
    if not existing.data:
        return jsonify({"error": "Eintrag nicht gefunden."}), 404

    result = (
        supabase.table("entries")
        .update({"is_published_for_guests": published})
        .eq("id", entry_id)
        .execute()
    )
    return jsonify(result.data[0])


@comments_bp.route("/api/guest/entries", methods=["GET"])
@login_required
def list_published_entries_for_guests():
    """Liefert nur freigegebene Einträge -- zugänglich für Gäste UND Eltern
    (Eltern sehen so dieselbe Ansicht, die Gäste auch bekommen, falls nötig)."""
    result = (
        supabase.table("entries")
        .select("*")
        .eq("is_published_for_guests", True)
        .order("entry_date", desc=True)
        .execute()
    )
    entries = result.data

    if not entries:
        return jsonify(entries)

    from images import _get_signed_url

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
        supabase.table("comments")
        .select("*, users(display_name)")
        .in_("entry_id", entry_ids)
        .order("created_at")
        .execute()
    )
    comments_by_entry: dict[str, list[dict]] = {}
    for c in comments_result.data:
        c["author_name"] = c.get("users", {}).get("display_name", "Unbekannt")
        c.pop("users", None)
        comments_by_entry.setdefault(c["entry_id"], []).append(c)

    for entry in entries:
        entry["images"] = images_by_entry.get(entry["id"], [])
        entry["comments"] = comments_by_entry.get(entry["id"], [])

    return jsonify(entries)


@comments_bp.route("/api/entries/<entry_id>/comments", methods=["GET"])
@login_required
def list_comments(entry_id):
    """
    Kommentare zu einem Eintrag. Eltern dürfen immer lesen (für die
    Editor-Ansicht). Gäste nur, wenn der Eintrag freigegeben ist.
    """
    if not _is_parent():
        entry = supabase.table("entries").select("is_published_for_guests").eq("id", entry_id).execute()
        if not entry.data or not entry.data[0]["is_published_for_guests"]:
            return jsonify({"error": "Eintrag nicht verfügbar."}), 403

    result = (
        supabase.table("comments")
        .select("*, users(display_name)")
        .eq("entry_id", entry_id)
        .order("created_at")
        .execute()
    )
    comments = result.data
    for c in comments:
        c["author_name"] = c.get("users", {}).get("display_name", "Unbekannt")
        c.pop("users", None)
    return jsonify(comments)


@comments_bp.route("/api/entries/<entry_id>/comments", methods=["POST"])
@login_required
def create_comment(entry_id):
    """Eltern können immer kommentieren; Gäste nur bei freigegebenen Einträgen."""
    if not _is_parent():
        entry = supabase.table("entries").select("is_published_for_guests").eq("id", entry_id).execute()
        if not entry.data or not entry.data[0]["is_published_for_guests"]:
            return jsonify({"error": "Eintrag nicht verfügbar."}), 403

    data = request.get_json(silent=True) or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "Kommentar darf nicht leer sein."}), 400
    if len(content) > 1000:
        return jsonify({"error": "Kommentar ist zu lang (max. 1000 Zeichen)."}), 400

    entry_check = supabase.table("entries").select("id").eq("id", entry_id).execute()
    if not entry_check.data:
        return jsonify({"error": "Eintrag nicht gefunden."}), 404

    result = (
        supabase.table("comments")
        .insert(
            {
                "entry_id": entry_id,
                "author_id": session["user_id"],
                "content": content,
            }
        )
        .execute()
    )
    comment = result.data[0]
    comment["author_name"] = session.get("display_name", "Unbekannt")
    return jsonify(comment), 201


@comments_bp.route("/api/comments/<comment_id>", methods=["DELETE"])
@login_required
def delete_comment(comment_id):
    """Eltern dürfen jeden Kommentar löschen (Moderation). Gäste dürfen nur
    ihre eigenen Kommentare löschen."""
    existing = (
        supabase.table("comments").select("author_id").eq("id", comment_id).execute()
    )
    if not existing.data:
        return jsonify({"error": "Kommentar nicht gefunden."}), 404

    if not _is_parent() and existing.data[0]["author_id"] != session["user_id"]:
        return jsonify({"error": "Du darfst nur eigene Kommentare löschen."}), 403

    supabase.table("comments").delete().eq("id", comment_id).execute()
    return jsonify({"ok": True})
