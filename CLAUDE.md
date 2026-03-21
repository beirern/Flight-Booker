# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the App

**Development** (hot reload on file changes):
```bash
docker compose -f docker-compose.dev.yml up
```

**Production**:
```bash
docker compose up
```

Both run on port **5001**. Requires a `.env` file with `POSTGRES_PASSWORD` set (see `.env.example`).

The `DATABASE_URL` is constructed automatically by docker-compose:
```
postgresql://flightbooker:${POSTGRES_PASSWORD}@db:5432/flightbooker
```

## Architecture

This is a minimal single-file Flask app (`app.py`) with one HTML template (`templates/calendar.html`).

**Data flow:**
1. `/api/events` — fetches flight lesson events from a hardcoded public Google Calendar iCal URL, then joins with the local `bookings` and `waitlist` PostgreSQL tables to enrich each event with booking/waitlist status
2. `POST /api/events/<uid>/book` — if the event is unbooked, inserts into `bookings`; if already booked, falls back to inserting into `waitlist` (same form, different response)
3. `DELETE /api/events/<uid>/book` — removes the booking; automatically promotes the first waitlist entry (by insertion order) to a booking if one exists
4. `DELETE /api/events/<uid>/waitlist` — removes a specific person from the waitlist (identified by phone number)
5. The frontend is a single page using FullCalendar 6.1.15 (CDN) + vanilla JS. A modal opens on event click, showing event details and a booking/waitlist form.

**Database schema** (auto-created on startup via `init_db()`):
```sql
bookings(event_uid PK, first_name, last_initial, phone, created_at TIMESTAMPTZ)
waitlist(id SERIAL PK, event_uid, first_name, last_initial, phone, created_at TIMESTAMPTZ, UNIQUE(event_uid, phone))
```

**Key constraint:** There is no authentication. The calendar is fully public.

## Testing

All tests run via Docker using `docker-compose.test.yml`, which spins up a real Postgres container alongside the test runner.

**Unit tests:**
```bash
docker compose -f docker-compose.test.yml run --rm test python -m pytest tests/test_unit.py -q
```

**E2e tests** (Playwright + Chromium + real Postgres):
```bash
docker compose -f docker-compose.test.yml run --rm test python -m pytest tests/test_e2e.py --browser chromium
```

**All tests with coverage:**
```bash
docker compose -f docker-compose.test.yml run --rm test python -m pytest tests/test_unit.py tests/test_e2e.py --browser chromium
```

Tear down the DB container when done:
```bash
docker compose -f docker-compose.test.yml down
```

Coverage must stay at **95%+** (enforced by `pyproject.toml`).

## Timezone Handling

The backend (`parse_dt()`) converts all datetimes to `America/Los_Angeles` before sending them as ISO 8601 strings. FullCalendar on the frontend is configured with `timeZone: "local"` so the calendar and event modal both display in the viewer's local browser timezone.
