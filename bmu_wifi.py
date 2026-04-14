#!/usr/bin/env python3
"""
BMU Wi-Fi Auto-Login
--------------------
Automatically logs into the BMU Cyberoam captive portal and keeps you connected.

Setup (first time):
    python bmu_wifi.py --setup

Run in background (auto-reconnect every 15 sec):
    pythonw bmu_wifi.pyw          # silent, no console window
    python bmu_wifi.py            # with console output

Other options:
    python bmu_wifi.py --logout
    python bmu_wifi.py --loop 5   # re-login every 5 minutes

Install dependency:
    pip install requests
"""

import argparse
import configparser
import getpass
import os
import sys
import time

import requests
import urllib3

urllib3.disable_warnings()

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.ini")
LOGIN_URL   = "https://bmunet.bmu.edu.in:8090/login.xml"
CHECK_URL   = "http://connectivitycheck.gstatic.com/generate_204"
TIMEOUT     = 10


# ─────────────────────────── credentials ───────────────────────────

def load_credentials():
    """Read saved credentials, or run setup if missing."""
    if not os.path.exists(CONFIG_FILE):
        print("No credentials found. Running first-time setup...\n")
        return setup()
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE)
    try:
        return cfg["credentials"]["username"], cfg["credentials"]["password"]
    except KeyError:
        print("credentials.ini is malformed. Re-running setup...\n")
        return setup()


def setup():
    """Prompt for credentials and save them to credentials.ini."""
    print("BMU Wi-Fi Auto-Login — First-time Setup")
    print("Enter your BMU portal credentials (same as what you type on the login page).")
    username = input("Username: ").strip()
    password = getpass.getpass("Password: ")

    cfg = configparser.ConfigParser()
    cfg["credentials"] = {"username": username, "password": password}
    with open(CONFIG_FILE, "w") as f:
        cfg.write(f)
    print(f"\nCredentials saved to {CONFIG_FILE}")
    print("(This file is gitignored — it stays on your machine only.)\n")
    return username, password


# ─────────────────────────── network ───────────────────────────

def is_connected():
    """Return True only when real internet is reachable (not just captive portal)."""
    try:
        r = requests.get(CHECK_URL, timeout=5, allow_redirects=False)
        return r.status_code == 204
    except Exception:
        return False


def do_login(username, password):
    try:
        requests.post(
            LOGIN_URL,
            data={
                "mode": "191",
                "username": username,
                "password": password,
                "a": str(int(time.time() * 1000)),
                "producttype": "0",
            },
            verify=False,
            timeout=TIMEOUT,
        )
        print(f"[{_ts()}] Logged in.")
    except Exception as e:
        print(f"[{_ts()}] Login failed: {e}")


def do_logout(username, password):
    try:
        requests.post(
            LOGIN_URL,
            data={
                "mode": "193",
                "username": username,
                "password": password,
                "a": str(int(time.time() * 1000)),
                "producttype": "0",
            },
            verify=False,
            timeout=TIMEOUT,
        )
        print(f"[{_ts()}] Logged out.")
    except Exception as e:
        print(f"[{_ts()}] Logout failed: {e}")


def _ts():
    return time.strftime("%H:%M:%S")


# ─────────────────────────── modes ───────────────────────────

def run_auto(username, password, interval=15):
    """
    Default mode: login once on start, then check every `interval` seconds
    and re-login if the connection drops.
    """
    print(f"[{_ts()}] Auto-login started. Checking every {interval}s. Press Ctrl-C to stop.")
    if not is_connected():
        do_login(username, password)

    while True:
        time.sleep(interval)
        if not is_connected():
            print(f"[{_ts()}] Connection lost — re-logging in...")
            do_login(username, password)


def run_loop(username, password, minutes):
    """Re-login every N minutes unconditionally (ignores connectivity check)."""
    interval = max(minutes, 1) * 60
    print(f"[{_ts()}] Loop mode: re-login every {minutes} min. Press Ctrl-C to stop.")
    while True:
        do_login(username, password)
        time.sleep(interval)


# ─────────────────────────── entry point ───────────────────────────

def main():
    ap = argparse.ArgumentParser(description="BMU Cyberoam Wi-Fi auto-login")
    ap.add_argument("--setup",  action="store_true", help="re-enter credentials")
    ap.add_argument("--logout", action="store_true", help="logout instead of login")
    ap.add_argument("--loop",   type=int, metavar="MINUTES",
                    help="force re-login every N minutes (instead of smart check)")
    ap.add_argument("--interval", type=int, default=15, metavar="SECONDS",
                    help="connectivity check interval in seconds (default: 15)")
    args = ap.parse_args()

    if args.setup:
        setup()
        return

    username, password = load_credentials()

    if args.logout:
        do_logout(username, password)
    elif args.loop:
        run_loop(username, password, args.loop)
    else:
        run_auto(username, password, args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
