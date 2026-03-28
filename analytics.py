"""
photobooth analytics.py
Drop this file into your photobooth project.
Call log_print(template_name) wherever a print job fires.
"""

import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "photobooth_stats.db")


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Call once at startup to create tables if they don't exist."""
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS prints (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                template  TEXT    NOT NULL,
                printed_at TEXT   NOT NULL
            )
        """)
        conn.commit()


def log_print(template_name: str):
    """
    Call this every time a print job is sent.
    
    Example:
        from analytics import log_print
        log_print("Gold Foil 4x6")
    """
    init_db()
    ts = datetime.now().isoformat(timespec="seconds")
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO prints (template, printed_at) VALUES (?, ?)",
            (template_name, ts)
        )
        conn.commit()
    print(f"[Analytics] Logged print: {template_name} @ {ts}")


# ── Query helpers used by the dashboard ──────────────────────────────────────

def get_stats():
    init_db()
    with _get_conn() as conn:
        today = datetime.now().date().isoformat()

        total = conn.execute("SELECT COUNT(*) FROM prints").fetchone()[0]

        today_count = conn.execute(
            "SELECT COUNT(*) FROM prints WHERE DATE(printed_at) = ?", (today,)
        ).fetchone()[0]

        templates = conn.execute("""
            SELECT template, COUNT(*) as count
            FROM prints
            GROUP BY template
            ORDER BY count DESC
        """).fetchall()

        hourly = conn.execute("""
            SELECT strftime('%H', printed_at) as hour, COUNT(*) as count
            FROM prints
            WHERE DATE(printed_at) = ?
            GROUP BY hour
        """, (today,)).fetchall()

        daily = conn.execute("""
            SELECT DATE(printed_at) as day, COUNT(*) as count
            FROM prints
            GROUP BY day
            ORDER BY day DESC
            LIMIT 30
        """).fetchall()

        last_10 = conn.execute("""
            SELECT template, printed_at
            FROM prints
            ORDER BY id DESC
            LIMIT 10
        """).fetchall()

    return {
        "total": total,
        "today": today_count,
        "templates": [dict(r) for r in templates],
        "hourly_today": [dict(r) for r in hourly],
        "daily_last_30": [dict(r) for r in reversed(daily)],
        "last_10": [dict(r) for r in last_10],
    }


def get_stats_for_date(date_str: str):
    """
    Get detailed stats for a specific date (YYYY-MM-DD format).
    Returns hourly breakdown, templates, and recent prints for that day.
    """
    init_db()
    with _get_conn() as conn:
        # Total for that specific day
        day_count = conn.execute(
            "SELECT COUNT(*) FROM prints WHERE DATE(printed_at) = ?", (date_str,)
        ).fetchone()[0]

        # Hourly breakdown for that day
        hourly = conn.execute("""
            SELECT strftime('%H', printed_at) as hour, COUNT(*) as count
            FROM prints
            WHERE DATE(printed_at) = ?
            GROUP BY hour
            ORDER BY hour
        """, (date_str,)).fetchall()

        # Template breakdown for that day
        templates = conn.execute("""
            SELECT template, COUNT(*) as count
            FROM prints
            WHERE DATE(printed_at) = ?
            GROUP BY template
            ORDER BY count DESC
        """, (date_str,)).fetchall()

        # Recent prints from that day
        recent = conn.execute("""
            SELECT template, printed_at
            FROM prints
            WHERE DATE(printed_at) = ?
            ORDER BY printed_at DESC
            LIMIT 20
        """, (date_str,)).fetchall()

    return {
        "date": date_str,
        "count": day_count,
        "hourly": [dict(h) for h in hourly],
        "templates": [dict(t) for t in templates],
        "recent": [dict(r) for r in recent],
    }
