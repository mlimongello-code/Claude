"""
Prenotami Appointment Scheduler Agent
======================================
Automates appointment booking on https://prenotami.esteri.it

The site releases a limited number of slots daily around 6pm EST.
This agent:
  1. Logs in (with reCAPTCHA handling)
  2. Navigates to the specified service booking page
  3. Polls aggressively for available calendar slots
  4. Books the first available slot
  5. Retries transparently on 503 / server overload errors

reCAPTCHA handling (in priority order):
  A. 2captcha service  — set "two_captcha_api_key" in config.json
  B. Automatic bypass   — realistic browser profile sometimes passes v2 automatically
  C. Manual            — set "headless": false, solve it yourself in the browser window
"""

import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, Page, BrowserContext, TimeoutError as PlaywrightTimeout

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scheduler.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("prenotami")

BASE_URL      = "https://prenotami.esteri.it"
LOGIN_URL     = f"{BASE_URL}/Home"
SERVICES_URL  = f"{BASE_URL}/Services"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path: str = "config.json") -> dict:
    cfg_path = Path(path)
    if not cfg_path.exists():
        log.error("config.json not found. Copy config.example.json → config.json and fill in your details.")
        sys.exit(1)

    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)

    for key in ("email", "password", "service_id"):
        if not cfg.get(key):
            log.error("Missing required config key: '%s'", key)
            sys.exit(1)

    cfg.setdefault("two_captcha_api_key", None)
    cfg.setdefault("max_retries_on_503", 300)
    cfg.setdefault("poll_interval_seconds", 3)
    cfg.setdefault("ramp_up_minutes_before", 10)
    cfg.setdefault("timezone", "America/New_York")
    cfg.setdefault("daily_release_hour", 18)
    cfg.setdefault("daily_release_minute", 0)
    cfg.setdefault("headless", True)
    cfg.setdefault("notify_webhook", None)
    return cfg


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

