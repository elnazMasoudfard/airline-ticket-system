# ✈️ Airline Ticket Booking System

A full-featured flight ticket booking platform built with **Django** and **PostgreSQL**, featuring a hold-then-pay booking flow with atomic seat reservation, wallet-based payments backed by an append-only ledger, a staff management dashboard, and a custom right-to-left (RTL) interface designed for Persian-speaking users.

This project was built as a university web programming course project, with an emphasis on production-grade practices: normalized data modeling, race-condition-safe booking logic, structured logging, automated tests, and a clean, maintainable Django architecture.

---

## ✨ Key Features

### Booking & Payments
- **Flight search & browsing** — search by origin, destination, date and **number of passengers** (only flights where one class can still seat the whole group are listed), with **live Ajax filtering and pagination** (no full page reload; falls back gracefully to normal form submission if JS is unavailable)
- **Multi-class seating** — Economy / Business / First class per flight, each with its own price multiplier and capacity
- **Specific seat selection** — users pick exact seats from a visual seat map; group bookings are validated to ensure seats are adjacent in the same row
- **Hold-then-pay booking** — choosing seats creates a *pending* reservation that holds those seats for a limited time (30 minutes by default). Passenger details are entered next, then the reservation is paid from the wallet. Unpaid reservations expire automatically and their seats are released
- **Atomic seat reservation** — uses `select_for_update()` and database-level `F()` expressions to prevent overbooking or double-booking under concurrent requests
- **Wallet-based payments** — users top up an in-app wallet (simulated, no real payment gateway) and pay for reservations from it; every balance change is atomic and recorded in a ledger
- **Passenger rules** — exactly one passenger per seat, Persian/Arabic digits in national IDs are normalised, an optional national-ID checksum validation can be switched on, and **one person can hold only one seat per flight** (checked across all users)
- **Editable passenger details** — typos can be corrected while the reservation is pending, and after payment until the flight turns active, one hour before departure (the number of passengers can never change)
- **Cancellation with penalty** — an unpaid reservation can always be cancelled. A paid reservation can be cancelled until one hour before departure; the refund is calculated from the flight's cancellation penalty percentage. If the **airline cancels the flight**, every reservation is refunded 100 %

### Accounts & Security
- **Custom user model** — extends Django's `AbstractUser` with a wallet balance and phone/email verification flags; emails are stored lowercase and duplicates are rejected case-insensitively
- **Wallet ledger** — every deposit, payment, refund and adjustment writes an immutable `WalletTransaction` (amount, balance before/after, booking reference). Ledger rows cannot be edited or deleted, and the balance is read-only in the Django admin
- **Safe login redirects** — users can log in with username or email; a `?next=` target is honoured only when it points to this site (no open redirects), and signed-in users are redirected away from the login/register pages
- **Profile editing** — users can update their name, email, and phone number; changing email or phone resets its verification status, **invalidates every pending verification link/code**, and (for email) triggers a fresh verification link
- **Email verification** — a one-time link (24h expiry) sent via a configurable email backend (console backend by default; switches to real SMTP via `.env` for a live demo)
- **Simulated phone (SMS) verification** — a 6-digit one-time code flow with a lockout after 5 wrong attempts (15 minutes); since no real SMS gateway is connected, the code is logged instead of texted
- **Structured logging** — rotating file handlers (`general.log`, `errors.log`, `security.log`) with a dedicated logger per app, covering auth events, wallet transactions, booking lifecycle, and staff actions. Client IPs are only taken from `X-Forwarded-For` when explicitly trusted, and user-supplied text is sanitised against log injection. Django admin actions (add/change/delete) are also captured via a signal on `LogEntry`.

### Staff Dashboard
- A separate, staff-only management area (distinct from the Django admin) for:
  - Creating and editing flights with inline seat-class management (changing a class capacity also adds or removes the real seat rows; booked seats are never touched)
  - Bulk seat generation with continuous row numbering across classes (economy → business → first)
  - **Cancelling a whole flight**: every paid reservation is refunded to the user's wallet, unpaid ones are released. The operation is idempotent, so it can safely be repeated if a refund ever fails
  - Viewing all reservations (filtered by paid / pending / cancelled-unpaid / refunded) and users, with drill-down into per-flight booking history
  - A financial overview: gross revenue, refunded amounts, and net revenue, both system-wide and per flight. Only reservations that were really **paid** count as revenue, and pending amounts are shown separately
