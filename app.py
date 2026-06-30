"""
Projekt Logbuch — Backend-Einstiegspunkt.

Liefert sowohl die API (unter /api/...) als auch das Frontend
(frontend/index.html) aus derselben Flask-App aus -- vermeidet getrennte
Domains/CORS-Komplikationen für ein Projekt dieser Größe.
"""
import os
from pathlib import Path
from flask import Flask, request, jsonify, session, send_from_directory
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask import render_template
from dotenv import load_dotenv

# Lädt die .env unabhängig davon, aus welchem Verzeichnis "python app.py"
# aufgerufen wird (z.B. auch wenn die .env eine Ebene höher liegt).
# Auf Render selbst ist keine .env-Datei vorhanden -- dort kommen die Werte
# direkt aus den im Dashboard gesetzten Umgebungsvariablen, load_dotenv()
# findet dann einfach nichts und das ist kein Fehler.
load_dotenv(Path(__file__).resolve().parent / ".env")
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

from auth import (
    seed_parents,
    login_with_codename,
    login_required,
)
from entries import entries_bp
from images import images_bp
from comments import comments_bp
from ai import ai_bp
from invites import invites_bp
from measurements import measurements_bp

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET_KEY"]

# In Produktion (Render setzt RENDER=true automatisch) läuft alles über
# HTTPS -- Cookies entsprechend absichern. Lokal (kein HTTPS) bleibt
# Secure=False, sonst würde das Cookie im Browser verworfen werden.
IS_PRODUCTION = os.environ.get("RENDER") is not None
app.config.update(
    SESSION_COOKIE_SECURE=IS_PRODUCTION,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

# CORS: Frontend wird von derselben Domain ausgeliefert, daher reicht ein
# permissiver Default für die lokale Entwicklung (unterschiedliche Ports).
CORS(app, supports_credentials=True)

limiter = Limiter(get_remote_address, app=app, default_limits=[])

app.register_blueprint(entries_bp)
app.register_blueprint(images_bp)
app.register_blueprint(comments_bp)
app.register_blueprint(ai_bp)
app.register_blueprint(invites_bp)
app.register_blueprint(measurements_bp)

# Öffentliche Invite-Routen sind ohne Login erreichbar -- zusätzliches
# Rate-Limiting gegen Token-Brute-Force, auch wenn die Tokens (32 Zeichen,
# kryptographisch zufällig) praktisch nicht erratbar sind.
limiter.limit("20 per 5 minutes")(app.view_functions["invites.check_invite"])
limiter.limit("10 per 5 minutes")(app.view_functions["invites.accept_invite"])
# guest-login nutzt eine 4-stellige PIN (nur 10.000 Kombinationen) --
# entsprechend strikteres Limit, um Brute-Force unpraktikabel zu machen.
limiter.limit("5 per 15 minutes")(app.view_functions["invites.guest_login"])

# Eltern/Gast-Accounts anlegen, falls noch nicht vorhanden
with app.app_context():
    seed_parents()


@app.route("/api/auth/login", methods=["POST"])
@limiter.limit("10 per 5 minutes")
def login():
    data = request.get_json(silent=True) or {}
    codename = data.get("codename", "")

    if not codename:
        return jsonify({"error": "Codename fehlt."}), 400

    user = login_with_codename(codename)
    if not user:
        return jsonify({"error": "Codename nicht gefunden."}), 401

    session["user_id"] = user["id"]
    session["role"] = user["role"]
    session["display_name"] = user["display_name"]

    return jsonify(
        {
            "id": user["id"],
            "role": user["role"],
            "display_name": user["display_name"],
        }
    )


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/auth/me", methods=["GET"])
@login_required
def me():
    return jsonify(
        {
            "id": session["user_id"],
            "role": session["role"],
            "display_name": session["display_name"],
        }
    )


# ---------- Frontend-Auslieferung ----------
# Alles, was nicht mit /api/ beginnt, bekommt die Single-Page-App
# (index.html) ausgeliefert. Das Routing innerhalb der App passiert
# clientseitig über den ?invite=TOKEN Parameter und internen State.

@app.route("/")
def serve_frontend():
    return render_template("index.html")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
