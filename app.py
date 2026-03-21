import logging
import os
import re

import psycopg2
import psycopg2.extras
import requests
from flask import Flask, g, jsonify, render_template, request
from icalendar import Calendar
from datetime import datetime, date
from zoneinfo import ZoneInfo

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

ICAL_URL = "https://calendar.google.com/calendar/ical/4689879c573066382f0d2ae50dab1064f6c7f99f131987ae9fc3f02df1f94f24%40group.calendar.google.com/public/basic.ics"
DATABASE_URL = os.environ["DATABASE_URL"]
NTFY_URL = "https://ntfy.sh/nicola-b-flying-14587"

LA = ZoneInfo("America/Los_Angeles")
_DESCRIPTION_SEP = re.compile(r"(?:\r?\n|(?<=\.)\s+(?=[A-Z][A-Za-z ]+:))")


# ---------------------------------------------------------------------------
# Notification helpers
# ---------------------------------------------------------------------------

def send_ntfy(title, body):
    try:
        r = requests.post(NTFY_URL, data=body, headers={"Title": title}, timeout=5)
        r.raise_for_status()
        logging.info("ntfy notification sent: %s", title)
    except requests.exceptions.RequestException as e:
        logging.warning("Failed to send ntfy notification (%s): %s", title, e)


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


def format_description(raw):
    """Parse iCal description key-value pairs and reformat for display."""
    if not raw:
        return None

    segments = _DESCRIPTION_SEP.split(raw.rstrip("."))

    fields = {}
    for seg in segments:
        seg = seg.strip().rstrip(".")
        if ":" in seg:
            key, _, val = seg.partition(":")
            fields[key.strip()] = val.strip()

    flight_type = fields.get("Flight Type", "")
    aircraft = fields.get("Aircraft", "")

    if flight_type == "Training":
        course = fields.get("Course", "")
        lesson = fields.get("Lesson", "")
        parts = ["Flight Type: Training"]
        if aircraft:
            parts.append(f"Aircraft: {aircraft}")
        if course:
            parts.append(f"Course: {course}")
        if lesson:
            parts.append(f"Lesson: {lesson}")
        return "\n".join(parts)
    elif flight_type == "Leisure":
        parts = ["Flight Type: Leisure"]
        if aircraft:
            parts.append(f"Aircraft: {aircraft}")
        return "\n".join(parts)

    return raw


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
        raw_description = str(component.get("DESCRIPTION", "")) or None
        description = format_description(raw_description)
        location = str(component.get("LOCATION", "")) or None
        uid = str(component.get("UID", ""))

        booking = booked.get(uid)

        # Determine whether this event is in the past
        end_dt = end.dt if end else (start.dt if start else None)
        if end_dt is None:
            past = False
        elif isinstance(end_dt, datetime):
            aware = end_dt.astimezone(LA) if end_dt.tzinfo is not None else end_dt.replace(tzinfo=LA)
            past = aware < datetime.now(LA)
        else:
            # date-only
            past = end_dt < date.today()

        event = {
            "id": uid,
            "title": f"Flight Lesson ({'1' if booking else '0'}/1)",
            "start": parse_dt(start.dt) if start else None,
            "end": parse_dt(end.dt) if end else None,
            "booked": bool(booking),
            "past": past,
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
    except psycopg2.Error as e:
        db.rollback()
        logging.error("Database error during booking: %s", e)
        return jsonify({"error": "Database error"}), 500

    send_ntfy("New Booking", f"Name: {first_name} {last_initial}.\nPhone: {phone}\nEvent: {uid}")

    return jsonify({
        "title": "Flight Lesson (1/1)",
        "bookedBy": f"{first_name} {last_initial}.",
    })


@app.route("/api/events/<path:uid>/book", methods=["DELETE"])
def unbook_event(uid):
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("DELETE FROM bookings WHERE event_uid = %s", (uid,))
            deleted = cur.rowcount
        db.commit()
    except psycopg2.Error as e:
        db.rollback()
        logging.error("Database error during cancellation: %s", e)
        return jsonify({"error": "Database error"}), 500
    if deleted == 0:
        return jsonify({"error": "No booking found"}), 404

    send_ntfy("Booking Cancelled", f"Event: {uid}")

    return jsonify({"title": "Flight Lesson (0/1)"})


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
    app.run(debug=True, port=5000)
