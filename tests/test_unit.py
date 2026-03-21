"""Unit tests for app.py — covers helpers, DB functions, and all routes."""

import os
from datetime import date, datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import psycopg2.errors
import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

from app import parse_dt, format_description, send_ntfy, LA
from tests.conftest import SAMPLE_ICAL, make_mock_db


class TestParseDt:
    def test_naive_datetime(self):
        dt = datetime(2026, 3, 15, 10, 30, 0)
        assert parse_dt(dt) == "2026-03-15T10:30:00"

    def test_aware_datetime_utc(self):
        dt = datetime(2026, 3, 15, 18, 0, 0, tzinfo=ZoneInfo("UTC"))
        result = parse_dt(dt)
        assert "America/Los_Angeles" in result or "-07:00" in result or "-08:00" in result

    def test_aware_datetime_other_tz(self):
        dt = datetime(2026, 7, 1, 12, 0, 0, tzinfo=ZoneInfo("Europe/London"))
        result = parse_dt(dt)
        assert "T" in result

    def test_date_only(self):
        d = date(2026, 6, 1)
        assert parse_dt(d) == "2026-06-01"

    def test_string_fallback(self):
        assert parse_dt("something") == "something"


class TestFormatDescription:
    def test_none(self):
        assert format_description(None) is None

    def test_empty(self):
        assert format_description("") is None

    def test_training_full(self):
        raw = "Flight Type: Training. Course: PPL. Lesson: Takeoffs. Aircraft: C172"
        result = format_description(raw)
        assert "Flight Type: Training" in result
        assert "Aircraft: C172" in result
        assert "Course: PPL" in result
        assert "Lesson: Takeoffs" in result

    def test_training_no_course(self):
        raw = "Flight Type: Training. Aircraft: C172"
        result = format_description(raw)
        assert "Flight Type: Training" in result
        assert "Aircraft: C172" in result
        assert "Course" not in result

    def test_leisure(self):
        raw = "Flight Type: Leisure. Aircraft: C152"
        result = format_description(raw)
        assert "Flight Type: Leisure" in result
        assert "Aircraft: C152" in result

    def test_leisure_no_aircraft(self):
        raw = "Flight Type: Leisure"
        result = format_description(raw)
        assert result == "Flight Type: Leisure"

    def test_unknown_type_returns_raw(self):
        raw = "Some random description"
        assert format_description(raw) == "Some random description"

    def test_newline_separated(self):
        raw = "Flight Type: Training\nCourse: PPL\nLesson: Stalls\nAircraft: C172"
        result = format_description(raw)
        assert "Flight Type: Training" in result
        assert "Lesson: Stalls" in result


