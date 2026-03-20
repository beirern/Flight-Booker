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
1. `/api/events` — fetches flight lesson events from a hardcoded public Google Calendar iCal URL, then joins with the local `bookings` PostgreSQL table to enrich each event with booking status
2. `POST /api/events/<uid>/book` — inserts a booking row; `event_uid` is the primary key so double-booking is prevented at the DB level (returns 409 on conflict)
3. The frontend is a single page using FullCalendar 6.1.15 (CDN) + vanilla JS. A modal opens on event click, showing event details and a booking form.

**Database schema** (auto-created on startup via `init_db()`):
```sql
bookings(event_uid PK, first_name, last_initial, phone, created_at TIMESTAMPTZ)
```

**Key constraint:** There is no authentication. The calendar is fully public.

## Timezone Handling

The backend (`parse_dt()`) converts all datetimes to `America/Los_Angeles` before sending them as ISO 8601 strings. FullCalendar on the frontend is configured with `timeZone: "local"` so the calendar and event modal both display in the viewer's local browser timezone.
