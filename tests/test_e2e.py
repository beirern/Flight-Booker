"""End-to-end Playwright tests against a live Flask server with a real Postgres DB.

The DATABASE_URL environment variable must point to a live Postgres instance
(provided by docker-compose.test.yml in CI / local Docker runs).
"""

import os
import threading
import time

import psycopg2
import pytest
from unittest.mock import MagicMock, patch

from tests.conftest import SAMPLE_ICAL

pytestmark = pytest.mark.e2e

_DB_URL = os.environ.get("DATABASE_URL", "postgresql://flightbooker:testpassword@localhost:5432/flightbooker")


@pytest.fixture(scope="session")
def server_url():
    """Initialise the DB schema, start the Flask dev server, and return its URL."""
    os.environ["DATABASE_URL"] = _DB_URL

    from app import app as flask_app, init_db
    flask_app.config["TESTING"] = True
    init_db()

    mock_resp = MagicMock()
    mock_resp.content = SAMPLE_ICAL
    mock_resp.raise_for_status = MagicMock()

    from werkzeug.serving import make_server
    server = make_server("127.0.0.1", 5199, flask_app)
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True

    with (
        patch("app.requests.get", return_value=mock_resp),
        patch("app.send_ntfy"),
    ):
        thread.start()
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
def clean_db():
    """Truncate both tables before each test that uses the DB."""
    conn = psycopg2.connect(_DB_URL)
    with conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE bookings, waitlist RESTART IDENTITY")
    conn.close()
    yield


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
    page.locator("#modal-overlay").click(position={"x": 10, "y": 10})
    modal = page.locator("#modal-overlay")
    assert "open" not in (modal.get_attribute("class") or "")


def test_booking_validation(page, server_url, clean_db):
    page.goto(f"{server_url}/calendar")
    page.wait_for_selector(".fc-event", timeout=10000)

    _navigate_to_month(page, "April 2026")
    page.wait_for_selector(".fc-event", timeout=10000)
    page.locator(".fc-event").first.click()
    page.wait_for_selector("#modal-overlay.open")

    page.locator("#book-submit").click()
    error = page.locator("#book-error")
    assert error.is_visible()
    assert "fill in all fields" in error.text_content().lower()


def test_book_flight(page, server_url, clean_db):
    page.goto(f"{server_url}/calendar")
    page.wait_for_selector(".fc-event", timeout=10000)

    _navigate_to_month(page, "April 2026")
    page.wait_for_selector(".fc-event", timeout=10000)
    page.locator(".fc-event").first.click()
    page.wait_for_selector("#modal-overlay.open")

    page.locator("#book-first-name").fill("Alice")
    page.locator("#book-last-initial").fill("B")
    page.locator("#book-phone").fill("555-1234")
    page.locator("#book-submit").click()

    page.wait_for_selector("#booked-section:visible", timeout=5000)
    booked_name = page.locator("#booked-by-name")
    assert "Alice" in booked_name.text_content()


def test_unbook_flight(page, server_url, clean_db):
    page.goto(f"{server_url}/calendar")
    page.wait_for_selector(".fc-event", timeout=10000)

    _navigate_to_month(page, "April 2026")
    page.wait_for_selector(".fc-event", timeout=10000)
    page.locator(".fc-event").first.click()
    page.wait_for_selector("#modal-overlay.open")

    page.locator("#book-first-name").fill("Bob")
    page.locator("#book-last-initial").fill("C")
    page.locator("#book-phone").fill("555-5678")
    page.locator("#book-submit").click()
    page.wait_for_selector("#booked-section:visible", timeout=5000)

    page.locator("#unbook-btn").click()
    page.wait_for_selector("#book-form-section:visible", timeout=5000)
    assert page.locator("#book-first-name").is_visible()


def test_past_event_shows_notice(page, server_url, clean_db):
    page.goto(f"{server_url}/calendar")
    page.wait_for_selector(".fc", timeout=10000)

    _navigate_to_month(page, "January 2020")
    page.wait_for_selector(".fc-event", timeout=10000)
    page.locator(".fc-event").first.click()
    page.wait_for_selector("#modal-overlay.open")

    past_notice = page.locator("#past-notice")
    assert past_notice.is_visible()
    assert "passed" in past_notice.text_content().lower()


