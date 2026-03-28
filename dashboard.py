"""
photobooth dashboard.py
Run this alongside your photobooth app:
    python dashboard.py

Then open http://<booth-ip>:5001 on any device on the same WiFi.
"""

import threading
import socket
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


def _get_network_ips():
    """Get all available network IPs for the dashboard."""
    ips = []
    try:
        # Try to get all network interfaces
        hostname = socket.gethostname()
        all_ips = socket.gethostbyname_ex(hostname)[2]
        ips.extend([ip for ip in all_ips if not ip.startswith('127.')])
    except Exception:
        pass
    
    # Also try localhost
    if not ips:
        ips = ['127.0.0.1']
    
    return list(set(ips))


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
    
    # Print connection info
    network_ips = _get_network_ips()
    print(f"[Dashboard] Server started on port {port}")
    print(f"[Dashboard] Access from any device (same network or not):")
    for ip in network_ips:
        print(f"[Dashboard]   → http://{ip}:{port}")
    print(f"[Dashboard] Dashboard will auto-reconnect when booth comes back online.")



if __name__ == "__main__":
    # Standalone mode — run directly with: python dashboard.py
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    
    run_dashboard(host="0.0.0.0", port=5001, debug=False)
