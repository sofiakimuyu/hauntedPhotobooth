"""
photobooth dashboard.py
Run this alongside your photobooth app:
    python dashboard.py

Then open http://<booth-ip>:5001 on any device on the same WiFi.
"""

import threading
from pathlib import Path

from flask import Flask, jsonify, render_template_string
from analytics import get_stats, get_stats_for_date

_BASE = Path(__file__).resolve().parent
app = Flask(__name__)

DASHBOARD_HTML = (_BASE / "dashboard.html").read_text(encoding="utf-8")


@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/stats")
def api_stats():
    """
    Fetch stats from the booth. Returns gracefully with booth_status
    if the booth or database is unavailable.
    """
    try:
        stats_data = get_stats()
        return jsonify({
            "booth_status": "online",
            **stats_data
        })
    except Exception as e:
        # Booth is down or database unavailable — still return 200 with offline status
        print(f"[Dashboard] Could not fetch stats: {e}")
        return jsonify({
            "booth_status": "offline",
            "error": str(e),
            "total": 0,
            "today": 0,
            "templates": [],
            "hourly_today": [],
            "daily_last_30": [],
            "last_10": [],
        }), 200


@app.route("/api/stats/date/<date_str>")
def api_stats_date(date_str):
    """
    Fetch detailed stats for a specific date (YYYY-MM-DD format).
    """
    try:
        stats_data = get_stats_for_date(date_str)
        return jsonify({
            "booth_status": "online",
            **stats_data
        })
    except Exception as e:
        print(f"[Dashboard] Could not fetch stats for date {date_str}: {e}")
        return jsonify({
            "booth_status": "offline",
            "error": str(e),
            "date": date_str,
            "count": 0,
            "hourly": [],
            "templates": [],
            "recent": [],
        }), 200


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