class TestSendNtfy:
    @patch("app.requests.post")
    def test_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp
        send_ntfy("Test Title", "Test Body")
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0].startswith("https://ntfy.sh/")
        assert kwargs["data"] == "Test Body"
        assert kwargs["headers"]["Title"] == "Test Title"

    @patch("app.requests.post", side_effect=__import__("requests").exceptions.RequestException("network error"))
    def test_failure_logs_warning(self, mock_post, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            send_ntfy("Fail", "Body")
        assert "Failed to send ntfy" in caplog.text


class TestInitDb:
    @patch("app.psycopg2.connect")
    def test_creates_table(self, mock_connect):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_connect.return_value = mock_conn

        from app import init_db
        init_db()

        assert mock_cursor.execute.call_count == 2
        sqls = [call[0][0] for call in mock_cursor.execute.call_args_list]
        assert any("CREATE TABLE IF NOT EXISTS bookings" in s for s in sqls)
        assert any("CREATE TABLE IF NOT EXISTS waitlist" in s for s in sqls)
        mock_conn.close.assert_called_once()


class TestIndexRoute:
    def test_returns_hello(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert resp.get_json() == {"message": "Hello, World!"}


class TestCalendarRoute:
    def test_returns_html(self, client):
        resp = client.get("/calendar")
        assert resp.status_code == 200
        assert b"FullCalendar" in resp.data
        assert b"calendar" in resp.data


def _ical_mock(ical_bytes=SAMPLE_ICAL):
    mock_resp = MagicMock()
    mock_resp.content = ical_bytes
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


class TestEventsRoute:
    @patch("app.requests.get")
    @patch("app.get_db")
    def test_returns_events(self, mock_get_db, mock_requests_get, client):
        mock_requests_get.return_value = _ical_mock()
        _, mock_db = make_mock_db()
        mock_get_db.return_value = mock_db

        resp = client.get("/api/events")
        assert resp.status_code == 200
        events = resp.get_json()
        assert isinstance(events, list)
        assert len(events) == 4
        uids = {e["id"] for e in events}
        assert "test-uid-1" in uids
        assert "test-uid-2" in uids
        assert "test-uid-past" in uids
        assert "test-uid-allday" in uids

    @patch("app.requests.get")
    @patch("app.get_db")
    def test_events_with_booking(self, mock_get_db, mock_requests_get, client):
        mock_requests_get.return_value = _ical_mock()
        mock_cursor, mock_db = make_mock_db()
        # First fetchall = bookings, second = waitlist counts (empty)
        mock_cursor.fetchall.side_effect = [
            [{"event_uid": "test-uid-1", "first_name": "Alice", "last_initial": "B"}],
            [],
        ]
        mock_get_db.return_value = mock_db

        resp = client.get("/api/events")
        events = resp.get_json()
        booked = [e for e in events if e["id"] == "test-uid-1"][0]
        assert booked["booked"]
        assert booked["bookedBy"] == "Alice B."
        assert "(1/1)" in booked["title"]
        assert booked["waitlistCount"] == 0

    @patch("app.requests.get")
    @patch("app.get_db")
    def test_events_include_waitlist_count(self, mock_get_db, mock_requests_get, client):
        mock_requests_get.return_value = _ical_mock()
        mock_cursor, mock_db = make_mock_db()
        mock_cursor.fetchall.side_effect = [
            [{"event_uid": "test-uid-1", "first_name": "Alice", "last_initial": "B"}],
            [{"event_uid": "test-uid-1", "cnt": 2}],
        ]
        mock_get_db.return_value = mock_db

        resp = client.get("/api/events")
        events = resp.get_json()
        booked = [e for e in events if e["id"] == "test-uid-1"][0]
        assert booked["waitlistCount"] == 2
        assert "+2 WL" in booked["title"]

    @patch("app.requests.get")
    @patch("app.get_db")
    def test_past_event_flagged(self, mock_get_db, mock_requests_get, client):
        mock_requests_get.return_value = _ical_mock()
        _, mock_db = make_mock_db()
        mock_get_db.return_value = mock_db

        resp = client.get("/api/events")
        events = resp.get_json()
        past = [e for e in events if e["id"] == "test-uid-past"][0]
        assert past["past"]

    @patch("app.requests.get")
    @patch("app.get_db")
    def test_event_description_and_location(self, mock_get_db, mock_requests_get, client):
        mock_requests_get.return_value = _ical_mock()
        _, mock_db = make_mock_db()
        mock_get_db.return_value = mock_db

        resp = client.get("/api/events")
        events = resp.get_json()
        ev1 = [e for e in events if e["id"] == "test-uid-1"][0]
        assert "location" in ev1
        assert "KPAO" in ev1["location"]
        assert "description" in ev1
        assert "Training" in ev1["description"]

    @patch("app.requests.get", side_effect=Exception("ical fetch failed"))
    @patch("app.get_db")
    def test_events_error_returns_502(self, mock_get_db, mock_requests_get, client):
        resp = client.get("/api/events")
        assert resp.status_code == 502
        assert "error" in resp.get_json()

    @patch("app.requests.get")
    @patch("app.get_db")
    def test_allday_event(self, mock_get_db, mock_requests_get, client):
        mock_requests_get.return_value = _ical_mock()
        _, mock_db = make_mock_db()
        mock_get_db.return_value = mock_db

        resp = client.get("/api/events")
        events = resp.get_json()
        allday = [e for e in events if e["id"] == "test-uid-allday"][0]
        assert allday["start"] == "2026-06-01"


class TestBookRoute:
    @patch("app.send_ntfy")
    @patch("app.get_db")
    def test_book_success(self, mock_get_db, mock_ntfy, client):
        _, mock_db = make_mock_db()
        mock_get_db.return_value = mock_db

        resp = client.post(
            "/api/events/test-uid-1/book",
            json={"first_name": "Alice", "last_initial": "B", "phone": "555-1234"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "booked"
        assert data["bookedBy"] == "Alice B."
        assert "(1/1)" in data["title"]
        mock_ntfy.assert_called_once()
        mock_db.commit.assert_called_once()

    @patch("app.get_db")
    def test_book_missing_fields(self, mock_get_db, client):
        resp = client.post("/api/events/uid/book", json={"first_name": "Alice"})
        assert resp.status_code == 400
        assert "required" in resp.get_json()["error"]

    @patch("app.get_db")
    def test_book_empty_fields(self, mock_get_db, client):
        resp = client.post(
            "/api/events/uid/book",
            json={"first_name": "  ", "last_initial": "B", "phone": "555"},
        )
        assert resp.status_code == 400

    @patch("app.get_db")
    def test_book_no_body(self, mock_get_db, client):
        resp = client.post(
            "/api/events/uid/book",
            data="",
            content_type="application/json",
        )
        assert resp.status_code == 400

    @patch("app.send_ntfy")
    @patch("app.get_db")
    def test_book_when_full_adds_to_waitlist(self, mock_get_db, mock_ntfy, client):
        mock_cursor, mock_db = make_mock_db()
        mock_cursor.execute.side_effect = [
            psycopg2.errors.UniqueViolation(),  # bookings INSERT fails
            None,  # waitlist INSERT succeeds
            None,  # SELECT COUNT
        ]
        mock_cursor.fetchone.return_value = (1,)
        mock_get_db.return_value = mock_db

        resp = client.post(
            "/api/events/uid/book",
            json={"first_name": "Bob", "last_initial": "C", "phone": "555"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "waitlisted"
        assert data["waitlistPosition"] == 1
        mock_ntfy.assert_called_once()

    @patch("app.get_db")
    def test_book_already_on_waitlist_returns_409(self, mock_get_db, client):
        mock_cursor, mock_db = make_mock_db()
        mock_cursor.execute.side_effect = [
            psycopg2.errors.UniqueViolation(),  # bookings INSERT fails
            psycopg2.errors.UniqueViolation(),  # waitlist INSERT also fails
        ]
        mock_get_db.return_value = mock_db

        resp = client.post(
            "/api/events/uid/book",
            json={"first_name": "Alice", "last_initial": "B", "phone": "555"},
        )
        assert resp.status_code == 409
        assert "already on the waitlist" in resp.get_json()["error"]

    @patch("app.get_db")
    def test_book_db_error_returns_500(self, mock_get_db, client):
        mock_cursor, mock_db = make_mock_db()
        mock_cursor.execute.side_effect = psycopg2.Error("db broken")
        mock_get_db.return_value = mock_db

        resp = client.post(
            "/api/events/uid/book",
            json={"first_name": "Alice", "last_initial": "B", "phone": "555"},
        )
        assert resp.status_code == 500
        mock_db.rollback.assert_called_once()

    @patch("app.send_ntfy")
    @patch("app.get_db")
    def test_book_uid_with_slash(self, mock_get_db, mock_ntfy, client):
        _, mock_db = make_mock_db()
        mock_get_db.return_value = mock_db

        resp = client.post(
            "/api/events/some/complex/uid/book",
            json={"first_name": "Bob", "last_initial": "C", "phone": "555"},
        )
        assert resp.status_code == 200


class TestUnbookRoute:
    @patch("app.send_ntfy")
    @patch("app.get_db")
    def test_unbook_success(self, mock_get_db, mock_ntfy, client):
        mock_cursor, mock_db = make_mock_db()
        mock_cursor.rowcount = 1
        mock_cursor.fetchone.return_value = None  # no waitlist entry
        mock_get_db.return_value = mock_db

        resp = client.delete("/api/events/test-uid-1/book")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "(0/1)" in data["title"]
        assert data["waitlistCount"] == 0
        mock_ntfy.assert_called_once()

    @patch("app.send_ntfy")
    @patch("app.get_db")
    def test_unbook_promotes_from_waitlist(self, mock_get_db, mock_ntfy, client):
        mock_cursor, mock_db = make_mock_db()
        mock_cursor.rowcount = 1
        mock_cursor.fetchone.side_effect = [
            {"id": 42, "first_name": "Bob", "last_initial": "C", "phone": "999"},
            {"cnt": 0},  # COUNT after removal
        ]
        mock_get_db.return_value = mock_db

        resp = client.delete("/api/events/test-uid-1/book")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["promoted"] is True
        assert "Bob C." == data["bookedBy"]
        assert data["waitlistCount"] == 0
        mock_ntfy.assert_called_once()

    @patch("app.get_db")
    def test_unbook_not_found(self, mock_get_db, client):
        mock_cursor, mock_db = make_mock_db()
        mock_cursor.rowcount = 0
        mock_get_db.return_value = mock_db

        resp = client.delete("/api/events/nonexistent/book")
        assert resp.status_code == 404

    @patch("app.get_db")
    def test_unbook_db_error(self, mock_get_db, client):
        mock_cursor, mock_db = make_mock_db()
        mock_cursor.execute.side_effect = psycopg2.Error("db broken")
        mock_get_db.return_value = mock_db

        resp = client.delete("/api/events/uid/book")
        assert resp.status_code == 500
        mock_db.rollback.assert_called_once()


class TestWaitlistRoute:
    @patch("app.get_db")
    def test_leave_missing_phone(self, mock_get_db, client):
        resp = client.delete("/api/events/uid/waitlist", json={})
        assert resp.status_code == 400
        assert "phone" in resp.get_json()["error"]

    @patch("app.get_db")
    def test_leave_not_found(self, mock_get_db, client):
        mock_cursor, mock_db = make_mock_db()
        mock_cursor.rowcount = 0
        mock_cursor.fetchone.return_value = (0,)
        mock_get_db.return_value = mock_db

        resp = client.delete("/api/events/uid/waitlist", json={"phone": "555"})
        assert resp.status_code == 404

    @patch("app.get_db")
    def test_leave_success(self, mock_get_db, client):
        mock_cursor, mock_db = make_mock_db()
        mock_cursor.rowcount = 1
        mock_cursor.fetchone.return_value = (0,)
        mock_get_db.return_value = mock_db

        resp = client.delete("/api/events/uid/waitlist", json={"phone": "555"})
        assert resp.status_code == 200
        assert resp.get_json()["waitlistCount"] == 0

    @patch("app.get_db")
    def test_leave_db_error(self, mock_get_db, client):
        mock_cursor, mock_db = make_mock_db()
        mock_cursor.execute.side_effect = psycopg2.Error("db broken")
        mock_get_db.return_value = mock_db

        resp = client.delete("/api/events/uid/waitlist", json={"phone": "555"})
        assert resp.status_code == 500
        mock_db.rollback.assert_called_once()


class TestDbLifecycle:
    @patch("app.psycopg2.connect")
    def test_get_db_creates_connection(self, mock_connect, app):
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn
        with app.test_request_context():
            from flask import g
            from app import get_db
            conn = get_db()
            assert conn is mock_conn
            conn2 = get_db()
            assert conn2 is conn
            mock_connect.assert_called_once()

    @patch("app.psycopg2.connect")
    def test_close_db_closes_connection(self, mock_connect, app):
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn
        with app.test_request_context():
            from app import get_db, close_db
            get_db()
            close_db()
            mock_conn.close.assert_called_once()

    def test_close_db_no_connection(self, app):
        with app.test_request_context():
            from app import close_db
            close_db()


class TestFetchEventsEdgeCases:
    @patch("app.requests.get")
    @patch("app.get_db")
    def test_event_without_end(self, mock_get_db, mock_requests_get, client):
        ical_no_end = b"""\
BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
DTSTART:20260401T100000Z
SUMMARY:No End Event
UID:no-end-uid
END:VEVENT
END:VCALENDAR
"""
        mock_requests_get.return_value = _ical_mock(ical_no_end)
        _, mock_db = make_mock_db()
        mock_get_db.return_value = mock_db

        resp = client.get("/api/events")
        events = resp.get_json()
        ev = [e for e in events if e["id"] == "no-end-uid"][0]
        assert ev["end"] is None

    @patch("app.requests.get")
    @patch("app.get_db")
    def test_event_no_description_no_location(self, mock_get_db, mock_requests_get, client):
        ical_bare = b"""\
BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
DTSTART:20260501T100000Z
DTEND:20260501T110000Z
SUMMARY:Bare Event
UID:bare-uid
END:VEVENT
END:VCALENDAR
"""
        mock_requests_get.return_value = _ical_mock(ical_bare)
        _, mock_db = make_mock_db()
        mock_get_db.return_value = mock_db

        resp = client.get("/api/events")
        events = resp.get_json()
        ev = [e for e in events if e["id"] == "bare-uid"][0]
        assert "description" not in ev
        assert "location" not in ev