def test_past_event_has_opacity_class(page, server_url, clean_db):
    page.goto(f"{server_url}/calendar")
    page.wait_for_selector(".fc", timeout=10000)

    _navigate_to_month(page, "January 2020")
    page.wait_for_selector(".fc-event", timeout=10000)
    event = page.locator(".fc-event").first
    assert "past-event" in (event.get_attribute("class") or "")


def test_calendar_view_switching(page, server_url):
    page.goto(f"{server_url}/calendar")
    page.wait_for_selector(".fc", timeout=10000)

    page.locator(".fc-timeGridWeek-button").click()
    assert page.locator(".fc-timegrid").is_visible()

    page.locator(".fc-timeGridDay-button").click()
    assert page.locator(".fc-timegrid").is_visible()

    page.locator(".fc-dayGridMonth-button").click()
    assert page.locator(".fc-daygrid").is_visible()


def test_location_link(page, server_url, clean_db):
    page.goto(f"{server_url}/calendar")
    page.wait_for_selector(".fc-event", timeout=10000)

    _navigate_to_month(page, "April 2026")
    page.wait_for_selector(".fc-event", timeout=10000)
    page.locator(".fc-event").first.click()
    page.wait_for_selector("#modal-overlay.open")

    loc_row = page.locator("#modal-location-row")
    if loc_row.is_visible():
        link = loc_row.locator("a")
        href = link.get_attribute("href")
        assert "google.com/maps" in href


def test_double_book_shows_waitlist_form(page, server_url, clean_db):
    """After a flight is booked, reopening shows the Join Waitlist form."""
    page.goto(f"{server_url}/calendar")
    page.wait_for_selector(".fc-event", timeout=10000)

    _navigate_to_month(page, "April 2026")
    page.wait_for_selector(".fc-event", timeout=10000)
    page.locator(".fc-event").first.click()
    page.wait_for_selector("#modal-overlay.open")

    page.locator("#book-first-name").fill("Alice")
    page.locator("#book-last-initial").fill("B")
    page.locator("#book-phone").fill("555-1234")
    page.locator("#book-submit").click()
    page.wait_for_selector("#booked-section:visible", timeout=5000)

    # Close and reopen — event should show booked badge + Join Waitlist form
    page.locator("#modal-close").click()
    page.locator(".fc-event").first.click()
    page.wait_for_selector("#modal-overlay.open")
    assert page.locator("#booked-section").is_visible()
    assert page.locator("#book-form-section").is_visible()
    assert page.locator("#book-submit").text_content() == "Join Waitlist"


def test_join_waitlist(page, server_url, clean_db):
    """Book a flight, then a second person joins the waitlist."""
    page.goto(f"{server_url}/calendar")
    page.wait_for_selector(".fc-event", timeout=10000)
    _navigate_to_month(page, "April 2026")
    page.wait_for_selector(".fc-event", timeout=10000)
    page.locator(".fc-event").first.click()
    page.wait_for_selector("#modal-overlay.open")

    # First person books
    page.locator("#book-first-name").fill("Alice")
    page.locator("#book-last-initial").fill("B")
    page.locator("#book-phone").fill("555-1234")
    page.locator("#book-submit").click()
    page.wait_for_selector("#booked-section:visible", timeout=5000)

    # Second person joins waitlist via the form shown below the booked badge
    page.locator("#book-first-name").fill("Carol")
    page.locator("#book-last-initial").fill("D")
    page.locator("#book-phone").fill("555-9999")
    page.locator("#book-submit").click()

    page.wait_for_selector("#waitlisted-section:visible", timeout=5000)
    assert page.locator("#waitlist-position").text_content() == "1"


