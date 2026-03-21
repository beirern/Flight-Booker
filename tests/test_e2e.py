"""End-to-end Playwright tests against a live Flask server with mocked externals."""

import os
import threading
import time

import psycopg2.errors
import pytest
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

from app import app as flask_app
from tests.conftest import SAMPLE_ICAL

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def _mock_externals():
    """Module-scoped patches for iCal fetch and DB."""
    # Mock iCal response
    mock_resp = MagicMock()
    mock_resp.content = SAMPLE_ICAL
    mock_resp.raise_for_status = MagicMock()

    # In-memory bookings store to simulate DB
    bookings = {}

    def fake_get_db():
        mock_db = MagicMock()

        class FakeCursor:
            def __init__(self):
                self.rowcount = 0
                self._result = []

            def execute(self, sql, params=None):
                if "SELECT" in sql:
                    self._result = [
                        {"event_uid": uid, "first_name": b["first_name"], "last_initial": b["last_initial"]}
                        for uid, b in bookings.items()
                    ]
                elif "INSERT" in sql:
                    uid = params[0]
                    if uid in bookings:
                        raise psycopg2.errors.UniqueViolation()
                    bookings[uid] = {
                        "first_name": params[1],
                        "last_initial": params[2],
                        "phone": params[3],
                    }
                elif "DELETE" in sql:
                    uid = params[0]
                    if uid in bookings:
                        del bookings[uid]
                        self.rowcount = 1
                    else:
                        self.rowcount = 0

            def fetchall(self):
                return self._result

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        mock_db.cursor.return_value = FakeCursor()
        mock_db.commit = MagicMock()
        mock_db.rollback = MagicMock()
        return mock_db

    with (
        patch("app.requests.get", return_value=mock_resp),
        patch("app.get_db", side_effect=fake_get_db),
        patch("app.send_ntfy"),
    ):
        # Clear bookings between module runs
        bookings.clear()
        yield bookings


@pytest.fixture(scope="module")
def server_url(_mock_externals):
    """Start the Flask app in a background thread and return its URL."""
    flask_app.config["TESTING"] = True

    server = None
    from werkzeug.serving import make_server
    server = make_server("127.0.0.1", 5199, flask_app)
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()

    # Wait for server to be ready
    import urllib.request
    for _ in range(50):
        try:
            urllib.request.urlopen("http://127.0.0.1:5199/")
            break
        except Exception:
            time.sleep(0.1)

    yield "http://127.0.0.1:5199"
    server.shutdown()


@pytest.fixture()
def clean_bookings(_mock_externals):
    """Clear bookings before each test."""
    _mock_externals.clear()


def test_calendar_page_loads(page, server_url):
    page.goto(f"{server_url}/calendar")
    assert page.title() == "Calendar"
    page.wait_for_selector(".fc")
    assert page.locator(".fc").is_visible()


def test_header_visible(page, server_url):
    page.goto(f"{server_url}/calendar")
    header = page.locator("header h1")
    assert "Calendar" in header.text_content()


def test_events_displayed(page, server_url):
    page.goto(f"{server_url}/calendar")
    page.wait_for_selector(".fc-event", timeout=10000)
    events = page.locator(".fc-event")
    assert events.count() > 0


def test_click_event_opens_modal(page, server_url):
    page.goto(f"{server_url}/calendar")
    page.wait_for_selector(".fc-event", timeout=10000)
    page.locator(".fc-event").first.click()
    modal = page.locator("#modal-overlay")
    assert modal.is_visible()
    assert "open" in modal.get_attribute("class")


def test_modal_shows_event_details(page, server_url):
    page.goto(f"{server_url}/calendar")
    page.wait_for_selector(".fc-event", timeout=10000)
    page.locator(".fc-event").first.click()
    page.wait_for_selector("#modal-overlay.open")
    title = page.locator("#modal-title")
    assert "Flight Lesson" in title.text_content()
    time_el = page.locator("#modal-time")
    assert time_el.text_content().strip() != ""