- **Automatic flight status transitions** — flights move `Scheduled → Active` one hour before departure, and `→ Completed` once arrival time has passed, via a lightweight middleware that runs at most once a minute per process (no external cron/Celery required). Manually cancelled flights are never touched by this logic. Because only `Scheduled` flights can be booked, **sales and cancellations of paid tickets close one hour before departure**
- **Django admin hardened** — reservations, passengers and seats are view-only, `available_seats` is derived from capacity and bookings (never typed by hand), cancelling a flight from the admin triggers the same refunds as the dashboard, and a seat class that holds, or ever received, money cannot be deleted

### Engineering Practices
- **Automated test suite** — unit and integration tests (`python manage.py test`) covering the wallet ledger, registration/login and verification flows, database constraints, seat generation and capacity sync, flight search, the status middleware and management commands, the full booking flow through views (select seats → passengers → pay → cancel/refund), expiry of unpaid reservations, dashboard financials, flight cancellation, and the admin
- **Service layer** — booking, payment, cancellation and expiry live in `tickets/services.py`, so views, the dashboard and the management commands all share one implementation of the business rules
- **Custom model managers/querysets** (`Flight.objects.upcoming()`, `Reservation.objects.active()`, etc.) centralize query logic instead of repeating filters across views
- **DRY templates** — shared partials (`pagination.html`) and custom template tags (`{% flight_status_badge %}`, `{% reservation_status_badge %}`) eliminate duplicated markup across pages
- **Custom RTL UI** — hand-built Persian interface (no frontend framework) with a boarding-pass-inspired visual identity

## 🔄 Booking lifecycle & business rules

```
select seats ──► enter passengers ──► pay from wallet ──► (optional) cancel
     │                  │                    │
     └── PENDING_PAYMENT: seats held for 30 min ──┘── RESERVED (paid)
                        │
                        └── no payment in time ──► CANCELLED (timeout), seats released
```

| Rule | Behaviour |
|---|---|
| Hold time | 30 minutes by default (`TICKETS_PAYMENT_WINDOW_MINUTES`) |
| Seats per reservation | up to 9 by default (`TICKETS_MAX_SEATS_PER_RESERVATION`), all in one row and side by side |
| Open unpaid reservations per user | up to 3 by default (`TICKETS_MAX_PENDING_RESERVATIONS_PER_USER`) |
| Price | locked when the seats are chosen (`final price = base price × class multiplier`), so a later price change does not affect an open reservation |
| Payment | atomic withdrawal from the wallet; refused if the hold expired, passengers are incomplete, the flight is no longer bookable, or the balance is too low |
| Selling window | until one hour before departure (the flight turns `ACTIVE`) |
| Cancelling an unpaid reservation | always allowed, no money involved |
| Cancelling a paid reservation | until one hour before departure; refund = price × (100 − penalty %) ÷ 100. A 100 % penalty refunds nothing |
| Flight cancelled by the airline | every reservation is cancelled; paid ones get a **full** refund, no penalty |
| Passengers | one per seat; one person (national ID) can hold only one active seat per flight; editable until one hour before departure, count fixed |

## 🛠️ Tech Stack

| Layer | Technology |
|---|---|
| Backend | Django (Class-Based Views) |
| Database | PostgreSQL |
| Cache | Django database cache (shared between processes; used for the phone-verification lockout) |
| Frontend | Django Templates, vanilla CSS (RTL, custom design system), vanilla JS (Ajax) |
| Fonts | Vazirmatn (UI text), JetBrains Mono (flight/booking codes) |
| Config | Environment variables via `.env` (`python-decouple`) |

## 🏗️ Architecture

The project is split into five Django apps, each with a single responsibility:

```
airline_system/
├── core/        # Shared abstract base model, admin-action logging signal, custom template tags
├── accounts/    # Custom user model, authentication, wallet + ledger, email/phone verification
├── flights/     # Airports, airlines, routes, flights, seat classes, individual seats, status sync
├── tickets/     # Reservations, passengers, seat-selection / payment / cancellation workflow
└── dashboard/   # Staff-only management area (no models of its own — composes the apps above)
```

### Data model highlights

