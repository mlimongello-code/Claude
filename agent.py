"""
Prenotami Appointment Scheduler Agent
======================================
Automates appointment booking on https://prenotami.esteri.it

Appointments are released daily at 6pm EST. This agent:
  1. Wakes up before the release window
  2. Logs in and polls for available slots aggressively
  3. Books the first available appointment
  4. Retries transparently on 503 / overload errors
"""

import json
import logging
import os
import sys
import time
import datetime
import zoneinfo
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, Page, Browser, TimeoutError as PlaywrightTimeout

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

BASE_URL = "https://prenotami.esteri.it"
LOGIN_URL = f"{BASE_URL}/Home"
BOOKING_URL = f"{BASE_URL}/Services"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path: str = "config.json") -> dict:
    cfg_path = Path(path)
    if not cfg_path.exists():
        example = Path("config.example.json")
        if example.exists():
            log.error(
                "config.json not found. Copy config.example.json to config.json "
                "and fill in your credentials."
            )
        else:
            log.error("config.json not found.")
        sys.exit(1)

    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)

    required = ["email", "password"]
    for key in required:
        if not cfg.get(key):
            log.error("Missing required config key: %s", key)
            sys.exit(1)

    # Apply defaults
    cfg.setdefault("max_retries_on_503", 300)
    cfg.setdefault("poll_interval_seconds", 3)
    cfg.setdefault("ramp_up_minutes_before", 10)
    cfg.setdefault("timezone", "America/New_York")
    cfg.setdefault("daily_release_hour", 18)
    cfg.setdefault("daily_release_minute", 0)
    cfg.setdefault("headless", True)
    return cfg


# ---------------------------------------------------------------------------
# Notification helpers
# ---------------------------------------------------------------------------

def notify(cfg: dict, subject: str, body: str) -> None:
    """Send notification via webhook if configured."""
    webhook = cfg.get("notify_webhook")
    if webhook:
        try:
            import urllib.request, urllib.parse
            payload = json.dumps({"text": f"*{subject}*\n{body}"}).encode()
            req = urllib.request.Request(
                webhook,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
            log.info("Webhook notification sent.")
        except Exception as exc:
            log.warning("Failed to send webhook notification: %s", exc)


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

def launch_browser(playwright, cfg: dict):
    browser = playwright.chromium.launch(
        headless=cfg["headless"],
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    )
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        locale="it-IT",
        timezone_id="America/New_York",
    )
    # Hide webdriver flag
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return browser, context


def is_503(page: Page) -> bool:
    """Detect 503 / overload pages."""
    try:
        content = page.content().lower()
        title = page.title().lower()
        url = page.url.lower()
        indicators = [
            "503",
            "service unavailable",
            "server error",
            "too many requests",
            "429",
            "overloaded",
            "temporarily unavailable",
        ]
        return any(ind in content or ind in title or ind in url for ind in indicators)
    except Exception:
        return False


