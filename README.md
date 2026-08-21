# ✈️ Airline Ticket Booking System

A full-featured flight ticket booking platform built with **Django** and **PostgreSQL**, featuring atomic seat reservation, wallet-based payments, and a custom right-to-left (RTL) interface designed for Persian-speaking users.

This project was built as a university web programming course project, with an emphasis on production-grade practices: normalized data modeling, race-condition-safe booking logic, and a clean, maintainable Django architecture.

---

## ✨ Key Features

- **Flight search & browsing** — search flights by origin, destination, and date, with paginated results
- **Multi-class seating** — Economy / Business / First class per flight, each with its own price and capacity
- **Atomic seat reservation** — uses database-level `F()` expressions to prevent overbooking when multiple users book simultaneously
- **Wallet-based payments** — users deposit and withdraw from an in-app wallet; all balance changes are atomic and race-condition-safe
- **Booking cancellation with penalty** — cancellation refunds are automatically calculated based on each flight's cancellation penalty percentage
- **Custom user model** — extends Django's `AbstractUser` with phone/email verification flags and wallet balance
- **Custom RTL UI** — hand-designed Persian interface (no framework templates) with a boarding-pass-inspired visual identity
- **Admin panel** — full CRUD over all models, with inline seat-class management for flights

## 🛠️ Tech Stack

| Layer | Technology |
|---|---|
| Backend | Django (Class-Based Views) |
| Database | PostgreSQL |
| Frontend | Django Templates, vanilla CSS (RTL, custom design system) |
| Fonts | Vazirmatn (UI text), JetBrains Mono (flight/booking codes) |
| Config | `python-decouple` for environment-based settings |

## 🏗️ Architecture

The project is split into four Django apps, each with a single responsibility:

```
airline_system/
├── core/        # Shared abstract base model (TimeStampedModel)
├── accounts/    # Custom user model, authentication, wallet logic
├── flights/     # Airports, airlines, routes, flights, seat classes
└── tickets/     # Reservations and passengers, booking workflow
```

### Data model highlights

- `Route` enforces uniqueness and prevents self-referencing routes (origin ≠ destination) at the database level via `CheckConstraint`.
- `SeatClass.reserve_seats()` / `release_seats()` use `F()` expressions with conditional `UPDATE` queries — no read-then-write race condition, even under concurrent requests.
- `Reservation.total_paid_price` is stored as a price **snapshot** at booking time, so historical bookings remain accurate even if flight prices change later.
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

Create a `.env` file in the project root:

```env
DB_NAME=airline_db
DB_USER=airline_user
DB_PASSWORD=your_password
DB_HOST=localhost
DB_PORT=5432
SECRET_KEY=your-django-secret-key
DEBUG=True
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

Visit `http://127.0.0.1:8000/` for the app, and `http://127.0.0.1:8000/admin/` for the admin panel.

## 📁 Project Structure

```
FinalProject/
├── accounts/
│   ├── models.py       # CustomUser with wallet & verification fields
│   ├── forms.py         # Registration & login forms
│   ├── views.py         # Auth flow (register, login, logout, profile)
│   └── urls.py
├── flights/
│   ├── models.py         # Airport, Airline, Route, Flight, SeatClass
│   ├── forms.py           # Flight search form
│   ├── views.py           # Flight listing & detail views
│   └── urls.py
├── tickets/
│   ├── models.py          # Reservation, Passenger
│   ├── forms.py            # Booking & passenger forms
│   ├── views.py            # Booking workflow, cancellation
│   └── urls.py
├── core/
│   └── models.py            # TimeStampedModel abstract base
├── templates/                 # RTL HTML templates
├── static/css/main.css        # Custom design system
└── airline_system/            # Project settings & root URLs
```

## 🗺️ Roadmap

- [ ] Email/SMS verification (currently modeled but not yet wired to a provider)
- [ ] Payment gateway integration for wallet top-ups
- [ ] Automated test suite (unit tests for booking concurrency)
- [ ] Deployment (Docker + Railway/Render)

## 📄 License

This project was built for educational purposes as part of a university course.