- `Route` enforces uniqueness and prevents self-referencing routes (origin ≠ destination) at the database level via `CheckConstraint`. `Flight` requires arrival after departure, and `SeatClass` requires `available_seats <= capacity`, also as database constraints.
- `Seat` models individual, selectable seats per class; booking locks specific seats with `select_for_update()` rather than just decrementing a counter. `ReservationSeat` links a reservation to its seats with a one-to-one relation, so a seat can belong to only one reservation.
- `Reservation.total_paid_price` is stored as a price **snapshot** when the seats are chosen: it is the amount to pay while the reservation is pending and the amount paid afterwards. `paid_at` tells whether money was really received, which is what the financial reports use.
- `Reservation.seat_class` uses `on_delete=PROTECT` to preserve financial history. A dedicated admin action can delete a seat class only if none of its reservations is active or was ever paid.
- `CustomUser.deposit()` / `withdraw()` follow the same atomic-update pattern as seat reservation, preventing negative balances under concurrent transactions, and write a `WalletTransaction` row for every change (`balance_after == balance_before + amount`).

### Concurrency & integrity

Booking, payment, cancellation and expiry all run inside transactions that take row locks in one fixed order, so concurrent requests cannot deadlock:

```
Reservation → SeatClass → Seat (ordered by id) → User (wallet)
```

A `Flight` row is locked in only three places: when passenger details are saved (`Reservation → Flight`, so one national ID cannot end up on two active reservations of the same flight), when a whole flight is cancelled, and in the management screens (`Flight → SeatClass → Seat`). The booking flow itself never locks a flight. The full explanation is at the top of `tickets/services.py`.

## 🚀 Getting Started

### Prerequisites

- Python 3.11+
- PostgreSQL 14+

### Installation

```bash
# Clone the repository
git clone https://github.com/elnazMasoudfard/airline-ticket-system.git
cd airline-ticket-system

# Create and activate a virtual environment
python -m venv venv
venv\Scripts\activate      # Windows
source venv/bin/activate   # macOS/Linux

# Install dependencies
pip install -r requirements.txt
```

### Environment setup

Create a `.env` file in the project root (see `.env.example`):

```env
# Database
DB_NAME=airline_db
DB_USER=airline_user
DB_PASSWORD=your_password
DB_HOST=localhost
DB_PORT=5432

# Core
SECRET_KEY=your-django-secret-key
DEBUG=True

# Email (optional — defaults to console backend, no real email is sent)
# To send real email for a live demo, uncomment and fill these in:
# EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
# EMAIL_HOST_USER=your_email@gmail.com
# EMAIL_HOST_PASSWORD=your_gmail_app_password
# DEFAULT_FROM_EMAIL=your_email@gmail.com
```

### Settings checklist

Besides the `.env` values, `airline_system/settings.py` needs these entries:

```python
AUTH_USER_MODEL = 'accounts.CustomUser'
LOGIN_URL = 'accounts:login'          # where anonymous users are sent (the ?next= target is kept)

# Shared between processes, so the phone-verification lockout works with several workers.
# (LocMemCache keeps a separate counter per process.)
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.db.DatabaseCache',
        'LOCATION': 'django_cache_table',
    }
}
```

Optional settings (defaults in parentheses):

| Setting | Meaning |
|---|---|
| `TICKETS_PAYMENT_WINDOW_MINUTES` (30) | how long unpaid seats are held |
| `TICKETS_MAX_SEATS_PER_RESERVATION` (9) | seats per reservation |
| `TICKETS_MAX_PENDING_RESERVATIONS_PER_USER` (3) | open unpaid reservations per user |
| `TICKETS_VALIDATE_NATIONAL_ID_CHECKSUM` (`False`) | enforce the Iranian national-ID check digit (turn on once your data uses real codes) |
| `TRUST_X_FORWARDED_FOR` (`False`) | take the client IP for the security log from `X-Forwarded-For` (only behind a proxy you control) |

### Database setup

```sql
CREATE USER airline_user WITH PASSWORD 'your_password';
CREATE DATABASE airline_db OWNER airline_user;
ALTER USER airline_user CREATEDB;  -- required for running the test suite
```

### Run migrations & start the server

```bash
python manage.py migrate
python manage.py createcachetable      # creates the table used by the database cache
python manage.py createsuperuser
python manage.py runserver
```

Visit `http://127.0.0.1:8000/` for the app, `http://127.0.0.1:8000/admin/` for the Django admin, and `http://127.0.0.1:8000/dashboard/` for the staff dashboard (requires a staff user).

### Scheduled job: releasing unpaid reservations

Unpaid reservations are also expired lazily whenever one of their pages is opened, but seats held by someone who never comes back are only released by the periodic command. Run it **every minute**:

```bash
# Linux/macOS (crontab -e)
* * * * * cd /path/to/airline-ticket-system && venv/bin/python manage.py expire_reservations

# Windows (Task Scheduler), one line in a terminal — adjust the paths
schtasks /Create /SC MINUTE /MO 1 /TN "ExpireReservations" /TR "C:\path\to\venv\Scripts\python.exe C:\path\to\manage.py expire_reservations"
```

