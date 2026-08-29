# ✈️ Airline Ticket Booking System

A full-featured flight ticket booking platform built with **Django** and **PostgreSQL**, featuring atomic seat reservation, wallet-based payments, a staff management dashboard, and a custom right-to-left (RTL) interface designed for Persian-speaking users.

This project was built as a university web programming course project, with an emphasis on production-grade practices: normalized data modeling, race-condition-safe booking logic, structured logging, and a clean, maintainable Django architecture.

---

## ✨ Key Features

### Booking & Payments
- **Flight search & browsing** — search by origin, destination, and date, with paginated results
- **Multi-class seating** — Economy / Business / First class per flight, each with its own price multiplier and capacity
- **Specific seat selection** — users pick exact seats from a visual seat map; group bookings are validated to ensure seats are adjacent in the same row
- **Atomic seat reservation** — uses `select_for_update()` and database-level `F()` expressions to prevent overbooking or double-booking under concurrent requests
- **Wallet-based payments** — users deposit and withdraw from an in-app wallet (simulated top-up, no real payment gateway); all balance changes are atomic
- **Booking cancellation with penalty** — refunds are automatically calculated based on each flight's cancellation penalty percentage, with a confirmation modal before cancelling

### Accounts & Security
- **Custom user model** — extends Django's `AbstractUser` with a wallet balance and phone/email verification flags
- **Email verification** — a one-time link (24h expiry) sent via a configurable email backend (console backend by default; switches to real SMTP via `.env` for a live demo)
- **Simulated phone (SMS) verification** — a 6-digit one-time code flow; since no real SMS gateway is connected, the code is logged instead of texted
- **Structured logging** — rotating file handlers (`general.log`, `errors.log`, `security.log`) with a dedicated logger per app, covering auth events, wallet transactions, booking lifecycle, and staff actions. Django admin actions (add/change/delete) are also captured via a signal on `LogEntry`.

### Staff Dashboard
- A separate, staff-only management area (distinct from the Django admin) for:
  - Creating and editing flights with inline seat-class management
  - Bulk seat generation with continuous row numbering across classes (economy → business → first)
  - Viewing all reservations and users, with drill-down into per-flight booking history
  - A financial overview: gross revenue, refunded amounts, and net revenue, both system-wide and per flight
- **Automatic flight status transitions** — flights move `Scheduled → Active` one hour before departure, and `→ Completed` once arrival time has passed, via a lightweight cache-throttled middleware (no external cron/Celery required). Manually cancelled flights are never touched by this logic.

### Engineering Practices
- **Custom model managers/querysets** (`Flight.objects.upcoming()`, `Reservation.objects.active()`, etc.) centralize query logic instead of repeating filters across views
- **DRY templates** — shared partials (`pagination.html`) and custom template tags (`{% flight_status_badge %}`, `{% reservation_status_badge %}`) eliminate duplicated markup across pages
- **Custom RTL UI** — hand-built Persian interface (no frontend framework) with a boarding-pass-inspired visual identity

## 🛠️ Tech Stack

| Layer | Technology |
|---|---|
| Backend | Django (Class-Based Views) |
| Database | PostgreSQL |
| Frontend | Django Templates, vanilla CSS (RTL, custom design system) |
| Fonts | Vazirmatn (UI text), JetBrains Mono (flight/booking codes) |
| Config | Environment variables via `.env` (`python-decouple`) |

## 🏗️ Architecture

The project is split into five Django apps, each with a single responsibility:

```
airline_system/
├── core/        # Shared abstract base model, admin-action logging signal, custom template tags
├── accounts/    # Custom user model, authentication, wallet logic, email/phone verification
├── flights/     # Airports, airlines, routes, flights, seat classes, individual seats
├── tickets/     # Reservations, passengers, seat-selection booking workflow
└── dashboard/   # Staff-only management area (no models of its own — composes the apps above)
```

### Data model highlights

- `Route` enforces uniqueness and prevents self-referencing routes (origin ≠ destination) at the database level via `CheckConstraint`.
- `Seat` models individual, selectable seats per class; booking locks specific seats with `select_for_update()` rather than just decrementing a counter.
- `Reservation.total_paid_price` is stored as a price **snapshot** at booking time, so historical bookings remain accurate even if flight prices change later.
- `Reservation.seat_class` uses `on_delete=PROTECT` to preserve financial history — a seat class with any booking history (even cancelled) can't be casually deleted; a dedicated admin action allows an explicit, audited force-delete when there's no active reservation.
- `CustomUser.deposit()` / `withdraw()` follow the same atomic-update pattern as seat reservation, preventing negative balances under concurrent transactions.

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

### Database setup

```sql
CREATE USER airline_user WITH PASSWORD 'your_password';
CREATE DATABASE airline_db OWNER airline_user;
```

### Run migrations & start the server

```bash
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Visit `http://127.0.0.1:8000/` for the app, `http://127.0.0.1:8000/admin/` for the Django admin, and `http://127.0.0.1:8000/dashboard/` for the staff dashboard (requires a staff user).

### Generating seat maps for test flights

After creating a flight with seat classes (via the dashboard or Django admin), run the **"ساخت خودکار صندلی‌ها"** action (available in both places) to auto-generate individual seats with continuous row numbering.

### Useful management commands

```bash
python manage.py sync_flight_statuses          # manually trigger the scheduled/active/completed transition
python manage.py recalculate_seat_availability # reconcile seat counts if data ever drifts out of sync
```

## 📁 Project Structure

```
FinalProject/
├── accounts/
│   ├── models.py      # CustomUser, EmailVerificationToken, PhoneVerificationCode
│   ├── forms.py        # Registration, login, deposit, phone verification forms
│   ├── views.py        # Auth flow, wallet deposit, email/phone verification
│   └── urls.py
├── flights/
│   ├── models.py        # Airport, Airline, Route, Flight, SeatClass, Seat
│   ├── forms.py          # Flight search form
│   ├── views.py          # Flight listing & detail views
│   ├── services.py       # Seat generation, flight status sync
│   ├── middleware.py     # Automatic flight status transitions
│   └── urls.py
├── tickets/
│   ├── models.py         # Reservation, Passenger, ReservationSeat
│   ├── forms.py           # Booking & passenger forms
│   ├── views.py           # Booking workflow, seat selection, cancellation
│   └── urls.py
├── dashboard/
│   ├── views.py            # Staff-only flight/reservation/user management, financial overview
│   ├── forms.py             # Flight + seat-class inline formset
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

Core requirements are complete. A few optional enhancements remain:

- [ ] Ajax-based interactions (e.g., seat map or search without a full page reload)
- [ ] Automated test suite (unit tests for booking concurrency and wallet logic)
- [ ] Real payment gateway integration for wallet top-ups
- [ ] Real SMS gateway integration (currently simulated via logging)
- [ ] Deployment (Docker + Railway/Render)

## 📄 License

This project was built for educational purposes as part of a university course.
