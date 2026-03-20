import os

import psycopg2
import psycopg2.extras
import requests
from flask import Flask, g, jsonify, render_template, request
from icalendar import Calendar
from datetime import datetime, date
from zoneinfo import ZoneInfo

app = Flask(__name__)

ICAL_URL = "https://calendar.google.com/calendar/ical/4689879c573066382f0d2ae50dab1064f6c7f99f131987ae9fc3f02df1f94f24%40group.calendar.google.com/public/basic.ics"
DATABASE_URL = os.environ["DATABASE_URL"]

LA = ZoneInfo("America/Los_Angeles")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = psycopg2.connect(DATABASE_URL)
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    with conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bookings (
                    event_uid   TEXT PRIMARY KEY,
                    first_name  TEXT NOT NULL,
                    last_initial TEXT NOT NULL,
                    phone       TEXT NOT NULL,
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)
    conn.close()


# ---------------------------------------------------------------------------
# iCal helpers
# ---------------------------------------------------------------------------

def parse_dt(dt):
    """Return an ISO 8601 string from a date or datetime, converted to LA time."""
    if isinstance(dt, datetime):
        if dt.tzinfo is not None:
            dt = dt.astimezone(LA)
        return dt.isoformat()
    if isinstance(dt, date):
        return dt.isoformat()
    return str(dt)


def fetch_events():
    response = requests.get(ICAL_URL, timeout=10)
    response.raise_for_status()
    cal = Calendar.from_ical(response.content)

    # Load all booked UIDs in one query
    with get_db().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT event_uid, first_name, last_initial FROM bookings")
        booked = {row["event_uid"]: row for row in cur.fetchall()}

    events = []
    for component in cal.walk():
        if component.name != "VEVENT":
            continue

        start = component.get("DTSTART")
        end = component.get("DTEND")
        description = str(component.get("DESCRIPTION", "")) or None
        location = str(component.get("LOCATION", "")) or None
        uid = str(component.get("UID", ""))

        booking = booked.get(uid)
        event = {
            "id": uid,
            "title": f"Flight Lesson ({'1' if booking else '0'}/1)",
            "start": parse_dt(start.dt) if start else None,
            "end": parse_dt(end.dt) if end else None,
            "booked": bool(booking),
        }
        if booking:
            event["bookedBy"] = f"{booking['first_name']} {booking['last_initial']}."
        if description:
            event["description"] = description
        if location:
            event["location"] = location

        events.append(event)

    return events


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return {"message": "Hello, World!"}


@app.route("/calendar")
def calendar():
    return render_template("calendar.html")


@app.route("/api/events/<path:uid>/book", methods=["POST"])
def book_event(uid):
    data = request.get_json(force=True) or {}
    first_name = (data.get("first_name") or "").strip()
    last_initial = (data.get("last_initial") or "").strip()
    phone = (data.get("phone") or "").strip()

    if not first_name or not last_initial or not phone:
        return jsonify({"error": "first_name, last_initial, and phone are required"}), 400

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bookings (event_uid, first_name, last_initial, phone)
                VALUES (%s, %s, %s, %s)
                """,
                (uid, first_name, last_initial, phone),
            )
        db.commit()
    except psycopg2.errors.UniqueViolation:
        db.rollback()
        return jsonify({"error": "This flight is already booked"}), 409

    return jsonify({
        "title": "Flight Lesson (1/1)",
        "bookedBy": f"{first_name} {last_initial}.",
    })


@app.route("/api/events")
def events():
    try:
        return jsonify(fetch_events())
    except Exception as e:
        return jsonify({"error": str(e)}), 502


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5001)
