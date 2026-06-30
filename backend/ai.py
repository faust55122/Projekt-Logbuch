"""
KI-Generierung von Zusammenfassungen/Geschichten über einen Zeitraum.

Zweistufiger Ablauf (wie gewünscht):
1. POST /api/ai/draft-prompt   -> sammelt Daten im Zeitraum, baut Prompt-Entwurf,
                                   gibt ihn zurück OHNE die KI aufzurufen.
2. POST /api/ai/generate       -> nimmt den (ggf. vom Nutzer editierten) Prompt
                                   entgegen und ruft die Claude API auf.

Kontext für den Prompt: Tagebuchtexte + Bild-Beschriftungen + Mutter-Maße
im gewählten Zeitraum (Maße werden weggelassen, falls keine vorhanden sind --
robust auch bevor das Measurements-Feature vollständig genutzt wird).
"""
import os
from datetime import date

from flask import Blueprint, request, jsonify, session
from anthropic import Anthropic

from db import supabase
from auth import parent_required

ai_bp = Blueprint("ai", __name__)

ANTHROPIC_MODEL = "claude-sonnet-4-6"
_anthropic_client = None


def _get_anthropic_client() -> Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _anthropic_client


def _gather_period_data(period_start: str, period_end: str) -> dict:
    """Sammelt alle relevanten Daten (Texte, Bild-Captions, Maße) im Zeitraum."""
    entries_result = (
        supabase.table("entries")
        .select("entry_date, title, text_content")
        .gte("entry_date", period_start)
        .lte("entry_date", period_end)
        .order("entry_date")
        .execute()
    )
    entries = entries_result.data

    entry_ids_result = (
        supabase.table("entries")
        .select("id")
        .gte("entry_date", period_start)
        .lte("entry_date", period_end)
        .execute()
    )
    entry_ids = [e["id"] for e in entry_ids_result.data]

    captions = []
    if entry_ids:
        images_result = (
            supabase.table("images")
            .select("caption, entry_id")
            .in_("entry_id", entry_ids)
            .execute()
        )
        captions = [img["caption"] for img in images_result.data if img.get("caption")]

    measurements_result = (
        supabase.table("measurements")
        .select("measurement_date, weight_kg, waist_cm, pregnancy_week, note")
        .gte("measurement_date", period_start)
        .lte("measurement_date", period_end)
        .order("measurement_date")
        .execute()
    )
    measurements = measurements_result.data

    return {"entries": entries, "captions": captions, "measurements": measurements}


def _build_prompt(period_start: str, period_end: str, gen_type: str, data: dict) -> str:
    """Baut einen Prompt-Entwurf aus den gesammelten Daten."""
    if not data["entries"]:
        return (
            f"Hinweis: Für den Zeitraum {period_start} bis {period_end} wurden "
            f"keine Tagebucheinträge gefunden. Bitte wähle einen anderen Zeitraum "
            f"oder lege zuerst Einträge an."
        )

    entries_text = "\n\n".join(
        f"[{e['entry_date']}] {e['title']}\n{e['text_content']}" for e in data["entries"]
    )

    captions_block = ""
    if data["captions"]:
        captions_text = "\n".join(f"- {c}" for c in data["captions"])
        captions_block = f"\n\nBildunterschriften aus diesem Zeitraum:\n{captions_text}"

    measurements_block = ""
    if data["measurements"]:
        lines = []
        for m in data["measurements"]:
            parts = [m["measurement_date"]]
            if m.get("weight_kg"):
                parts.append(f"Gewicht: {m['weight_kg']} kg")
            if m.get("waist_cm"):
                parts.append(f"Bauchumfang: {m['waist_cm']} cm")
            if m.get("pregnancy_week"):
                parts.append(f"SSW {m['pregnancy_week']}")
            if m.get("note"):
                parts.append(m["note"])
            lines.append(" – ".join(parts))
        measurements_block = "\n\nMaße/Notizen im Zeitraum:\n" + "\n".join(lines)

    if gen_type == "summary":
        instruction = (
            "Schreibe eine sachliche, chronologische Zusammenfassung dieses "
            "Zeitraums. Hebe die wichtigsten Ereignisse und Entwicklungen hervor. "
            "Schreibe auf Deutsch, in einem warmen, aber klaren Stil."
        )
    else:  # story
        instruction = (
            "Schreibe eine warme, erzählerische Geschichte über diesen Zeitraum, "
            "als würde sie später dem Kind vorgelesen werden. Schreibe auf Deutsch, "
            "einfühlsam und bildhaft, aber ohne zu kitschig zu werden."
        )

    return (
        f"{instruction}\n\n"
        f"Zeitraum: {period_start} bis {period_end}\n\n"
        f"Tagebucheinträge:\n{entries_text}"
        f"{captions_block}"
        f"{measurements_block}"
    )


