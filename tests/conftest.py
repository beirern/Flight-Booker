import os
import pytest
import psycopg2
from unittest.mock import MagicMock

# Set DATABASE_URL before importing app so module-level code doesn't fail
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

from app import app as flask_app


@pytest.fixture()
def app():
    flask_app.config["TESTING"] = True
    yield flask_app


@pytest.fixture()
def client(app):
    return app.test_client()


def make_mock_db(fetchall_return=None):
    """Return (mock_cursor, mock_db) with context manager support on cursor."""
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [] if fetchall_return is None else fetchall_return
    mock_db = MagicMock()
    mock_db.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_db.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return mock_cursor, mock_db


# ---------------------------------------------------------------------------
# Sample iCal data
# ---------------------------------------------------------------------------

SAMPLE_ICAL = b"""\
BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
DTSTART:20260401T100000Z
DTEND:20260401T110000Z
SUMMARY:Flight Lesson
UID:test-uid-1
DESCRIPTION:Flight Type: Training. Course: PPL. Lesson: Takeoffs. Aircraft: C172
LOCATION:KPAO Palo Alto Airport
END:VEVENT
BEGIN:VEVENT
DTSTART:20260402T140000Z
DTEND:20260402T150000Z
SUMMARY:Flight Lesson
UID:test-uid-2
DESCRIPTION:Flight Type: Leisure. Aircraft: C152
END:VEVENT
BEGIN:VEVENT
DTSTART:20200101T100000Z
DTEND:20200101T110000Z
SUMMARY:Flight Lesson
UID:test-uid-past
DESCRIPTION:Flight Type: Training. Course: PPL. Lesson: Landings. Aircraft: C172
END:VEVENT
BEGIN:VEVENT
DTSTART:20260601
DTEND:20260602
SUMMARY:All Day Event
UID:test-uid-allday
END:VEVENT
END:VCALENDAR
"""
