"""
Baby-Maße: Körpergröße (cm) und Körpergewicht (kg) nach der Geburt.
Beide Werte sind optional pro Eintrag (man kann auch nur eines erfassen).
Lesen ist für alle eingeloggten Nutzer (auch Gäste) sichtbar; Eintragen/
Ändern/Löschen bleibt auf Eltern beschränkt.
"""
from flask import Blueprint, request, jsonify, session

from db import supabase
from auth import parent_required, login_required

measurements_bp = Blueprint("measurements", __name__)


@measurements_bp.route("/api/measurements", methods=["GET"])
@login_required
def list_measurements():
    """Alle Baby-Maße, älteste zuerst (passend für eine Zeitreihe/Chart)."""
    date_from = request.args.get("from")
    date_to = request.args.get("to")

    query = supabase.table("baby_measurements").select("*").order("measurement_date")
    if date_from:
        query = query.gte("measurement_date", date_from)
    if date_to:
        query = query.lte("measurement_date", date_to)

    result = query.execute()
    return jsonify(result.data)


@measurements_bp.route("/api/measurements", methods=["POST"])
@parent_required
def create_measurement():
    data = request.get_json(silent=True) or {}
    measurement_date = data.get("measurement_date")
    height_cm = data.get("height_cm")
    weight_kg = data.get("weight_kg")
    note = data.get("note")

    if not measurement_date:
        return jsonify({"error": "measurement_date ist ein Pflichtfeld."}), 400
    if height_cm is None and weight_kg is None:
        return jsonify({"error": "Mindestens Größe oder Gewicht muss angegeben werden."}), 400

    result = (
        supabase.table("baby_measurements")
        .insert(
            {
                "measurement_date": measurement_date,
                "height_cm": height_cm,
                "weight_kg": weight_kg,
                "note": note,
                "created_by": session["user_id"],
            }
        )
        .execute()
    )
    return jsonify(result.data[0]), 201


@measurements_bp.route("/api/measurements/<measurement_id>", methods=["PUT"])
@parent_required
def update_measurement(measurement_id):
    existing = (
        supabase.table("baby_measurements").select("id").eq("id", measurement_id).execute()
    )
    if not existing.data:
        return jsonify({"error": "Eintrag nicht gefunden."}), 404

    data = request.get_json(silent=True) or {}
    update_fields = {}
    for field in ("measurement_date", "height_cm", "weight_kg", "note"):
        if field in data:
            update_fields[field] = data[field]

    if not update_fields:
        return jsonify({"error": "Keine Änderungen übergeben."}), 400

    result = (
        supabase.table("baby_measurements")
        .update(update_fields)
        .eq("id", measurement_id)
        .execute()
    )
    return jsonify(result.data[0])


@measurements_bp.route("/api/measurements/<measurement_id>", methods=["DELETE"])
@parent_required
def delete_measurement(measurement_id):
    existing = (
        supabase.table("baby_measurements").select("id").eq("id", measurement_id).execute()
    )
    if not existing.data:
        return jsonify({"error": "Eintrag nicht gefunden."}), 404

    supabase.table("baby_measurements").delete().eq("id", measurement_id).execute()
    return jsonify({"ok": True})