@ai_bp.route("/api/ai/draft-prompt", methods=["POST"])
@parent_required
def draft_prompt():
    """Schritt 1: Daten sammeln, Prompt-Entwurf zurückgeben (kein API-Call)."""
    data = request.get_json(silent=True) or {}
    period_start = data.get("period_start")
    period_end = data.get("period_end")
    gen_type = data.get("type", "summary")

    if not period_start or not period_end:
        return jsonify({"error": "period_start und period_end sind Pflichtfelder."}), 400
    if gen_type not in ("summary", "story"):
        return jsonify({"error": "type muss 'summary' oder 'story' sein."}), 400

    period_data = _gather_period_data(period_start, period_end)
    prompt = _build_prompt(period_start, period_end, gen_type, period_data)

    return jsonify(
        {
            "prompt": prompt,
            "entry_count": len(period_data["entries"]),
            "caption_count": len(period_data["captions"]),
            "measurement_count": len(period_data["measurements"]),
        }
    )


@ai_bp.route("/api/ai/generate", methods=["POST"])
@parent_required
def generate():
    """Schritt 2: Den (ggf. editierten) Prompt an Claude senden, Ergebnis speichern."""
    data = request.get_json(silent=True) or {}
    period_start = data.get("period_start")
    period_end = data.get("period_end")
    gen_type = data.get("type", "summary")
    prompt = (data.get("prompt") or "").strip()

    if not period_start or not period_end or not prompt:
        return jsonify({"error": "period_start, period_end und prompt sind Pflichtfelder."}), 400

    try:
        client = _get_anthropic_client()
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        content = "".join(block.text for block in response.content if block.type == "text")
    except Exception as e:
        return jsonify({"error": f"KI-Anfrage fehlgeschlagen: {e}"}), 502

    result = (
        supabase.table("ai_generations")
        .insert(
            {
                "requested_by": session["user_id"],
                "period_start": period_start,
                "period_end": period_end,
                "type": gen_type,
                "prompt_used": prompt,
                "content": content,
                "is_published": False,
            }
        )
        .execute()
    )
    return jsonify(result.data[0]), 201


@ai_bp.route("/api/ai/generations", methods=["GET"])
@parent_required
def list_generations():
    """Alle bisherigen Generierungen, neueste zuerst."""
    result = (
        supabase.table("ai_generations")
        .select("*")
        .order("created_at", desc=True)
        .execute()
    )
    return jsonify(result.data)


@ai_bp.route("/api/ai/generations/<generation_id>/publish", methods=["PUT"])
@parent_required
def set_generation_published(generation_id):
    """Eltern schalten die Gast-Sichtbarkeit einer Generierung an/aus."""
    data = request.get_json(silent=True) or {}
    published = bool(data.get("is_published", False))

    existing = supabase.table("ai_generations").select("id").eq("id", generation_id).execute()
    if not existing.data:
        return jsonify({"error": "Generierung nicht gefunden."}), 404

    result = (
        supabase.table("ai_generations")
        .update({"is_published": published})
        .eq("id", generation_id)
        .execute()
    )
    return jsonify(result.data[0])


@ai_bp.route("/api/ai/generations/<generation_id>", methods=["DELETE"])
@parent_required
def delete_generation(generation_id):
    existing = supabase.table("ai_generations").select("id").eq("id", generation_id).execute()
    if not existing.data:
        return jsonify({"error": "Generierung nicht gefunden."}), 404

    supabase.table("ai_generations").delete().eq("id", generation_id).execute()
    return jsonify({"ok": True})
