"""
photobooth dashboard.py
Run this alongside your photobooth app:
    python dashboard.py

Then open http://<booth-ip>:5001 on any device on the same WiFi.
"""

import threading
from pathlib import Path

from flask import Flask, jsonify, render_template_string
from analytics import get_stats

_BASE = Path(__file__).resolve().parent
app = Flask(__name__)

DASHBOARD_HTML = (_BASE / "dashboard.html").read_text(encoding="utf-8")


@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/stats")
def api_stats():
    return jsonify(get_stats())


def run_dashboard(host="0.0.0.0", port=5001, debug=False):
    """
    Launch the dashboard in a background thread so it doesn't
    block your main photobooth loop.

    Usage in your main booth script:
        from dashboard import run_dashboard
        run_dashboard()          # non-blocking
    """
    t = threading.Thread(
        target=lambda: app.run(host=host, port=port, debug=debug, use_reloader=False),
        daemon=True
    )
    t.start()
    print(f"[Dashboard] Live at http://0.0.0.0:{port}  (open on phone via LAN IP)")


if __name__ == "__main__":
    # Standalone mode — run directly with: python dashboard.py
    app.run(host="0.0.0.0", port=5001, debug=True)