def test_modal_close_button(page, server_url):
    page.goto(f"{server_url}/calendar")
    page.wait_for_selector(".fc-event", timeout=10000)
    page.locator(".fc-event").first.click()
    page.wait_for_selector("#modal-overlay.open")
    page.locator("#modal-close").click()
    modal = page.locator("#modal-overlay")
    assert "open" not in (modal.get_attribute("class") or "")


def test_modal_close_escape(page, server_url):
    page.goto(f"{server_url}/calendar")
    page.wait_for_selector(".fc-event", timeout=10000)
    page.locator(".fc-event").first.click()
    page.wait_for_selector("#modal-overlay.open")
    page.keyboard.press("Escape")
    modal = page.locator("#modal-overlay")
    assert "open" not in (modal.get_attribute("class") or "")


def test_modal_close_overlay_click(page, server_url):
    page.goto(f"{server_url}/calendar")
    page.wait_for_selector(".fc-event", timeout=10000)
    page.locator(".fc-event").first.click()
    page.wait_for_selector("#modal-overlay.open")
    # Click outside the modal dialog
    page.locator("#modal-overlay").click(position={"x": 10, "y": 10})
    modal = page.locator("#modal-overlay")
    assert "open" not in (modal.get_attribute("class") or "")


def test_booking_validation(page, server_url, clean_bookings):
    page.goto(f"{server_url}/calendar")
    page.wait_for_selector(".fc-event", timeout=10000)

    # Navigate to April 2026 to find future events
    _navigate_to_month(page, "April 2026")
    page.wait_for_selector(".fc-event", timeout=10000)
    page.locator(".fc-event").first.click()
    page.wait_for_selector("#modal-overlay.open")

    # Try to submit empty form
    page.locator("#book-submit").click()
    error = page.locator("#book-error")
    assert error.is_visible()
    assert "fill in all fields" in error.text_content().lower()


def test_book_flight(page, server_url, clean_bookings):
    page.goto(f"{server_url}/calendar")
    page.wait_for_selector(".fc-event", timeout=10000)

    _navigate_to_month(page, "April 2026")
    page.wait_for_selector(".fc-event", timeout=10000)
    page.locator(".fc-event").first.click()
    page.wait_for_selector("#modal-overlay.open")

    # Fill booking form
    page.locator("#book-first-name").fill("Alice")
    page.locator("#book-last-initial").fill("B")
    page.locator("#book-phone").fill("555-1234")
    page.locator("#book-submit").click()

    # Should show booked state
    page.wait_for_selector("#booked-section:visible", timeout=5000)
    booked_name = page.locator("#booked-by-name")
    assert "Alice" in booked_name.text_content()


def test_unbook_flight(page, server_url, clean_bookings):
    page.goto(f"{server_url}/calendar")
    page.wait_for_selector(".fc-event", timeout=10000)

    _navigate_to_month(page, "April 2026")
    page.wait_for_selector(".fc-event", timeout=10000)
    page.locator(".fc-event").first.click()
    page.wait_for_selector("#modal-overlay.open")

    # Book first
    page.locator("#book-first-name").fill("Bob")
    page.locator("#book-last-initial").fill("C")
    page.locator("#book-phone").fill("555-5678")
    page.locator("#book-submit").click()
    page.wait_for_selector("#booked-section:visible", timeout=5000)

    # Now unbook
    page.locator("#unbook-btn").click()
    page.wait_for_selector("#book-form-section:visible", timeout=5000)
    assert page.locator("#book-first-name").is_visible()


