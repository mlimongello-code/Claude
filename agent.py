"""
Prenotami Appointment Scheduler Agent
======================================
Automates appointment booking on https://prenotami.esteri.it

Full booking flow:
  1. Login at /Home  (reCAPTCHA-gated)
  2. Navigate to /Services/Booking/{service_id}
  3. Dismiss "no availability" dialog if shown  ← new
  4. Fill service-specific form (dropdowns, text, files, privacy)  ← new
  5. Click "Avanti" (Next) → land on calendar
  6. Find green (available) days; navigate months if needed  ← improved
  7. Click day → select time slot
  8. Submit → site emails an OTP
  9. Enter OTP in the popup modal  ← new
  10. Booking confirmed

reCAPTCHA options (priority order):
  A. 2captcha  — set "two_captcha_api_key" in config.json
  B. Automatic — realistic browser profile sometimes auto-passes reCAPTCHA v2
  C. Manual    — set "headless": false, solve in the browser window

OTP options:
  A. IMAP auto-retrieve — set imap_* keys in config.json
  B. Manual             — terminal prompt (requires you to be present)
"""

import email as email_lib
import imaplib
import json
import logging
import os
import re
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

BASE_URL  = "https://prenotami.esteri.it"
LOGIN_URL = f"{BASE_URL}/Home"


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

    # Defaults
    cfg.setdefault("two_captcha_api_key", None)
    cfg.setdefault("max_retries_on_503", 300)
    cfg.setdefault("poll_interval_seconds", 3)
    cfg.setdefault("ramp_up_minutes_before", 10)
    cfg.setdefault("timezone", "America/New_York")
    cfg.setdefault("daily_release_hour", 18)
    cfg.setdefault("daily_release_minute", 0)
    cfg.setdefault("headless", True)
    cfg.setdefault("notify_webhook", None)
    cfg.setdefault("form_fields", {})
    cfg.setdefault("file_uploads", {})
    cfg.setdefault("otp_delay_seconds", 30)
    cfg.setdefault("imap_host", None)
    cfg.setdefault("imap_user", None)
    cfg.setdefault("imap_password", None)
    cfg.setdefault("max_months_to_check", 3)
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
# Browser
# ---------------------------------------------------------------------------

