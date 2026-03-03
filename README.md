# Prenotami Appointment Scheduler

Automated agent that books appointments on [prenotami.esteri.it](https://prenotami.esteri.it).

Appointments are released daily around **6:00 PM EST**. This agent wakes up before the
window, logs in, and polls aggressively — retrying through 503 errors — until a slot
is secured.

---

## Prerequisites

```bash
pip3 install playwright
python3 -m playwright install chromium
```

---

## Setup

### 1. Create your config

```bash
cp config.example.json config.json
```

Edit `config.json`:

```json
{
  "email": "your.email@example.com",
  "password": "your_password",
  "service_id": 1090,
  "appointment_type_text": "",
  "two_captcha_api_key": null,
  "headless": true
}
```

### 2. Find your `service_id`

This is the most important setting.

1. Log in manually to prenotami.esteri.it
2. Click the **Book** tab in the navigation
3. Select your appointment category (e.g. Citizenship, Passport, Visa)
4. Look at the URL — it will be something like:
   ```
   https://prenotami.esteri.it/Services/Booking/1090
   ```
5. Copy that number (`1090`) into `service_id` in config.json

### 3. Handle reCAPTCHA

The login form has a **reCAPTCHA** which is the main obstacle. You have three options:

#### Option A — 2captcha service (recommended for headless/server use)
Sign up at [2captcha.com](https://2captcha.com) (~$3 per 1000 solves).
```json
"two_captcha_api_key": "your_2captcha_api_key_here"
```

#### Option B — Manual solve (no cost, requires you to be present)
```json
"headless": false
```
The browser window will open. When it reaches the login page, solve the CAPTCHA yourself
and press **Enter** in the terminal. The agent takes over from there.

#### Option C — Automatic (may work, may not)
Leave `two_captcha_api_key` as `null` and `headless` as `true`. Playwright with a
human-like browser profile sometimes passes reCAPTCHA v2 automatically. Worth trying
first — if login keeps failing, switch to Option A or B.

---

## Usage

### Run daily scheduler (recommended)
```bash
python3 scheduler.py
```
Sleeps until 10 minutes before 6pm EST, then polls until a slot is booked. Repeats
daily until an appointment is secured.

### Start polling immediately (for testing or if slots are available now)
```bash
python3 scheduler.py --now
# or
python3 agent.py
```

### Run for exactly one release window, then exit
```bash
python3 scheduler.py --once
```

### Use a different config file
```bash
python3 scheduler.py --config /path/to/my_config.json
```

---

## How it works

```
6:00 PM EST         Slots released
5:50 PM EST         Agent wakes up, logs in (with reCAPTCHA)
5:50–6:00 PM        Navigates to Services/Booking/{service_id}
6:00 PM →           Reloads page every 3 seconds
                    On 503: waits 3s and retries (up to 300 times)
                    On session expiry: re-logs in automatically
                    On available slot: clicks it → selects time → confirms
                    On success: saves screenshot, sends webhook notification
```

---

## Config reference

| Key | Required | Description |
|---|---|---|
| `email` | Yes | Your prenotami.esteri.it account email |
| `password` | Yes | Your account password |
| `service_id` | Yes | Numeric ID from the booking URL (e.g. `1090`) |
| `appointment_type_text` | No | Text to match when selecting an appointment sub-type |
| `two_captcha_api_key` | No | API key for 2captcha.com reCAPTCHA solving service |
| `notify_webhook` | No | Slack / Discord webhook URL for success/failure notifications |
| `headless` | No | `true` = no visible browser (default). `false` = shows browser window |
| `max_retries_on_503` | No | Max consecutive 503s before giving up (default: 300 ≈ 15 min) |
| `poll_interval_seconds` | No | Seconds between polls (default: 3) |
| `ramp_up_minutes_before` | No | How many minutes early to wake up (default: 10) |
| `daily_release_hour` | No | Release hour in your timezone (default: 18 = 6pm) |
| `daily_release_minute` | No | Release minute (default: 0) |
| `timezone` | No | Timezone for release time (default: `America/New_York`) |

---

## Output files

| File | Description |
|---|---|
| `scheduler.log` | Full activity log |
| `booking_confirmed.png` | Screenshot saved when appointment is successfully booked |
| `booking_state.png` | Screenshot saved when outcome is unclear (review manually) |
| `login_failed.png` | Screenshot saved on login failure |

---

## Troubleshooting

**Login keeps failing with headless=true**
→ Set `"headless": false` to watch the browser, or add a `two_captcha_api_key`.

**Agent navigates to wrong page after login**
→ Find the exact `service_id` from the URL and set it in config.

**Booking outcome unclear / `booking_state.png` saved**
→ The calendar or confirmation flow may have changed. Check the screenshot to see
   where the agent got stuck and open an issue.

**503 errors persist for more than 15 minutes**
→ Increase `max_retries_on_503`. The site is very overloaded at release time.