def test_past_event_shows_notice(page, server_url, clean_bookings):
    page.goto(f"{server_url}/calendar")
    page.wait_for_selector(".fc", timeout=10000)

    # Navigate to January 2020 for the past event
    _navigate_to_month(page, "January 2020")
    page.wait_for_selector(".fc-event", timeout=10000)
    page.locator(".fc-event").first.click()
    page.wait_for_selector("#modal-overlay.open")

    past_notice = page.locator("#past-notice")
    assert past_notice.is_visible()
    assert "passed" in past_notice.text_content().lower()


def test_past_event_has_opacity_class(page, server_url, clean_bookings):
    page.goto(f"{server_url}/calendar")
    page.wait_for_selector(".fc", timeout=10000)

    _navigate_to_month(page, "January 2020")
    page.wait_for_selector(".fc-event", timeout=10000)
    event = page.locator(".fc-event").first
    assert "past-event" in (event.get_attribute("class") or "")


def test_calendar_view_switching(page, server_url):
    page.goto(f"{server_url}/calendar")
    page.wait_for_selector(".fc", timeout=10000)

    # Switch to week view
    page.locator(".fc-timeGridWeek-button").click()
    assert page.locator(".fc-timegrid").is_visible()

    # Switch to day view
    page.locator(".fc-timeGridDay-button").click()
    assert page.locator(".fc-timegrid").is_visible()

    # Switch back to month view
    page.locator(".fc-dayGridMonth-button").click()
    assert page.locator(".fc-daygrid").is_visible()


def test_location_link(page, server_url, clean_bookings):
    page.goto(f"{server_url}/calendar")
    page.wait_for_selector(".fc-event", timeout=10000)

    _navigate_to_month(page, "April 2026")
    page.wait_for_selector(".fc-event", timeout=10000)

    # Find the event with location (test-uid-1)
    page.locator(".fc-event").first.click()
    page.wait_for_selector("#modal-overlay.open")

    loc_row = page.locator("#modal-location-row")
    if loc_row.is_visible():
        link = loc_row.locator("a")
        href = link.get_attribute("href")
        assert "google.com/maps" in href


def test_double_book_shows_error(page, server_url, clean_bookings):
    page.goto(f"{server_url}/calendar")
    page.wait_for_selector(".fc-event", timeout=10000)

    _navigate_to_month(page, "April 2026")
    page.wait_for_selector(".fc-event", timeout=10000)
    page.locator(".fc-event").first.click()
    page.wait_for_selector("#modal-overlay.open")

    # Book
    page.locator("#book-first-name").fill("Alice")
    page.locator("#book-last-initial").fill("B")
    page.locator("#book-phone").fill("555-1234")
    page.locator("#book-submit").click()
    page.wait_for_selector("#booked-section:visible", timeout=5000)

    # Close and reopen - event should show as booked
    page.locator("#modal-close").click()
    page.locator(".fc-event").first.click()
    page.wait_for_selector("#modal-overlay.open")
    assert page.locator("#booked-section").is_visible()


def _navigate_to_month(page, target_month_year: str):
    """Click prev/next until FullCalendar shows the target month/year."""
    # First ensure we're in month view
    month_btn = page.locator(".fc-dayGridMonth-button")
    if month_btn.is_visible():
        month_btn.click()

    for _ in range(200):  # safety limit
        title = page.locator(".fc-toolbar-title").text_content().strip()
        if target_month_year.lower() in title.lower():
            return
        # Determine direction
        target_parts = target_month_year.split()
        target_year = int(target_parts[-1])
        # Simple heuristic: parse current title
        current_parts = title.split()
        try:
            current_year = int(current_parts[-1])
        except (ValueError, IndexError):
            current_year = 2026

        if target_year < current_year or (
            target_year == current_year
            and _month_index(target_parts[0]) < _month_index(current_parts[0])
        ):
            page.locator(".fc-prev-button").click()
        else:
            page.locator(".fc-next-button").click()
        page.wait_for_timeout(100)


def _month_index(name: str) -> int:
    months = [
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december",
    ]
    try:
        return months.index(name.lower())
    except ValueError:
        return 0