def launch_browser(playwright, cfg: dict):
    """Chromium with human-like fingerprint to help auto-pass reCAPTCHA."""
    browser = playwright.chromium.launch(
        headless=cfg["headless"],
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
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
    context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins',   { get: () => [1,2,3,4,5] });
        Object.defineProperty(navigator, 'languages', { get: () => ['it-IT','it','en-US','en'] });
        window.chrome = { runtime: {} };
    """)
    return browser, context


# ---------------------------------------------------------------------------
# Page utilities
# ---------------------------------------------------------------------------

def is_503(page: Page) -> bool:
    try:
        content = page.content().lower()
        for ind in ["503", "service unavailable", "too many requests",
                    "429", "temporarily unavailable"]:
            if ind in content or ind in page.title().lower():
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
    try:
        content = page.content().lower()
        return "login-email" not in content and "login-password" not in content
    except Exception:
        return True


def dismiss_dialog(page: Page) -> bool:
    """
    Dismiss the 'no appointments available' modal dialog.
    Returns True if a dialog was found and dismissed.

    Dialog structure:
      <div role="dialog"> ... <button>ok</button> </div>
    """
    try:
        dialog = page.locator("div[role='dialog']")
        if dialog.count() == 0:
            return False
        ok_btn = dialog.locator("button:has-text('ok'), button:has-text('Ok'), button:has-text('OK')")
        if ok_btn.count() > 0:
            log.info("Dismissing 'no availability' dialog.")
            ok_btn.first.click()
            time.sleep(0.5)
            return True
        # Try any button inside the dialog
        any_btn = dialog.locator("button")
        if any_btn.count() > 0:
            log.info("Dismissing dialog (generic close).")
            any_btn.first.click()
            time.sleep(0.5)
            return True
    except Exception as exc:
        log.debug("dismiss_dialog error: %s", exc)
    return False


# ---------------------------------------------------------------------------
# reCAPTCHA
# ---------------------------------------------------------------------------

def _get_sitekey(page: Page) -> Optional[str]:
    for sel in ["[data-sitekey]", ".g-recaptcha"]:
        try:
            el = page.locator(sel)
            if el.count() > 0:
                key = el.first.get_attribute("data-sitekey")
                if key:
                    return key
        except Exception:
            pass
    return None


def _solve_2captcha(page: Page, api_key: str) -> bool:
    sitekey = _get_sitekey(page)
    if not sitekey:
        log.warning("reCAPTCHA sitekey not found.")
        return False

    log.info("Submitting reCAPTCHA to 2captcha…")
    try:
        submit_url = (
            "http://2captcha.com/in.php?" +
            urllib.parse.urlencode({
                "key": api_key, "method": "userrecaptcha",
                "googlekey": sitekey, "pageurl": page.url, "json": 1,
            })
        )
        with urllib.request.urlopen(submit_url, timeout=30) as r:
            res = json.loads(r.read())
        if res.get("status") != 1:
            log.warning("2captcha submit failed: %s", res)
            return False
        task_id = res["request"]
        log.info("2captcha task id=%s. Polling…", task_id)
    except Exception as exc:
        log.warning("2captcha submit error: %s", exc)
        return False

    for _ in range(24):
        time.sleep(5)
        try:
            poll_url = (
                "http://2captcha.com/res.php?" +
                urllib.parse.urlencode({
                    "key": api_key, "action": "get",
                    "id": task_id, "json": 1,
                })
            )
            with urllib.request.urlopen(poll_url, timeout=15) as r:
                res = json.loads(r.read())
            if res.get("status") == 1:
                token = res["request"]
                log.info("reCAPTCHA solved. Injecting token…")
                page.evaluate(f"""() => {{
                    const ta = document.getElementById('g-recaptcha-response');
                    if (ta) ta.innerHTML = '{token}';
                    if (typeof ___grecaptcha_cfg !== 'undefined') {{
                        Object.values(___grecaptcha_cfg.clients || {{}}).forEach(c => {{
                            if (c && c.S && typeof c.S.callback === 'function')
                                c.S.callback('{token}');
                        }});
                    }}
                }}""")
                return True
            if res.get("request") == "ERROR_CAPTCHA_UNSOLVABLE":
                log.warning("2captcha: unsolvable.")
                return False
        except Exception as exc:
            log.debug("2captcha poll: %s", exc)

    log.warning("2captcha timed out.")
    return False


def handle_recaptcha(page: Page, cfg: dict) -> None:
    api_key = cfg.get("two_captcha_api_key")
    if api_key:
        if not _solve_2captcha(page, api_key):
            log.warning("2captcha failed — submitting anyway.")
    elif not cfg["headless"]:
        log.info("Please solve the reCAPTCHA in the browser, then press Enter here.")
        input("Press Enter after solving the reCAPTCHA…")
    else:
        log.warning(
            "No 2captcha key and headless=true — login may fail due to reCAPTCHA. "
            "Set two_captcha_api_key or headless=false."
        )


# ---------------------------------------------------------------------------
# OTP retrieval
# ---------------------------------------------------------------------------

def _get_otp_via_imap(cfg: dict, timeout: int = 120) -> Optional[str]:
    """
    Poll IMAP inbox for an email from prenotami containing an OTP code.
    Returns the OTP string, or None on failure.
    """
    host     = cfg.get("imap_host")
    user     = cfg.get("imap_user")
    password = cfg.get("imap_password")
    if not all([host, user, password]):
        return None

    log.info("Polling IMAP (%s) for OTP email…", host)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with imaplib.IMAP4_SSL(host) as imap:
                imap.login(user, password)
                imap.select("INBOX")
                # Search for recent unseen mail from prenotami
                _, msg_ids = imap.search(None, '(UNSEEN FROM "prenotami")')
                ids = msg_ids[0].split()
                if ids:
                    _, msg_data = imap.fetch(ids[-1], "(RFC822)")
                    raw = msg_data[0][1]
                    msg = email_lib.message_from_bytes(raw)
                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                body = part.get_payload(decode=True).decode(errors="replace")
                                break
                    else:
                        body = msg.get_payload(decode=True).decode(errors="replace")

                    # Extract OTP — typically a 5-6 digit code
                    match = re.search(r"\b(\d{5,8})\b", body)
                    if match:
                        otp = match.group(1)
                        log.info("OTP retrieved via IMAP: %s", otp)
                        return otp
        except Exception as exc:
            log.warning("IMAP error: %s", exc)

        time.sleep(5)

    log.warning("IMAP OTP retrieval timed out.")
    return None


def handle_otp(page: Page, cfg: dict) -> bool:
    """
    After calendar slot selection, the site shows an OTP popup.
    Retrieves the OTP (via IMAP or manual input) and submits it.
    Returns True on success.
    """
    # Wait for OTP dialog to appear
    try:
        page.wait_for_selector(
            "div[role='dialog'] input, input[id*='otp' i], input[name*='otp' i]",
            timeout=15_000,
        )
    except PlaywrightTimeout:
        log.info("No OTP dialog appeared — may not be required for this service.")
        return True

    log.info("OTP dialog detected.")

    otp_delay = cfg.get("otp_delay_seconds", 30)
    otp_code: Optional[str] = None

    # Try IMAP first
    if cfg.get("imap_host"):
        log.info("Waiting %ds then fetching OTP via IMAP…", otp_delay)
        time.sleep(otp_delay)
        otp_code = _get_otp_via_imap(cfg)

    # Fall back to manual entry
    if not otp_code:
        log.info(
            "Check your email for an OTP from prenotami.esteri.it. "
            "You have ~2 minutes before the session expires."
        )
        otp_code = input("Enter the OTP code from your email: ").strip()

    if not otp_code:
        log.error("No OTP provided — cannot complete booking.")
        return False

    # Fill OTP field inside the dialog
    try:
        otp_field = page.locator(
            "div[role='dialog'] input, "
            "input[id*='otp' i], input[name*='otp' i], "
            "input[placeholder*='otp' i], input[placeholder*='codice' i]"
        )
        otp_field.first.fill(otp_code)
        time.sleep(0.5)

        ok_btn = page.locator(
            "div[role='dialog'] button:has-text('ok'), "
            "div[role='dialog'] button:has-text('Ok'), "
            "div[role='dialog'] button:has-text('Conferma'), "
            "div[role='dialog'] button[type='submit']"
        )
        if ok_btn.count() > 0:
            ok_btn.first.click()
            wait_for_page(page)
            log.info("OTP submitted.")
            return True
        else:
            log.warning("OTP OK button not found.")
            page.screenshot(path="otp_state.png")
            return False
    except Exception as exc:
        log.warning("OTP submission error: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def login(page: Page, cfg: dict) -> bool:
    log.info("Loading login page…")
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        wait_for_page(page)
    except Exception as exc:
        log.warning("Login page load failed: %s", exc)
        return False

    if is_503(page):
        log.warning("503 on login page.")
        return False

    # Cookie consent
    try:
        btn = page.locator("#cookieAccept, button:has-text('Accetto'), button:has-text('Accept')")
        if btn.count() > 0:
            btn.first.click()
            time.sleep(0.3)
    except Exception:
        pass

    # Wait for form
    try:
        page.wait_for_selector("#login-email", timeout=15_000)
    except PlaywrightTimeout:
        log.warning("Login form not visible. URL: %s", page.url)
        return False

    page.fill("#login-email", cfg["email"])
    time.sleep(0.3)
    page.fill("#login-password", cfg["password"])
    time.sleep(0.5)

    handle_recaptcha(page, cfg)

    log.info("Clicking login button…")
    try:
        page.locator("button.g-recaptcha, button[type='submit'], input[type='submit']").first.click()
        wait_for_page(page)
    except Exception as exc:
        log.warning("Login submit error: %s", exc)
        return False

    content = page.content().lower()
    if "login-email" in content or "login-password" in content:
        log.error("Login failed — still on login page (wrong credentials or reCAPTCHA blocked).")
        page.screenshot(path="login_failed.png")
        return False

    log.info("Login successful. URL: %s", page.url)
    return True


# ---------------------------------------------------------------------------
# Navigate to booking page
# ---------------------------------------------------------------------------

def go_to_booking_page(page: Page, cfg: dict) -> bool:
    url = f"{BASE_URL}/Services/Booking/{cfg['service_id']}"
    log.info("Navigating to: %s", url)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        wait_for_page(page)
    except Exception as exc:
        log.warning("Navigation error: %s", exc)
        return False

    if is_503(page):
        log.warning("503 on booking page.")
        return False

    if not is_logged_in(page):
        log.warning("Redirected to login — session expired.")
        return False

    return True


# ---------------------------------------------------------------------------
# Service form (before calendar)
# ---------------------------------------------------------------------------

def fill_service_form(page: Page, cfg: dict) -> bool:
    """
    Fill the service-specific pre-booking form, then click 'Avanti' (Next).

    Config keys under "form_fields":
      "ddls_0": "Si"          — answer to first dropdown question
      "ddls_1": "No"          — answer to second dropdown question
      "ddls_4": "Celibe/Nubile"  — marital status dropdown
      "DatiAddizionaliPrenotante_2___testo": "0"    — number of children
      "DatiAddizionaliPrenotante_3___testo": "123 Main St"  — address

    Config keys under "file_uploads":
      "File_0": "/absolute/path/to/identity.pdf"
      "File_1": "/absolute/path/to/residence.pdf"
    """
    # Check if this page has the form (look for #btnAvanti)
    try:
        page.wait_for_selector("#btnAvanti, #PrivacyCheck", timeout=8_000)
    except PlaywrightTimeout:
        # No form — maybe we're already on the calendar
        log.info("No service form found — skipping form step.")
        return True

    log.info("Filling service form…")
    form_fields = cfg.get("form_fields", {})
    file_uploads = cfg.get("file_uploads", {})

    # Fill dropdowns
    for field_id, value in form_fields.items():
        if not value:
            continue
        try:
            el = page.locator(f"#{field_id}")
            if el.count() == 0:
                log.debug("Form field #%s not found, skipping.", field_id)
                continue
            tag = el.evaluate("e => e.tagName.toLowerCase()")
            if tag == "select":
                el.select_option(label=str(value))
                log.info("Set #%s = '%s'", field_id, value)
            else:
                el.fill(str(value))
                log.info("Filled #%s = '%s'", field_id, value)
        except Exception as exc:
            log.warning("Could not fill #%s: %s", field_id, exc)

    # File uploads
    for field_id, file_path in file_uploads.items():
        if not file_path:
            continue
        abs_path = str(Path(file_path).resolve())
        if not Path(abs_path).exists():
            log.warning("File for #%s not found: %s", field_id, abs_path)
            continue
        try:
            page.set_input_files(f"#{field_id}", abs_path)
            log.info("Uploaded file for #%s: %s", field_id, abs_path)
        except Exception as exc:
            log.warning("File upload error for #%s: %s", field_id, exc)

    # Privacy checkbox
    try:
        privacy = page.locator("#PrivacyCheck")
        if privacy.count() > 0 and not privacy.is_checked():
            privacy.check()
            log.info("Privacy checkbox checked.")
    except Exception as exc:
        log.warning("Privacy checkbox error: %s", exc)

    # Click "Avanti" (Next/Forward)
    try:
        page.locator("#btnAvanti").click()
        wait_for_page(page)
        log.info("Clicked Avanti — on calendar page now.")
    except Exception as exc:
        log.warning("Could not click #btnAvanti: %s", exc)
        page.screenshot(path="form_state.png")
        return False

    return True


# ---------------------------------------------------------------------------
# Calendar — find green days across multiple months
# ---------------------------------------------------------------------------

def find_and_book_slot(page: Page, cfg: dict) -> bool:
    """
    Search the calendar for available (green) days.
    Navigates forward through months (up to max_months_to_check).
    Clicks first available day → first time slot → submits → handles OTP.
    Returns True if appointment was fully booked.
    """
    max_months = cfg.get("max_months_to_check", 3)

    for month_offset in range(max_months):
        if month_offset > 0:
            # Navigate to next month
            try:
                forward_btn = page.locator(
                    "th.next, .datepicker-days th.next, "
                    "button.next-month, .calendar-nav-next, "
                    "[aria-label*='next' i], [title*='next' i]"
                )
                if forward_btn.count() == 0:
                    log.info("No forward button found — only one month available.")
                    break
                forward_btn.first.click()
                time.sleep(1)
            except Exception as exc:
                log.warning("Could not navigate to next month: %s", exc)
                break

        # Look for green/available day cells
        # Prenotami uses Bootstrap datepicker — available days are td.day without .disabled/.old/.new
        # The "green" class name varies; confirmed pattern is just non-disabled td.day
        green_days = page.locator(
            "td.day:not(.disabled):not(.old):not(.new), "
            "td.day.active:not(.disabled), "
            "td.open-day, td.available-day, "
            ".datepicker td.day:not([class*='disabled']):not([class*='old']):not([class*='new'])"
        )
        count = green_days.count()
        log.info("Month +%d: found %d available day(s).", month_offset, count)

        if count > 0:
            log.info("Clicking first available day…")
            green_days.first.click()
            wait_for_page(page, timeout=8_000)
            return _select_time_and_confirm(page, cfg)

    log.info("No available days found in %d month(s).", max_months)
    return False


def _select_time_and_confirm(page: Page, cfg: dict) -> bool:
    """
    After clicking an available day: select first time slot, submit, handle OTP.
    Returns True if booking is confirmed.
    """
    # Select first available time slot
    time_slots = page.locator(
        "input[type='radio'], "
        ".orario, button.orario, "
        ".slot-time, button.time-slot, "
        "li.slot, td.slot"
    )
    if time_slots.count() > 0:
        log.info("Selecting first time slot…")
        time_slots.first.click()
        time.sleep(0.5)

    # Fill optional notes field
    notes = page.locator("#Notes, textarea[name='Notes'], textarea[name='note']")
    if notes.count() > 0:
        notes.first.fill("Automated booking")

    # Confirm / submit
    confirm_btn = page.locator(
        "button:has-text('Prenota'), "
        "button:has-text('Conferma'), "
        "button:has-text('Confirm'), "
        "button:has-text('Book'), "
        "input[type='submit'], button[type='submit']"
    )
    if confirm_btn.count() == 0:
        log.warning("No confirmation button found.")
        page.screenshot(path="booking_state.png")
        return False

    log.info("Submitting booking…")
    confirm_btn.first.click()
    wait_for_page(page)

    # Handle OTP step
    if not handle_otp(page, cfg):
        log.warning("OTP step failed.")
        return False

    # Verify success
    final = page.content().lower()
    success_phrases = [
        "appuntamento confermato", "booking confirmed",
        "prenotazione effettuata", "successfully booked",
        "conferma", "ricevuta", "receipt",
    ]
    if any(p in final for p in success_phrases):
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
    Main loop: login → navigate → dismiss dialog → fill form →
    poll calendar → book. Handles 503s and session expiry.
    Returns True if appointment was secured.
    """
    max_503    = cfg["max_retries_on_503"]
    interval   = cfg["poll_interval_seconds"]
    streak_503 = 0
    attempt    = 0

    with sync_playwright() as pw:
        browser, context = launch_browser(pw, cfg)
        page = context.new_page()

        try:
            # Login
            for i in range(1, 6):
                if login(page, cfg):
                    break
                log.warning("Login attempt %d/5 failed. Retrying in 10s…", i)
                time.sleep(10)
            else:
                log.error("Could not log in after 5 attempts.")
                return False

            # Navigate to booking page
            go_to_booking_page(page, cfg)

            # Dismiss initial "no availability" dialog if present
            dismiss_dialog(page)

            # Fill the service form (dropdowns, uploads, privacy, Avanti)
            if not fill_service_form(page, cfg):
                log.error("Service form could not be filled. Check form_fields config.")
                return False

            # --- Polling loop ---
            while streak_503 < max_503:
                attempt += 1
                log.info("Poll #%d (503 streak: %d/%d)…", attempt, streak_503, max_503)

                # Try to find and book a slot on the current calendar
                if find_and_book_slot(page, cfg):
                    return True

                # No slot found — reload to get a fresh calendar
                log.info("No slot. Waiting %ds then reloading…", interval)
                time.sleep(interval)

                try:
                    page.reload(wait_until="domcontentloaded", timeout=20_000)
                    wait_for_page(page, timeout=10_000)
                except Exception as exc:
                    log.warning("Reload error: %s", exc)

                if is_503(page):
                    streak_503 += 1
                    log.warning("503 (#%d/%d).", streak_503, max_503)
                    continue

                streak_503 = 0

                # Re-login if session expired
                if not is_logged_in(page):
                    log.info("Session expired — re-logging in…")
                    for _ in range(3):
                        if login(page, cfg):
                            go_to_booking_page(page, cfg)
                            dismiss_dialog(page)
                            fill_service_form(page, cfg)
                            break
                        time.sleep(5)
                    continue

                # Dismiss any "no availability" dialog that appeared after reload
                dismiss_dialog(page)

            log.error("Exceeded max 503 retries. Stopping.")
            return False

        finally:
            try:
                context.close()
                browser.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Entry point
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
