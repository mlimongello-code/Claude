# Prenotami Appointment Scheduler

Automated agent that books appointments on [prenotami.esteri.it](https://prenotami.esteri.it).

Appointments are released daily at **6:00 PM EST**. This agent wakes up 10 minutes early,
logs in, and polls aggressively — retrying transparently through 503 errors — until a slot
is secured.

---

## Setup

### 1. Install dependencies

```bash
pip3 install playwright
playwright install chromium
```

### 2. Configure credentials

```bash
cp config.example.json config.json
```

Edit `config.json`:

```json
{
  "email": "your.email@example.com",
  "password": "your_password_here",
  "service_id": null,
  "notify_webhook": null,
  "headless": true
}
```

**Key fields:**

| Field | Description |
|---|---|
| `email` | Your Prenotami account email |
| `password` | Your Prenotami account password |
| `service_id` | (Optional) Numeric ID from the booking URL for your specific service |
| `notify_webhook` | (Optional) Slack/Discord webhook URL for notifications |
| `max_retries_on_503` | Max 503 retries before giving up (default: 300) |
| `poll_interval_seconds` | Seconds between each poll (default: 3) |
| `ramp_up_minutes_before` | How many minutes before 6pm to start (default: 10) |
| `headless` | Run browser in headless mode (default: true) |

### 3. Find your `service_id` (Recommended)

1. Log in manually to prenotami.esteri.it
2. Navigate to the service you want (e.g., citizenship, passport)
3. Look at the URL — it will contain a number like `/Services/Booking/1234`
4. Set `"service_id": 1234` in your config

Without `service_id`, the agent lands on the general services list page.

---

## Usage

### Wait for 6pm release window (recommended)

```bash
python3 scheduler.py
```

The scheduler wakes up 10 minutes before 6pm EST, logs in, and polls until a slot is booked.
Runs every day until an appointment is secured.

### Run immediately (skip time gate)

```bash
python3 scheduler.py --now
# or
python3 agent.py
```

Useful for testing your configuration or if you know slots are currently available.

### Run for exactly one release window

```bash
python3 scheduler.py --once
```

---

## How it works

1. **Wakes up** 10 minutes before 6pm EST
2. **Logs in** to your Prenotami account
3. **Navigates** to the booking page for your service
4. **Polls** every 3 seconds for available calendar slots
5. On **503 errors**: waits and retries (up to 300 times)
6. On **session expiry**: automatically re-logs in
7. When a slot appears: **clicks it**, selects the first time, and confirms
8. **Saves a screenshot** (`booking_confirmed.png`) as proof
9. **Sends a notification** via webhook (if configured)
10. **Loops daily** until an appointment is secured

---

## Output files

| File | Description |
|---|---|
| `scheduler.log` | Full log of all activity |
| `booking_confirmed.png` | Screenshot when appointment is booked |
| `booking_state.png` | Screenshot saved when outcome is unclear |

---

## Tips

- Run this on a server or always-on machine (not your laptop) so it doesn't miss the window
- Set `headless: false` while testing so you can watch the browser
- Use `--now` first to verify your credentials and service navigation work correctly
- The Slack/Discord webhook lets you get notified immediately when the appointment is booked

---

## Troubleshooting

**Login keeps failing** — Double-check your email/password in `config.json`. Run with `"headless": false` to see what's happening.

**Lands on wrong page** — Find your `service_id` from the URL and add it to config.

**Keeps getting 503** — This is normal. The agent handles it automatically. Increase `max_retries_on_503` if needed.

**Booking state unclear** — Check `booking_state.png` to see where the agent got stuck.