def wait_for_stable_page(page: Page, timeout: int = 30_000) -> None:
    """Wait until the page network is idle."""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except PlaywrightTimeout:
        pass  # Proceed anyway; some pages never fully idle


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def login(page: Page, cfg: dict) -> bool:
    """
    Log in to Prenotami. Returns True on success, False otherwise.
    """
    log.info("Navigating to login page…")
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        wait_for_stable_page(page)
    except Exception as exc:
        log.warning("Navigation error on login page: %s", exc)
        return False

    if is_503(page):
        log.warning("503 on login page.")
        return False

    try:
        # Accept cookie banner if present
        cookie_btn = page.locator("button:has-text('Accetto'), button:has-text('Accept'), #cookieAccept")
        if cookie_btn.count() > 0:
            cookie_btn.first.click()
            time.sleep(0.5)

        # Fill credentials
        email_field = page.locator("input[type='email'], input[name='Email'], #Email")
        password_field = page.locator("input[type='password'], input[name='Password'], #Password")

        email_field.first.fill(cfg["email"])
        password_field.first.fill(cfg["password"])

        # Submit
        submit = page.locator("button[type='submit'], input[type='submit']")
        submit.first.click()
        wait_for_stable_page(page)

        # Check for failed login indicators
        current = page.url.lower()
        content = page.content().lower()
        failure_signals = ["invalid", "incorrect", "errore", "error", "wrong"]
        if any(s in content for s in failure_signals) and "home" not in current and "service" not in current:
            log.error("Login failed — check your credentials.")
            return False

        log.info("Login successful. Current URL: %s", page.url)
        return True

    except Exception as exc:
        log.warning("Exception during login: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Navigate to booking service
# ---------------------------------------------------------------------------

def navigate_to_service(page: Page, cfg: dict) -> bool:
    """
    Navigate to the booking/services page after login.
    If service_id is set in config, navigate directly to that service.
    """
    service_id = cfg.get("service_id")
    if service_id:
        target = f"{BASE_URL}/Services/Booking/{service_id}"
    else:
        target = BOOKING_URL

    log.info("Navigating to services: %s", target)
    try:
        page.goto(target, wait_until="domcontentloaded", timeout=30_000)
        wait_for_stable_page(page)
    except Exception as exc:
        log.warning("Navigation error to services: %s", exc)
        return False

    if is_503(page):
        log.warning("503 on services page.")
        return False

    return True


# ---------------------------------------------------------------------------
# Check for available slots
# ---------------------------------------------------------------------------

def check_and_book(page: Page, cfg: dict) -> bool:
    """
    Scan the booking calendar for available slots and book the first one.
    Returns True if appointment was successfully booked.
    """
    content = page.content()

    # Detect "no availability" messages (Italian + English)
    no_avail_phrases = [
        "non ci sono appuntamenti disponibili",
        "no appointments available",
        "nessuna disponibilità",
        "no availability",
        "fully booked",
        "esauriti",
    ]
    content_lower = content.lower()
    if any(p in content_lower for p in no_avail_phrases):
        log.info("No appointments available yet.")
        return False

    # Look for clickable calendar dates / time slots
    # Prenotami uses a table-based calendar; available days are clickable <td> or <a> elements
    available_slots = page.locator(
        "td.day:not(.disabled):not(.old):not(.new), "
        "td.available, "
        "a.available-slot, "
        "td[class*='available'], "
        "button.available, "
        ".fc-day:not(.fc-day-disabled)"
    )

    count = available_slots.count()
    log.info("Found %d potential available slot(s).", count)

    if count == 0:
        return False

    # Click the first available slot
    log.info("Clicking first available slot…")
    available_slots.first.click()
    wait_for_stable_page(page)

    # Look for a time-slot picker or confirmation button
    time_options = page.locator(
        "input[type='radio'], "
        "button.time-slot, "
        ".orario, "
        "td.slot, "
        "li.slot-time"
    )
    if time_options.count() > 0:
        log.info("Selecting first available time…")
        time_options.first.click()
        wait_for_stable_page(page)

    # Fill any required notes / remarks field
    notes_field = page.locator("textarea[name='Notes'], textarea[name='note'], #Notes")
    if notes_field.count() > 0:
        notes_field.first.fill("Appointment booking via automated scheduler.")

    # Confirm / submit
    confirm_btn = page.locator(
        "button:has-text('Prenota'), "
        "button:has-text('Confirm'), "
        "button:has-text('Conferma'), "
        "input[type='submit'], "
        "button[type='submit']"
    )
    if confirm_btn.count() > 0:
        log.info("Submitting booking confirmation…")
        confirm_btn.first.click()
        wait_for_stable_page(page)
    else:
        log.warning("No confirmation button found. Manual review may be needed.")
        page.screenshot(path="booking_state.png")
        return False

    # Verify booking success
    final_content = page.content().lower()
    success_phrases = [
        "appuntamento confermato",
        "booking confirmed",
        "prenotazione effettuata",
        "successfully booked",
        "confirmation",
        "conferma",
        "ricevuta",
    ]
    if any(p in final_content for p in success_phrases):
        log.info("APPOINTMENT BOOKED SUCCESSFULLY!")
        page.screenshot(path="booking_confirmed.png")
        return True

    # Check if we ended up on an unexpected page
    if is_503(page):
        log.warning("503 after booking attempt.")
        return False

    # Ambiguous — save screenshot for review
    log.warning("Booking outcome unclear. Saving screenshot for review.")
    page.screenshot(path="booking_state.png")
    return False


# ---------------------------------------------------------------------------
# Core retry polling loop
# ---------------------------------------------------------------------------

def poll_until_booked(cfg: dict) -> bool:
    """
    Spin up a browser session and aggressively poll for an appointment.
    Handles 503s by reloading; re-logs in if session expires.
    Returns True if an appointment was booked.
    """
    max_503_retries = cfg["max_503_retries_on_503"] if "max_503_retries_on_503" in cfg else cfg["max_retries_on_503"]
    poll_interval = cfg["poll_interval_seconds"]
    retry_503 = 0
    attempt = 0

    with sync_playwright() as pw:
        browser, context = launch_browser(pw, cfg)
        page = context.new_page()

        try:
            # Initial login
            logged_in = False
            for _ in range(5):
                if login(page, cfg):
                    logged_in = True
                    break
                log.warning("Login attempt failed, retrying in 5s…")
                time.sleep(5)

            if not logged_in:
                log.error("Could not log in after multiple attempts. Aborting.")
                return False

            if not navigate_to_service(page, cfg):
                log.warning("Could not navigate to service page initially.")

            while retry_503 < max_503_retries:
                attempt += 1
                log.info("Poll attempt #%d (503 retries: %d/%d)…", attempt, retry_503, max_503_retries)

                # Reload the booking page to get fresh data
                try:
                    page.reload(wait_until="domcontentloaded", timeout=20_000)
                    wait_for_stable_page(page, timeout=10_000)
                except Exception as exc:
                    log.warning("Reload error: %s", exc)

                # Detect 503
                if is_503(page):
                    retry_503 += 1
                    log.warning("503 detected (%d/%d). Waiting %ds before retry…",
                                retry_503, max_503_retries, poll_interval)
                    time.sleep(poll_interval)
                    continue

                # Detect logged-out state and re-login
                if "login" in page.url.lower() or "home" in page.url.lower():
                    log.info("Session expired, re-logging in…")
                    if not login(page, cfg):
                        log.warning("Re-login failed. Retrying…")
                        time.sleep(5)
                        retry_503 += 1
                        continue
                    navigate_to_service(page, cfg)

                # Try to book
                retry_503 = 0  # Reset 503 counter on successful page load
                if check_and_book(page, cfg):
                    return True

                log.info("No slot grabbed. Waiting %ds before next poll…", poll_interval)
                time.sleep(poll_interval)

            log.error("Exceeded maximum 503 retries. Giving up.")
            return False

        finally:
            try:
                browser.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Entry point (direct run — skips time gating)
# ---------------------------------------------------------------------------

def run_now(cfg: dict) -> None:
    log.info("Starting appointment polling NOW (no time gate).")
    booked = poll_until_booked(cfg)
    if booked:
        notify(cfg, "Appointment Booked!", "Your appointment on prenotami.esteri.it has been confirmed!")
        log.info("Done — appointment secured.")
    else:
        notify(cfg, "Appointment NOT Booked", "The scheduler finished without securing an appointment.")
        log.warning("Done — no appointment was secured.")


if __name__ == "__main__":
    cfg = load_config()
    run_now(cfg)
