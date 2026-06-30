"""
Einladungslink-System für Gäste.

Konzept: Eltern erstellen vorab einen Gast-Eintrag mit vorgegebenem
display_name. Statt eines selbstgewählten Codenamens bekommt der Gast einen
langen, zufälligen Token als Teil eines Links. Der Token IST im Grunde der
Codename -- er wird genauso gehasht und in codename_hash gespeichert wie bei
den Eltern-Codenamen, nur dass ihn niemand selbst eintippen muss.

Zusätzlich bekommt jeder Gast eine kurze, zufällige PIN (4 Ziffern) für den
Re-Login per Name+PIN, falls der Link verloren geht oder die Session
abläuft. Die PIN wird wie der Token gehasht gespeichert -- aber NICHT
zusammen mit dem Namen, da mehrere Gäste denselben Namen haben könnten.
Stattdessen: Login sucht alle Gäste mit passendem Namen, vergleicht die PIN
gegen jeden Kandidaten (kleine Nutzerzahl erwartet, daher unproblematisch).

Öffentliche Routen (kein Login nötig): /api/invite/<token>, .../accept,
und /api/auth/guest-login -- das ist beabsichtigt, da der Gast ja noch
keine Session hat. Token/PIN übernehmen die Sicherheitsfunktion.
"""
import secrets
import hashlib

from flask import Blueprint, request, jsonify, session

from db import supabase
from auth import parent_required, hash_codename, FLASK_SECRET_KEY

invites_bp = Blueprint("invites", __name__)

TOKEN_BYTES = 24  # ergibt einen ~32-stelligen URL-sicheren Token
PIN_LENGTH = 4


def _generate_pin() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(PIN_LENGTH))


def _hash_pin(user_id: str, pin: str) -> str:
    """PIN wird zusammen mit der User-ID gehasht (nicht dem Namen), damit
    gleichnamige Gäste mit zufällig gleicher PIN nicht kollidieren können."""
    salted = f"{FLASK_SECRET_KEY}:{user_id}:{pin.strip()}"
    return hashlib.sha256(salted.encode("utf-8")).hexdigest()


@invites_bp.route("/api/invites", methods=["POST"])
@parent_required
def create_invite():
    """Eltern erstellen eine neue Einladung mit vorgegebenem Anzeigenamen.
    Gibt sowohl den Link-Token als auch eine PIN für den Re-Login zurück --
    beide werden nur HIER einmalig im Klartext angezeigt."""
    data = request.get_json(silent=True) or {}
    display_name = (data.get("display_name") or "").strip()

    if not display_name:
        return jsonify({"error": "Name darf nicht leer sein."}), 400

    token = secrets.token_urlsafe(TOKEN_BYTES)
    codename_hash = hash_codename(token)

    result = (
        supabase.table("users")
        .insert(
            {
                "role": "guest",
                "codename_hash": codename_hash,
                "display_name": display_name,
            }
        )
        .execute()
    )
    user = result.data[0]

    pin = _generate_pin()
    pin_hash = _hash_pin(user["id"], pin)
    supabase.table("users").update({"guest_pin": pin_hash}).eq("id", user["id"]).execute()

    return jsonify(
        {"id": user["id"], "display_name": display_name, "token": token, "pin": pin}
    ), 201


@invites_bp.route("/api/invites", methods=["GET"])
@parent_required
def list_invites():
    """Liste aller Gäste (= Einladungen). Token/PIN selbst werden NICHT
    zurückgegeben (nur beim Erstellen einmalig sichtbar), da sie aus dem
    Hash nicht rekonstruierbar sind -- das ist beabsichtigt (wie ein
    Passwort). Bei Verlust muss eine neue Einladung erstellt werden."""
    result = (
        supabase.table("users")
        .select("id, display_name, email, created_at")
        .eq("role", "guest")
        .order("created_at", desc=True)
        .execute()
    )
    return jsonify(result.data)


@invites_bp.route("/api/invites/<user_id>", methods=["DELETE"])
@parent_required
def delete_invite(user_id):
    """Löscht einen Gast (z.B. falsch erstellt, oder Zugang entziehen)."""
    existing = (
        supabase.table("users").select("id, role").eq("id", user_id).execute()
    )
    if not existing.data:
        return jsonify({"error": "Nicht gefunden."}), 404
    if existing.data[0]["role"] != "guest":
        return jsonify({"error": "Nur Gäste können hier gelöscht werden."}), 400

    supabase.table("users").delete().eq("id", user_id).execute()
    return jsonify({"ok": True})


@invites_bp.route("/api/invite/<token>", methods=["GET"])
def check_invite(token):
    """Öffentlich: prüft, ob der Token gültig ist, gibt den vorgeschlagenen
    Namen zurück (für die Bestätigungs-Anzeige im Frontend)."""
    codename_hash = hash_codename(token)
    result = (
        supabase.table("users")
        .select("id, display_name")
        .eq("codename_hash", codename_hash)
        .execute()
    )
    if not result.data:
        return jsonify({"error": "Einladung nicht gefunden oder ungültig."}), 404

    return jsonify({"display_name": result.data[0]["display_name"]})


@invites_bp.route("/api/invite/<token>/accept", methods=["POST"])
def accept_invite(token):
    """Öffentlich: loggt den Gast über den Token ein (setzt die Session)."""
    codename_hash = hash_codename(token)
    result = (
        supabase.table("users")
        .select("*")
        .eq("codename_hash", codename_hash)
        .execute()
    )
    if not result.data:
        return jsonify({"error": "Einladung nicht gefunden oder ungültig."}), 404

    user = result.data[0]
    session["user_id"] = user["id"]
    session["role"] = user["role"]
    session["display_name"] = user["display_name"]

    return jsonify(
        {"id": user["id"], "role": user["role"], "display_name": user["display_name"]}
    )


@invites_bp.route("/api/auth/guest-login", methods=["POST"])
def guest_login():
    """Öffentlich: Re-Login für Gäste per Name + PIN (Fallback, falls der
    ursprüngliche Link verloren geht oder die Session abläuft)."""
    data = request.get_json(silent=True) or {}
    display_name = (data.get("display_name") or "").strip()
    pin = (data.get("pin") or "").strip()

    if not display_name or not pin:
        return jsonify({"error": "Name und PIN sind erforderlich."}), 400

    candidates = (
        supabase.table("users")
        .select("*")
        .eq("role", "guest")
        .ilike("display_name", display_name)
        .execute()
    )

    for user in candidates.data:
        if user.get("guest_pin") and _hash_pin(user["id"], pin) == user["guest_pin"]:
            session["user_id"] = user["id"]
            session["role"] = user["role"]
            session["display_name"] = user["display_name"]
            return jsonify(
                {"id": user["id"], "role": user["role"], "display_name": user["display_name"]}
            )

    return jsonify({"error": "Name oder PIN ist falsch."}), 401
