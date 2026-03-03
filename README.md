# Prenotami Appointment Scheduler

Automated agent that books appointments on [prenotami.esteri.it](https://prenotami.esteri.it).

Appointments are released daily around **6:00 PM EST**. This agent wakes up before the
window, logs in, navigates through the full booking flow, and polls aggressively —
retrying through 503 errors — until a slot is secured.

---

## Full booking flow (what the agent does)

```
1.  Login              → /Home  (#login-email, #login-password, reCAPTCHA)
2.  Navigate           → /Services/Booking/{service_id}
3.  Dismiss dialog     → "no appointments available" popup (if shown)
4.  Fill service form  → dropdowns, text fields, file uploads, privacy checkbox
5.  Click "Avanti"     → (#btnAvanti) — lands on calendar
6.  Poll calendar      → every 3s, reload and look for green (available) days
7.  Click green day    → select first available time slot
8.  Submit             → site sends OTP to your email
9.  Enter OTP          → via IMAP auto-retrieval or manual terminal input
10. Confirmed!         → screenshot saved, webhook sent
```

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

### 2. Set your credentials

```json
{
  "email": "your.email@example.com",
  "password": "your_password",
  "service_id": 1319
}
```

### 3. Find your `service_id`

| Service | URL | ID |
|---|---|---|
| Passport | `/Services/Booking/1319` | `1319` |
| Citizenship | `/Services/Booking/2392` | `2392` |
| Other | Log in → Book tab → your service → check URL | varies |

Navigate to your service manually, copy the number at the end of the URL.

### 4. Configure reCAPTCHA (login is gated by Google reCAPTCHA v2)

**Option A — 2captcha (recommended for unattended/headless use)**
```json
"two_captcha_api_key": "your_api_key_here"
```
Sign up at [2captcha.com](https://2captcha.com). Costs ~$3 per 1000 solves.

**Option B — Manual (you solve it once)**
```json
"headless": false
```
Browser window opens at login. Solve the CAPTCHA yourself, press **Enter** in terminal.

**Option C — Automatic (may or may not work)**
Leave both unset. The agent uses a human-like browser profile that sometimes auto-passes
reCAPTCHA v2. Try it first; if login always fails, switch to A or B.

### 5. Fill in service form fields

Before the calendar appears, the site shows a service-specific form. Set the answers
in `form_fields` — these map directly to HTML element IDs on the page:

```json
"form_fields": {
  "ddls_0": "Si",
  "ddls_1": "No",
  "ddls_4": "Celibe/Nubile",
  "DatiAddizionaliPrenotante_2___testo": "0",
  "DatiAddizionaliPrenotante_3___testo": "123 Main St, New York NY"
}
```

**Passport form fields (`service_id: 1319`):**

| Field ID | Description | Example values |
|---|---|---|
| `ddls_0` | Do you have an expired passport? | `"Si"`, `"No"` |
| `ddls_1` | Second passport question | `"Si"`, `"No"` |
| `ddls_4` | Marital status | `"Celibe/Nubile"`, `"Coniugato/a"`, `"Divorziato/a"`, `"Vedovo/a"` |
| `DatiAddizionaliPrenotante_2___testo` | Number of children | `"0"`, `"1"`, `"2"` |
| `DatiAddizionaliPrenotante_3___testo` | Full address | your address |

**Citizenship form (`service_id: 2392`):**
Only requires file uploads — no `form_fields` needed.

**File uploads:**
```json
"file_uploads": {
  "File_0": "/home/user/docs/identity.pdf",
  "File_1": "/home/user/docs/residence.pdf"
}
```

| Field ID | Document |
|---|---|
| `File_0` | Identity document (passport, ID card) |
| `File_1` | Proof of residence |

> **Tip:** Run with `"headless": false` the first time to watch the form being filled
> and confirm the values are correct.

### 6. Configure OTP handling

After selecting a calendar slot, the site emails you a one-time code and shows a popup.

**Option A — IMAP auto-retrieve (unattended)**
```json
"imap_host": "imap.gmail.com",
"imap_user": "your.email@gmail.com",
"imap_password": "your_app_password",
"otp_delay_seconds": 30
```
The agent waits `otp_delay_seconds` for the email to arrive, then fetches it via IMAP.
For Gmail, use an [App Password](https://myaccount.google.com/apppasswords), not your
account password.

**Option B — Manual**
Leave all `imap_*` fields as `null`. The agent will print a prompt to the terminal
and wait for you to type the code. You have ~2 minutes before the session expires.

---

## Usage

### Test your setup (run immediately)
```bash
python3 scheduler.py --now
# or
python3 agent.py
```
Skips the time gate and starts polling right away. Good for verifying config.

### Run the daily scheduler (recommended)
```bash
python3 scheduler.py
```
Sleeps until 10 minutes before 6pm EST, then polls until a slot is booked.
Loops every day until an appointment is secured.

### One window only
```bash
python3 scheduler.py --once
```

---

## Config reference

| Key | Required | Description |
|---|---|---|
| `email` | Yes | Prenotami account email |
| `password` | Yes | Prenotami account password |
| `service_id` | Yes | Numeric service ID from the booking URL |
| `two_captcha_api_key` | No | API key for [2captcha.com](https://2captcha.com) |
| `form_fields` | No | Service form answers keyed by HTML element ID |
| `file_uploads` | No | File upload paths keyed by HTML element ID |
| `imap_host` | No | IMAP server for OTP retrieval (e.g. `imap.gmail.com`) |
| `imap_user` | No | IMAP login email |
| `imap_password` | No | IMAP password or app password |
| `otp_delay_seconds` | No | Seconds to wait before checking IMAP (default: 30) |
| `notify_webhook` | No | Slack/Discord webhook URL |
| `headless` | No | `true` = no browser window (default). `false` = visible window |
| `max_retries_on_503` | No | Max 503 retries before giving up (default: 300 ≈ 15 min) |
| `poll_interval_seconds` | No | Seconds between calendar polls (default: 3) |
| `max_months_to_check` | No | Months of calendar to scan per poll (default: 3) |
| `ramp_up_minutes_before` | No | How early to wake up before release time (default: 10) |
| `daily_release_hour` | No | Release hour in your timezone (default: 18) |
| `daily_release_minute` | No | Release minute (default: 0) |
| `timezone` | No | Timezone (default: `America/New_York`) |

---

## Output files

| File | Description |
|---|---|
| `scheduler.log` | Full activity log |
| `booking_confirmed.png` | Screenshot on successful booking |
| `booking_state.png` | Screenshot when outcome is unclear |
| `login_failed.png` | Screenshot on login failure |
| `form_state.png` | Screenshot if service form couldn't be submitted |
| `otp_state.png` | Screenshot if OTP step failed |

---

## Troubleshooting

**Login fails (headless=true)** → Add `two_captcha_api_key` or set `"headless": false`.

**Form fields wrong** → Run with `"headless": false`, watch the form fill, adjust values.

**OTP expires before entry** → Use IMAP auto-retrieve, or increase `otp_delay_seconds`.

**503 for 15+ minutes** → Increase `max_retries_on_503`. The site is extremely overloaded at release time.

**Booking outcome unclear** → Check `booking_state.png` to see where the flow stopped.