def notify(cfg: dict, subject: str, body: str) -> None:
    webhook = cfg.get("notify_webhook")
    if not webhook:
        return
    try:
        payload = json.dumps({"text": f"*{subject}*\n{body}"}).encode()
        req = urllib.request.Request(
            webhook, data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        log.info("Webhook notification sent.")
    except Exception as exc:
        log.warning("Webhook failed: %s", exc)


# ---------------------------------------------------------------------------
# Browser setup
# ---------------------------------------------------------------------------

def launch_browser(playwright, cfg: dict):
    """Launch Chromium with human-like fingerprinting to help pass reCAPTCHA."""
    browser = playwright.chromium.launch(
        headless=cfg["headless"],
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--window-size=1366,768",
        ],
    )
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1366, "height": 768},
        locale="it-IT",
        timezone_id="America/New_York",
        java_script_enabled=True,
    )
    # Remove navigator.webdriver fingerprint
    context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
        Object.defineProperty(navigator, 'languages', { get: () => ['it-IT','it','en-US','en'] });
        window.chrome = { runtime: {} };
    """)
    return browser, context


# ---------------------------------------------------------------------------
# reCAPTCHA helpers
# ---------------------------------------------------------------------------

def _get_recaptcha_sitekey(page: Page) -> Optional[str]:
    """Extract the reCAPTCHA sitekey from the page."""
    try:
        sitekey = page.get_attribute("[data-sitekey]", "data-sitekey", timeout=3000)
        return sitekey
    except Exception:
        pass
    try:
        sitekey = page.evaluate(
            "() => { const el = document.querySelector('.g-recaptcha'); "
            "return el ? el.dataset.sitekey : null; }"
        )
        return sitekey
    except Exception:
        return None


def solve_recaptcha_2captcha(page: Page, api_key: str) -> bool:
    """
    Use 2captcha.com to solve the reCAPTCHA v2.
    Returns True if solved and injected successfully.
    API key: https://2captcha.com (a few cents per solve)
    """
    sitekey = _get_recaptcha_sitekey(page)
    if not sitekey:
        log.warning("Could not find reCAPTCHA sitekey on page.")
        return False

    log.info("Sending reCAPTCHA to 2captcha (sitekey: %s)…", sitekey)
    page_url = page.url

    # Submit task
    try:
        submit_url = (
            "http://2captcha.com/in.php?"
            + urllib.parse.urlencode({
                "key": api_key,
                "method": "userrecaptcha",
                "googlekey": sitekey,
                "pageurl": page_url,
                "json": 1,
            })
        )
        with urllib.request.urlopen(submit_url, timeout=30) as r:
            result = json.loads(r.read())
        if result.get("status") != 1:
            log.warning("2captcha submit failed: %s", result)
            return False
        task_id = result["request"]
        log.info("2captcha task submitted, id=%s. Waiting for solve…", task_id)
    except Exception as exc:
        log.warning("2captcha submit error: %s", exc)
        return False

    # Poll for result (up to 120 seconds)
    for attempt in range(24):
        time.sleep(5)
        try:
            poll_url = (
                "http://2captcha.com/res.php?"
                + urllib.parse.urlencode({
                    "key": api_key,
                    "action": "get",
                    "id": task_id,
                    "json": 1,
                })
            )
            with urllib.request.urlopen(poll_url, timeout=15) as r:
                result = json.loads(r.read())
            if result.get("status") == 1:
                token = result["request"]
                log.info("2captcha solved! Injecting token…")
                break
            if result.get("request") == "ERROR_CAPTCHA_UNSOLVABLE":
                log.warning("2captcha: CAPTCHA unsolvable.")
                return False
        except Exception as exc:
            log.warning("2captcha poll error: %s", exc)
    else:
        log.warning("2captcha timed out waiting for solution.")
        return False

    # Inject the token into the page
    try:
        page.evaluate(
            f"""() => {{
                document.getElementById('g-recaptcha-response').innerHTML = '{token}';
                if (typeof ___grecaptcha_cfg !== 'undefined') {{
                    Object.entries(___grecaptcha_cfg.clients).forEach(([k, v]) => {{
                        if (v && v.S && typeof v.S.callback === 'function') {{
                            v.S.callback('{token}');
                        }}
                    }});
                }}
            }}"""
        )
        log.info("reCAPTCHA token injected.")
        return True
    except Exception as exc:
        log.warning("Token injection error: %s", exc)
        return False


def handle_recaptcha(page: Page, cfg: dict) -> None:
    """
    Attempt to handle reCAPTCHA via 2captcha if key is configured.
    If not configured, log a warning and rely on automatic pass or manual solve.
    """
    api_key = cfg.get("two_captcha_api_key")
    if api_key:
        solved = solve_recaptcha_2captcha(page, api_key)
        if not solved:
            log.warning("2captcha solve failed — attempting submit anyway.")
    else:
        if cfg["headless"]:
            log.warning(
                "No 2captcha key configured and headless=true. "
                "Login may fail due to reCAPTCHA. "
                "Consider setting two_captcha_api_key or headless=false."
            )
        else:
            log.info("headless=false: please solve the reCAPTCHA manually in the browser window, then press Enter here.")
            input("Press Enter after solving the reCAPTCHA in the browser…")


# ---------------------------------------------------------------------------
# Page helpers
# ---------------------------------------------------------------------------

def is_503(page: Page) -> bool:
    try:
        content = page.content().lower()
        title   = page.title().lower()
        url     = page.url.lower()
        for indicator in ["503", "service unavailable", "too many requests",
                          "429", "temporarily unavailable", "server error"]:
            if indicator in content or indicator in title or indicator in url:
                return True
    except Exception:
        pass
    return False


def wait_for_page(page: Page, timeout: int = 20_000) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except PlaywrightTimeout:
        pass


def is_logged_in(page: Page) -> bool:
    """Check if we still have an authenticated session."""
    url = page.url.lower()
    # Redirected to login page = not logged in
    if "login" in url or (url.endswith("/home") and "services" not in url):
        content = page.content().lower()
        if "login-email" in content or "login-password" in content:
            return False
    return True


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def login(page: Page, cfg: dict) -> bool:
    """
    Navigate to login page and authenticate.
    Handles reCAPTCHA via 2captcha or manual solve.
    Returns True on success.
    """
    log.info("Loading login page…")
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        wait_for_page(page)
    except Exception as exc:
        log.warning("Failed to load login page: %s", exc)
        return False

    if is_503(page):
        log.warning("503 on login page.")
        return False

    # Accept cookie consent if shown
    try:
        cookie_btn = page.locator("#cookieAccept, button:has-text('Accetto'), button:has-text('Accept All')")
        if cookie_btn.count() > 0:
            cookie_btn.first.click()
            time.sleep(0.5)
    except Exception:
        pass

    # Wait for email field
    try:
        page.wait_for_selector("#login-email", timeout=15_000)
    except PlaywrightTimeout:
        log.warning("Login form not found (login-email missing). Page: %s", page.url)
        return False

    log.info("Filling credentials…")
    page.fill("#login-email", cfg["email"])
    time.sleep(0.3)
    page.fill("#login-password", cfg["password"])
    time.sleep(0.5)

    # Handle reCAPTCHA before submitting
    handle_recaptcha(page, cfg)

    # Click the login button (has class 'g-recaptcha' because it triggers captcha callback)
    log.info("Submitting login form…")
    try:
        submit = page.locator("button.g-recaptcha, button[type='submit'], input[type='submit']")
        submit.first.click()
        wait_for_page(page)
    except Exception as exc:
        log.warning("Submit click error: %s", exc)
        return False

    # Verify login success
    url     = page.url.lower()
    content = page.content().lower()

    failure_signals = ["login-email", "login-password", "credenziali errate",
                       "invalid credentials", "errore di autenticazione"]
    if any(s in content for s in failure_signals):
        log.error("Login failed — wrong credentials or reCAPTCHA blocked.")
        page.screenshot(path="login_failed.png")
        return False

    log.info("Login successful. URL: %s", page.url)
    return True


# ---------------------------------------------------------------------------
# Navigate to booking page
# ---------------------------------------------------------------------------

def go_to_booking_page(page: Page, cfg: dict) -> bool:
    """
    Navigate directly to the service booking page:
    https://prenotami.esteri.it/Services/Booking/{service_id}
    """
    service_id = cfg["service_id"]
    url = f"{BASE_URL}/Services/Booking/{service_id}"
    log.info("Navigating to booking page: %s", url)

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        wait_for_page(page)
    except Exception as exc:
        log.warning("Navigation error: %s", exc)
        return False

    if is_503(page):
        log.warning("503 on booking page.")
        return False

    # If we got redirected to login, session expired
    if not is_logged_in(page):
        log.warning("Redirected to login — session expired.")
        return False

    return True


# ---------------------------------------------------------------------------
# Appointment type selection
# ---------------------------------------------------------------------------

def select_appointment_type(page: Page, cfg: dict) -> bool:
    """
    If the booking page shows a list of appointment types/services, select the
    correct one. If appointment_type_text is configured, match by text;
    otherwise pick the first option.
    """
    apt_text = cfg.get("appointment_type_text", "").strip().lower()

    # Check if there's a selection step (list of services/types)
    # Common patterns: <a> cards, <button>, <li> items, <select> dropdown
    type_links = page.locator(
        "a.service-type, a.appointment-type, "
        ".service-list a, .list-group-item, "
        "table.services td a, .table-services a"
    )

    if type_links.count() == 0:
        # Maybe it's a direct booking form — no selection needed
        log.info("No appointment type selection found — proceeding directly.")
        return True

    log.info("Found %d appointment type option(s).", type_links.count())

    if apt_text:
        for i in range(type_links.count()):
            link = type_links.nth(i)
            if apt_text in link.inner_text().lower():
                log.info("Selecting appointment type: %s", link.inner_text().strip())
                link.click()
                wait_for_page(page)
                return True
        log.warning("Could not find appointment type matching '%s'. Selecting first.", apt_text)

    type_links.first.click()
    wait_for_page(page)
    return True


# ---------------------------------------------------------------------------
# Calendar — detect and book available slot
# ---------------------------------------------------------------------------

def find_and_book_slot(page: Page) -> bool:
    """
    Look for available (green) calendar slots and book the first one.
    Prenotami uses a Bootstrap datepicker-style calendar where:
      - Available days: <td class="day"> without 'disabled' or 'old'
      - Green highlight or custom class signals availability
    Returns True if an appointment was booked.
    """
    # Check for no-availability messages first
    no_avail = [
        "non ci sono appuntamenti disponibili",
        "no appointments available",
        "nessuna disponibilità",
        "fully booked",
    ]
    content_lower = page.content().lower()
    if any(p in content_lower for p in no_avail):
        log.info("No appointments available (explicit message).")
        return False

    # Available calendar cells — green/active days
    # The site uses Bootstrap datepicker; available = .day without .disabled/.old/.new
    # Some versions use a custom 'active' or colored class for open slots
    available = page.locator(
        # Bootstrap datepicker available days
        "td.day:not(.disabled):not(.old):not(.new):not(.off), "
        # FullCalendar / custom calendar available event
        "td.open-day, td.available, .fc-event.available, "
        # Colored table cells (green background inline or class)
        "td[style*='green'], td.green, td.slot-available, "
        # Any day cell with a booking link inside
        "td.day > a"
    )

    count = available.count()
    if count == 0:
        log.info("No available slot cells found in calendar.")
        return False

    log.info("Found %d available slot(s). Clicking the first…", count)
    available.first.click()
    wait_for_page(page)

    # After clicking a day, a time-slot list may appear
    time_slots = page.locator(
        "input[type='radio'], "           # radio buttons for times
        "button.orario, .orario, "        # Italian "time" class
        "button.time-slot, a.time-slot, " # generic time slot
        "li.slot, td.slot-time"
    )
    if time_slots.count() > 0:
        log.info("Selecting first time slot…")
        time_slots.first.click()
        wait_for_page(page)

    # Fill optional notes
    notes_field = page.locator("#Notes, textarea[name='Notes'], textarea[name='note']")
    if notes_field.count() > 0:
        notes_field.first.fill("Automated booking")

    # Confirm the booking
    confirm = page.locator(
        "button:has-text('Prenota'), "
        "button:has-text('Conferma'), "
        "button:has-text('Confirm'), "
        "button:has-text('Book'), "
        "input[type='submit']"
    )
    if confirm.count() == 0:
        log.warning("No confirmation button found — saving screenshot.")
        page.screenshot(path="booking_state.png")
        return False

    log.info("Clicking confirmation button…")
    confirm.first.click()
    wait_for_page(page)

    # Check success
    final = page.content().lower()
    success = [
        "appuntamento confermato", "booking confirmed",
        "prenotazione effettuata", "successfully booked",
        "conferma", "ricevuta", "receipt",
    ]
    if any(p in final for p in success):
        log.info("APPOINTMENT BOOKED SUCCESSFULLY!")
        page.screenshot(path="booking_confirmed.png")
        return True

    if is_503(page):
        log.warning("503 after booking attempt.")
        return False

    log.warning("Booking outcome unclear — saving screenshot.")
    page.screenshot(path="booking_state.png")
    return False


# ---------------------------------------------------------------------------
# Core polling loop
# ---------------------------------------------------------------------------

def poll_until_booked(cfg: dict) -> bool:
    """
    Main loop: login → navigate → poll calendar → book.
    Handles 503s and session expiry automatically.
    Returns True if an appointment was secured.
    """
    max_503_retries  = cfg["max_retries_on_503"]
    poll_interval    = cfg["poll_interval_seconds"]
    consecutive_503s = 0
    attempt          = 0

    with sync_playwright() as pw:
        browser, context = launch_browser(pw, cfg)
        page = context.new_page()

        try:
            # Initial login
            for attempt_login in range(1, 6):
                if login(page, cfg):
                    break
                log.warning("Login attempt %d failed. Retrying in 10s…", attempt_login)
                time.sleep(10)
            else:
                log.error("Could not log in after 5 attempts. Aborting.")
                return False

            # Navigate to the booking page
            if not go_to_booking_page(page, cfg):
                log.warning("Failed to reach booking page after login.")

            # Select appointment type if needed
            select_appointment_type(page, cfg)

            # Polling loop
            while consecutive_503s < max_503_retries:
                attempt += 1
                log.info("Poll #%d (503 streak: %d)…", attempt, consecutive_503s)

                # Reload booking page to get fresh calendar
                try:
                    page.reload(wait_until="domcontentloaded", timeout=20_000)
                    wait_for_page(page, timeout=10_000)
                except Exception as exc:
                    log.warning("Reload error: %s", exc)

                # Handle 503
                if is_503(page):
                    consecutive_503s += 1
                    log.warning("503 (#%d). Waiting %ds…", consecutive_503s, poll_interval)
                    time.sleep(poll_interval)
                    continue

                consecutive_503s = 0  # Reset on a clean page load

                # Re-login if session expired
                if not is_logged_in(page):
                    log.info("Session expired — re-logging in…")
                    for _ in range(3):
                        if login(page, cfg):
                            go_to_booking_page(page, cfg)
                            select_appointment_type(page, cfg)
                            break
                        time.sleep(5)
                    else:
                        log.warning("Could not re-establish session.")
                        consecutive_503s += 1
                    continue

                # Try to book
                if find_and_book_slot(page):
                    return True

                log.info("No slot yet. Waiting %ds…", poll_interval)
                time.sleep(poll_interval)

            log.error("Exceeded max 503 retries (%d). Stopping.", max_503_retries)
            return False

        finally:
            try:
                context.close()
                browser.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Entry point (run immediately, no time gate)
# ---------------------------------------------------------------------------

def run_now(cfg: dict) -> None:
    log.info("Starting appointment polling immediately.")
    booked = poll_until_booked(cfg)
    if booked:
        notify(cfg, "Appointment Booked!", "Your prenotami.esteri.it appointment has been confirmed!")
    else:
        notify(cfg, "No Appointment Secured", "The scheduler finished without booking an appointment.")


if __name__ == "__main__":
    cfg = load_config()
    run_now(cfg)