def test_leave_waitlist(page, server_url, clean_db):
    """Join the waitlist then leave it."""
    page.goto(f"{server_url}/calendar")
    page.wait_for_selector(".fc-event", timeout=10000)
    _navigate_to_month(page, "April 2026")
    page.wait_for_selector(".fc-event", timeout=10000)
    page.locator(".fc-event").first.click()
    page.wait_for_selector("#modal-overlay.open")

    # Book first person
    page.locator("#book-first-name").fill("Alice")
    page.locator("#book-last-initial").fill("B")
    page.locator("#book-phone").fill("555-1234")
    page.locator("#book-submit").click()
    page.wait_for_selector("#booked-section:visible", timeout=5000)

    # Second person joins waitlist
    page.locator("#book-first-name").fill("Carol")
    page.locator("#book-last-initial").fill("D")
    page.locator("#book-phone").fill("555-9999")
    page.locator("#book-submit").click()
    page.wait_for_selector("#waitlisted-section:visible", timeout=5000)

    # Leave the waitlist
    page.locator("#leave-waitlist-btn").click()
    page.wait_for_selector("#book-form-section:visible", timeout=5000)
    assert page.locator("#waitlisted-section").is_hidden()


def test_cancel_promotes_waitlist(page, server_url, clean_db):
    """Cancel a booking and verify the waitlist person is automatically promoted."""
    page.goto(f"{server_url}/calendar")
    page.wait_for_selector(".fc-event", timeout=10000)
    _navigate_to_month(page, "April 2026")
    page.wait_for_selector(".fc-event", timeout=10000)
    page.locator(".fc-event").first.click()
    page.wait_for_selector("#modal-overlay.open")

    # Book first person
    page.locator("#book-first-name").fill("Alice")
    page.locator("#book-last-initial").fill("B")
    page.locator("#book-phone").fill("555-1234")
    page.locator("#book-submit").click()
    page.wait_for_selector("#booked-section:visible", timeout=5000)

    # Second person joins waitlist
    page.locator("#book-first-name").fill("Bob")
    page.locator("#book-last-initial").fill("C")
    page.locator("#book-phone").fill("555-5678")
    page.locator("#book-submit").click()
    page.wait_for_selector("#waitlisted-section:visible", timeout=5000)

    # Close modal and reopen to get fresh state
    page.locator("#modal-close").click()
    page.locator(".fc-event").first.click()
    page.wait_for_selector("#modal-overlay.open")

    # Alice cancels
    page.locator("#unbook-btn").click()

    # Bob should now be shown as the booked person (wait for name to update)
    page.locator("#booked-by-name").filter(has_text="Bob").wait_for(timeout=5000)
    assert "Bob" in page.locator("#booked-by-name").text_content()


def test_waitlist_count_displayed(page, server_url, clean_db):
    """When someone is on the waitlist, the count shows in the modal title."""
    page.goto(f"{server_url}/calendar")
    page.wait_for_selector(".fc-event", timeout=10000)
    _navigate_to_month(page, "April 2026")
    page.wait_for_selector(".fc-event", timeout=10000)
    page.locator(".fc-event").first.click()
    page.wait_for_selector("#modal-overlay.open")

    # Book first person
    page.locator("#book-first-name").fill("Alice")
    page.locator("#book-last-initial").fill("B")
    page.locator("#book-phone").fill("555-1234")
    page.locator("#book-submit").click()
    page.wait_for_selector("#booked-section:visible", timeout=5000)

    # Second person joins waitlist
    page.locator("#book-first-name").fill("Bob")
    page.locator("#book-last-initial").fill("C")
    page.locator("#book-phone").fill("555-5678")
    page.locator("#book-submit").click()
    page.wait_for_selector("#waitlisted-section:visible", timeout=5000)

    # Close and reopen — title should show +1 WL
    page.locator("#modal-close").click()
    page.locator(".fc-event").first.click()
    page.wait_for_selector("#modal-overlay.open")
    assert "+1 WL" in page.locator("#modal-title").text_content()


def _navigate_to_month(page, target_month_year: str):
    """Click prev/next until FullCalendar shows the target month/year."""
    month_btn = page.locator(".fc-dayGridMonth-button")
    if month_btn.is_visible():
        month_btn.click()

    for _ in range(200):
        title = page.locator(".fc-toolbar-title").text_content().strip()
        if target_month_year.lower() in title.lower():
            return
        target_parts = target_month_year.split()
        target_year = int(target_parts[-1])
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
