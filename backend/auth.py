"""
Codename-basierte Authentifizierung.

Konzept:
- Vater & Mutter haben feste, geheime Codenamen, die einmalig beim Start
  in die Datenbank geseedet werden (siehe seed_parents() unten).
- Gäste werden über Einladungslinks angelegt (siehe invites.py) -- der
  Link-Token übernimmt dabei die Rolle des Codenamens, der Gast muss
  selbst nichts eintippen.
- Codenamen/Tokens werden NIE im Klartext gespeichert, nur als Hash
  (SHA-256 + Server-Secret als Salt-Ersatz, ausreichend für diesen
  Bedrohungsgrad).
- Sessions laufen über Flask-Session (signiertes Cookie), kein JWT nötig,
  da wir keine Cross-Domain-API anbieten.
"""
import os
import hashlib
from functools import wraps
from flask import session, jsonify, request

from db import supabase

FLASK_SECRET_KEY = os.environ["FLASK_SECRET_KEY"]


def hash_codename(codename: str) -> str:
    """Hasht einen Codenamen mit dem App-Secret als zusätzlichem Faktor."""
    salted = f"{FLASK_SECRET_KEY}:{codename.strip()}"
    return hashlib.sha256(salted.encode("utf-8")).hexdigest()


def seed_parents():
    """
    Legt die Vater/Mutter-Accounts an, falls sie noch nicht existieren.
    Wird einmalig beim App-Start aufgerufen (siehe app.py).
    Die Codenamen kommen aus den Env-Variablen, NIE hartcodiert.
    """
    father_codename = os.environ.get("FATHER_CODENAME")
    mother_codename = os.environ.get("MOTHER_CODENAME")

    if not father_codename or not mother_codename:
        raise RuntimeError(
            "FATHER_CODENAME und MOTHER_CODENAME müssen in der .env gesetzt sein."
        )

    for role, codename, display_name in [
        ("parent_father", father_codename, "Papa"),
        ("parent_mother", mother_codename, "Mama"),
    ]:
        codename_hash = hash_codename(codename)
        existing = (
            supabase.table("users")
            .select("id")
            .eq("codename_hash", codename_hash)
            .execute()
        )
        if not existing.data:
            supabase.table("users").insert(
                {
                    "role": role,
                    "codename_hash": codename_hash,
                    "display_name": display_name,
                }
            ).execute()


def login_with_codename(codename: str):
    """
    Prüft einen Codenamen gegen die DB. Gibt das User-Dict zurück oder None.
    Wird nur noch für Eltern-Logins genutzt (Gäste loggen sich über
    Einladungslinks ein, siehe invites.py).
    """
    codename_hash = hash_codename(codename)
    result = (
        supabase.table("users")
        .select("*")
        .eq("codename_hash", codename_hash)
        .execute()
    )
    if result.data:
        return result.data[0]
    return None


def login_required(f):
    """Decorator: Endpunkt erfordert eine gültige Session (jede Rolle)."""

    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Nicht angemeldet."}), 401
        return f(*args, **kwargs)

    return wrapper


def parent_required(f):
    """Decorator: Endpunkt erfordert eine Eltern-Rolle (Vater oder Mutter)."""

    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Nicht angemeldet."}), 401
        if session.get("role") not in ("parent_father", "parent_mother"):
            return jsonify({"error": "Nur für Eltern zugänglich."}), 403
        return f(*args, **kwargs)

    return wrapper