If a path contains spaces, create the task in the Task Scheduler window instead: *Program* = the `python.exe` inside `venv\Scripts`, *Arguments* = `manage.py expire_reservations`, *Start in* = the project folder, trigger = repeat every 1 minute.

Flight statuses (`Scheduled → Active → Completed`) are kept up to date by the middleware; `sync_flight_statuses` is only needed if you prefer a real scheduler.

### Running tests

```bash
python manage.py test                                    # everything
python manage.py test accounts flights tickets dashboard # explicit list of apps
```

### Generating seat maps for test flights

After creating a flight with seat classes (via the dashboard or Django admin), run the **"ساخت خودکار صندلی‌ها"** action (available in both places) to auto-generate individual seats with continuous row numbering. If you later change a class capacity, the seat rows are brought in line automatically.

### Useful management commands

```bash
python manage.py expire_reservations              # cancel unpaid reservations whose hold expired and release their seats
python manage.py sync_flight_statuses             # manually trigger the scheduled/active/completed transition
python manage.py recalculate_seat_availability    # reconcile seat counts if data ever drifts out of sync
python manage.py recalculate_seat_availability --dry-run   # only show the differences, change nothing
```

`recalculate_seat_availability` counts paid reservations **and** unpaid ones that are still inside their hold time as holding seats, and releases expired holds first.

## 📁 Project Structure

```
FinalProject/
├── accounts/
│   ├── models.py      # CustomUser, WalletTransaction (ledger), EmailVerificationToken, PhoneVerificationCode
│   ├── admin.py        # View-only ledger, read-only balance
│   ├── forms.py        # Registration, login, profile edit, deposit, phone verification forms
│   ├── views.py        # Auth flow, profile editing, wallet deposit, email/phone verification
│   ├── utils.py        # Client IP and log-injection-safe text for the security log
│   ├── tests.py        # Ledger, registration/login, verification and lockout tests
│   └── urls.py
├── flights/
│   ├── models.py        # Airport, Airline, Route, Flight, SeatClass, Seat
│   ├── forms.py          # Flight search form
│   ├── views.py          # Flight listing (Ajax-aware) & detail views
│   ├── services.py       # Seat generation, seat/capacity sync, flight status sync
│   ├── middleware.py     # Automatic flight status transitions
│   ├── admin.py          # Derived availability, refunds on cancel, view-only seats
│   ├── management/commands/
│   │   ├── sync_flight_statuses.py
│   │   └── recalculate_seat_availability.py
│   ├── tests.py          # Constraints, seats, search, middleware, commands, admin
│   └── urls.py
├── tickets/
│   ├── models.py         # Reservation, Passenger, ReservationSeat
│   ├── forms.py           # Booking & passenger forms (national-ID normalisation/checksum)
│   ├── services.py        # Booking, payment, cancellation, expiry and flight-cancellation logic
│   ├── views.py           # Booking workflow, seat selection, passengers, payment, cancellation
│   ├── admin.py           # View-only reservations and passengers
│   ├── management/commands/expire_reservations.py
│   ├── tests.py           # End-to-end booking, payment, cancellation and expiry tests
│   └── urls.py
├── dashboard/
│   ├── views.py            # Staff-only flight/reservation/user management, financial overview
│   ├── forms.py             # Flight + seat-class inline formset
│   ├── mixins.py            # StaffRequiredMixin
│   ├── tests.py             # Access control, financial totals, flight cancellation
│   └── urls.py
├── core/
│   ├── models.py             # TimeStampedModel abstract base
│   ├── signals.py            # Logs Django admin actions into the app's logging system
│   └── templatetags/badges.py # Custom template tags for status badges
├── templates/                  # RTL HTML templates + shared partials
├── static/css/main.css         # Custom design system
└── airline_system/              # Project settings & root URLs
```

## 🗺️ Roadmap

Core requirements and all optional bonus items (custom managers, email/SMS verification, template optimization, automated tests, Ajax) are complete. What remains is polish and real-world integration:

- [ ] Mobile-responsive pass — hamburger nav for small screens, responsive dashboard tables, touch-friendly seat map
- [ ] Real payment gateway integration for wallet top-ups (e.g., ZarinPal sandbox)
- [ ] Real SMS gateway integration (currently simulated via logging)
- [ ] Redis cache for multi-server deployments (the database cache is enough for a single server)
- [ ] Deployment (Docker + a hosting platform)

## 📄 License

This project was built for educational purposes as part of a university course.
