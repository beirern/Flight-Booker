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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS waitlist (
                    id           SERIAL PRIMARY KEY,
                    event_uid    TEXT NOT NULL,
                    first_name   TEXT NOT NULL,
                    last_initial TEXT NOT NULL,
                    phone        TEXT NOT NULL,
                    created_at   TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(event_uid, phone)
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


def build_event_title(booked: bool, wl_count: int) -> str:
    slot = "1" if booked else "0"
    title = f"Flight Lesson ({slot}/1)"
    if wl_count > 0:
        title += f" +{wl_count} WL"
    return title


def fetch_events():
    response = requests.get(ICAL_URL, timeout=10)
    response.raise_for_status()
    cal = Calendar.from_ical(response.content)

    # Load all booked UIDs and waitlist counts in one query each
    db = get_db()
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT event_uid, first_name, last_initial FROM bookings")
        booked = {row["event_uid"]: row for row in cur.fetchall()}
        cur.execute("SELECT event_uid, COUNT(*) as cnt FROM waitlist GROUP BY event_uid")
        waitlist_counts = {row["event_uid"]: row["cnt"] for row in cur.fetchall()}

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
        wl_count = waitlist_counts.get(uid, 0)

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
            "title": build_event_title(bool(booking), wl_count),
            "start": parse_dt(start.dt) if start else None,
            "end": parse_dt(end.dt) if end else None,
            "booked": bool(booking),
            "past": past,
            "waitlistCount": wl_count,
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

    if not re.fullmatch(r"[A-Za-z ]+", first_name):
        return jsonify({"error": "First name must contain only letters"}), 400
    if not re.fullmatch(r"[A-Za-z]", last_initial):
        return jsonify({"error": "Last initial must be a single letter"}), 400
    phone_digits = re.sub(r"\D", "", phone)
    if len(phone_digits) != 10:
        return jsonify({"error": "Phone number must be 10 digits"}), 400
    phone = phone_digits

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
        # Flight is full — add to waitlist instead
        try:
            with db.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO waitlist (event_uid, first_name, last_initial, phone)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (uid, first_name, last_initial, phone),
                )
                cur.execute(
                    "SELECT COUNT(*) FROM waitlist WHERE event_uid = %s",
                    (uid,),
                )
                position = cur.fetchone()[0]
            db.commit()
        except psycopg2.errors.UniqueViolation:
            db.rollback()
            return jsonify({"error": "You are already on the waitlist"}), 409
        except psycopg2.Error as e:
            db.rollback()
            logging.error("Database error during waitlist: %s", e)
            return jsonify({"error": "Database error"}), 500

        send_ntfy("New Waitlist Entry", f"Name: {first_name} {last_initial}.\nPhone: {phone}\nEvent: {uid}\nPosition: {position}")
        return jsonify({
            "status": "waitlisted",
            "waitlistPosition": position,
            "title": build_event_title(True, position),
        })
    except psycopg2.Error as e:
        db.rollback()
        logging.error("Database error during booking: %s", e)
        return jsonify({"error": "Database error"}), 500

    send_ntfy("New Booking", f"Name: {first_name} {last_initial}.\nPhone: {phone}\nEvent: {uid}")

    return jsonify({
        "status": "booked",
        "title": "Flight Lesson (1/1)",
        "bookedBy": f"{first_name} {last_initial}.",
    })


@app.route("/api/events/<path:uid>/book", methods=["DELETE"])
def unbook_event(uid):
    db = get_db()
    try:
        with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("DELETE FROM bookings WHERE event_uid = %s", (uid,))
            deleted = cur.rowcount
            if deleted == 0:
                return jsonify({"error": "No booking found"}), 404

            # Promote first waitlist entry if one exists
            cur.execute(
                "SELECT id, first_name, last_initial, phone FROM waitlist WHERE event_uid = %s ORDER BY id LIMIT 1",
                (uid,),
            )
            next_up = cur.fetchone()
            if next_up:
                cur.execute(
                    "INSERT INTO bookings (event_uid, first_name, last_initial, phone) VALUES (%s, %s, %s, %s)",
                    (uid, next_up["first_name"], next_up["last_initial"], next_up["phone"]),
                )
                cur.execute("DELETE FROM waitlist WHERE id = %s", (next_up["id"],))
                cur.execute("SELECT COUNT(*) AS cnt FROM waitlist WHERE event_uid = %s", (uid,))
                wl_count = cur.fetchone()["cnt"]
        db.commit()
    except psycopg2.Error as e:
        db.rollback()
        logging.error("Database error during cancellation: %s", e)
        return jsonify({"error": "Database error"}), 500

    if next_up:
        promoted_name = f"{next_up['first_name']} {next_up['last_initial']}."
        send_ntfy(
            "Booking Cancelled - Waitlist Promoted",
            f"Event: {uid}\nPromoted: {promoted_name}\nPhone: {next_up['phone']}",
        )
        return jsonify({
            "title": build_event_title(True, wl_count),
            "bookedBy": promoted_name,
            "promoted": True,
            "waitlistCount": wl_count,
        })

    send_ntfy("Booking Cancelled", f"Event: {uid}")
    return jsonify({"title": build_event_title(False, 0), "waitlistCount": 0})


@app.route("/api/events/<path:uid>/waitlist", methods=["DELETE"])
def leave_waitlist(uid):
    data = request.get_json(force=True) or {}
    phone = (data.get("phone") or "").strip()
    if not phone:
        return jsonify({"error": "phone is required"}), 400
    phone_digits = re.sub(r"\D", "", phone)
    if len(phone_digits) != 10:
        return jsonify({"error": "Phone number must be 10 digits"}), 400
    phone = phone_digits

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                "DELETE FROM waitlist WHERE event_uid = %s AND phone = %s",
                (uid, phone),
            )
            if cur.rowcount == 0:
                return jsonify({"error": "Not on waitlist"}), 404
            cur.execute("SELECT COUNT(*) FROM waitlist WHERE event_uid = %s", (uid,))
            wl_count = cur.fetchone()[0]
        db.commit()
    except psycopg2.Error as e:
        db.rollback()
        logging.error("Database error leaving waitlist: %s", e)
        return jsonify({"error": "Database error"}), 500

    return jsonify({"title": build_event_title(True, wl_count), "waitlistCount": wl_count})


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
