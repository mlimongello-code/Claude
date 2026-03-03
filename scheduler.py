"""
Scheduler Runner
================
Runs the appointment agent daily, waking up before the 6pm EST release window.

Usage:
    python3 scheduler.py            # Wait for next 6pm window, then poll
    python3 scheduler.py --now      # Skip time gate and start polling immediately
    python3 scheduler.py --once     # Run once at the next window, then exit
"""

import argparse
import datetime
import logging
import sys
import time
import zoneinfo

from agent import load_config, run_now, poll_until_booked, notify

log = logging.getLogger("scheduler")


def next_release_time(cfg: dict) -> datetime.datetime:
    """
    Compute the next upcoming daily release datetime in the configured timezone.
    """
    tz = zoneinfo.ZoneInfo(cfg["timezone"])
    now = datetime.datetime.now(tz)

    release = now.replace(
        hour=cfg["daily_release_hour"],
        minute=cfg["daily_release_minute"],
        second=0,
        microsecond=0,
    )

    # If today's window has already passed, move to tomorrow
    if now >= release:
        release += datetime.timedelta(days=1)

    return release


def sleep_until(target: datetime.datetime, cfg: dict) -> None:
    """
    Sleep until `target`, printing a countdown every 60 seconds.
    Wakes up `ramp_up_minutes_before` minutes early to allow login.
    """
    ramp_up = datetime.timedelta(minutes=cfg["ramp_up_minutes_before"])
    wake_time = target - ramp_up

    tz = zoneinfo.ZoneInfo(cfg["timezone"])
    now = datetime.datetime.now(tz)

    if now >= wake_time:
        log.info("Already past wake time — starting immediately.")
        return

    log.info(
        "Next release: %s. Waking up at %s (%d min early).",
        target.strftime("%Y-%m-%d %H:%M:%S %Z"),
        wake_time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        cfg["ramp_up_minutes_before"],
    )

    while True:
        now = datetime.datetime.now(tz)
        remaining = (wake_time - now).total_seconds()
        if remaining <= 0:
            break
        sleep_for = min(remaining, 60)
        if remaining > 120:
            log.info("Sleeping… %.0f minutes until wake-up.", remaining / 60)
        else:
            log.info("Waking up in %.0f seconds…", remaining)
        time.sleep(sleep_for)


def run_at_release_time(cfg: dict) -> bool:
    """
    Wait for the next 6pm release window and then poll aggressively.
    Returns True if appointment was booked.
    """
    release = next_release_time(cfg)
    sleep_until(release, cfg)

    log.info("Release window approaching — starting polling loop.")
    booked = poll_until_booked(cfg)
    return booked


def main() -> None:
    parser = argparse.ArgumentParser(description="Prenotami appointment scheduler")
    parser.add_argument("--now", action="store_true",
                        help="Skip time gate and start polling immediately")
    parser.add_argument("--once", action="store_true",
                        help="Run for one release window only, then exit")
    parser.add_argument("--config", default="config.json",
                        help="Path to config file (default: config.json)")
    args = parser.parse_args()

    cfg = load_config(args.config)

    if args.now:
        log.info("--now flag: skipping time gate.")
        run_now(cfg)
        return

    log.info("Prenotami Appointment Scheduler started.")
    log.info("Will wake up %d minutes before %02d:%02d %s daily.",
             cfg["ramp_up_minutes_before"],
             cfg["daily_release_hour"],
             cfg["daily_release_minute"],
             cfg["timezone"])

    while True:
        booked = run_at_release_time(cfg)

        if booked:
            notify(cfg,
                   "Appointment Booked!",
                   "Your appointment on prenotami.esteri.it has been confirmed!")
            log.info("Appointment secured — scheduler exiting.")
            break

        notify(cfg,
               "Appointment NOT Booked",
               "Today's release window closed without securing an appointment. "
               "Will try again tomorrow.")
        log.warning("No appointment secured this window.")

        if args.once:
            log.info("--once flag set — exiting after single window.")
            break

        log.info("Will retry at next release window.")


if __name__ == "__main__":
    main()
