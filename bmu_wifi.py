#!/usr/bin/env python3
"""
BMU Wi-Fi Auto-Login
--------------------
Automatically logs into the BMU Cyberoam captive portal and keeps you connected.

First-time setup:
    python bmu_wifi.py --setup

Run (auto-reconnect, checks every 15 sec):
    python bmu_wifi.py
    pythonw bmu_wifi.pyw        # silent background, no console window

Stop / uninstall:
    python bmu_wifi.py --stop               # stop the background instance
    python bmu_wifi.py --logout             # logout from the portal
    python bmu_wifi.py --disable-startup    # remove from Windows startup

Startup:
    python bmu_wifi.py --enable-startup     # add to Windows startup (auto-run on login)
    python bmu_wifi.py --disable-startup    # remove from Windows startup

Other commands:
    python bmu_wifi.py --interval 30        # check every 30 sec
    python bmu_wifi.py --loop 5             # force re-login every 5 min
    python bmu_wifi.py --setup-primary      # update primary credentials only
    python bmu_wifi.py --setup-secondary    # add / update secondary credentials
    python bmu_wifi.py --clear-secondary    # remove secondary credentials
    python bmu_wifi.py --show-config        # print config (passwords masked)
    python bmu_wifi.py --verbose            # enable DEBUG console output

Install dependency:
    pip install requests
"""

import argparse
import base64
import configparser
import enum
import getpass
import logging
import logging.handlers
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

import requests
import urllib3

urllib3.disable_warnings()

# ─────────────────────────── constants ───────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "credentials.ini")
LOCK_FILE   = os.path.join(tempfile.gettempdir(), "bmu_wifi.lock")
LOG_FILE    = os.path.join(BASE_DIR, "bmu_wifi.log")

DEFAULT_LOGIN_URL = "https://bmunet.bmu.edu.in:8090/login.xml"
DEFAULT_CHECK_URL = "http://connectivitycheck.gstatic.com/generate_204"

TIMEOUT        = 10
BACKOFF_BASE   = 5
BACKOFF_MAX    = 300
BACKOFF_FACTOR = 2

# ─────────────────────────── enums ───────────────────────────────

class NetworkState(enum.Enum):
    INTERNET_OK    = "internet_ok"
    CAPTIVE_PORTAL = "captive_portal"
    NO_WIFI        = "no_wifi"
    UNKNOWN        = "unknown"


class LoginResult(enum.Enum):
    SUCCESS        = "success"
    ALREADY_LIVE   = "already_live"
    WRONG_CREDS    = "wrong_creds"
    LIMIT_REACHED  = "limit_reached"
    NETWORK_ERROR  = "network_error"
    UNEXPECTED     = "unexpected"


# ─────────────────────────── globals ─────────────────────────────

logger: logging.Logger = logging.getLogger("bmu_wifi")
_lock_fd = None


# ─────────────────────────── logging ─────────────────────────────

def setup_logging(log_file: str = LOG_FILE, verbose: bool = False) -> logging.Logger:
    log = logging.getLogger("bmu_wifi")
    log.setLevel(logging.DEBUG)
    log.handlers.clear()

    fmt_file    = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                    datefmt="%Y-%m-%d %H:%M:%S")
    fmt_console = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S")

    # File handler — always on
    try:
        fh = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=1_048_576, backupCount=3, encoding="utf-8"
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt_file)
        log.addHandler(fh)
    except OSError:
        pass  # log dir not writable — console only

    # Console handler — only when a real TTY is present
    if sys.stdout is not None and getattr(sys.stdout, "isatty", lambda: False)():
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.DEBUG if verbose else logging.INFO)
        ch.setFormatter(fmt_console)
        log.addHandler(ch)

    return log


# ─────────────────────────── single-instance lock ────────────────

def acquire_lock():
    """Return an open lock-file handle, or None if another instance is running."""
    global _lock_fd
    try:
        fd = open(LOCK_FILE, "w")
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd.write(str(os.getpid()))
        fd.flush()
        _lock_fd = fd
        return fd
    except OSError:
        try:
            fd.close()
        except Exception:
            pass
        return None


def release_lock(fd) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt
            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
    except Exception:
        pass


# ─────────────────────────── config I/O ──────────────────────────

def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE)
    return cfg


def save_config(cfg: configparser.ConfigParser) -> None:
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        cfg.write(f)
    os.replace(tmp, CONFIG_FILE)


def get_login_url(cfg: configparser.ConfigParser) -> str:
    try:
        return cfg["portal"]["login_url"]
    except KeyError:
        return DEFAULT_LOGIN_URL


def get_check_url(cfg: configparser.ConfigParser) -> str:
    try:
        return cfg["portal"]["check_url"]
    except KeyError:
        return DEFAULT_CHECK_URL


def get_primary_credentials(cfg: configparser.ConfigParser):
    try:
        return cfg["credentials"]["username"], cfg["credentials"]["password"]
    except KeyError:
        return None


def get_secondary_credentials(cfg: configparser.ConfigParser):
    try:
        return cfg["secondary"]["username"], cfg["secondary"]["password"]
    except KeyError:
        return None


def load_credentials_or_setup() -> configparser.ConfigParser:
    if not os.path.exists(CONFIG_FILE):
        logger.info("No credentials.ini found — starting first-time setup.")
        setup_full()
    cfg = load_config()
    if get_primary_credentials(cfg) is None:
        logger.warning("credentials.ini is missing [credentials] — re-running setup.")
        setup_full()
        cfg = load_config()
    return cfg


# ─────────────────────────── validation ──────────────────────────

def validate_credentials(username: str, password: str):
    if not username.strip():
        return False, "Username cannot be empty."
    if not password:
        return False, "Password cannot be empty."
    return True, ""


def validate_url(url: str):
    if not url.strip():
        return False, "URL cannot be empty."
    if not url.startswith("http"):
        return False, "URL must start with http:// or https://"
    return True, ""


# ─────────────────────────── setup wizard ────────────────────────

def _is_interactive() -> bool:
    return sys.stdin is not None and getattr(sys.stdin, "isatty", lambda: False)()


def _require_interactive() -> None:
    if not _is_interactive():
        msg = (
            "ERROR: Setup requires an interactive terminal.\n"
            "Run:  python bmu_wifi.py --setup\n"
            "in a console window (not via pythonw.exe)."
        )
        logger.error(msg)
        print(msg, file=sys.stderr)
        sys.exit(1)


def _prompt_url(label: str, current: str) -> str:
    while True:
        val = input(f"{label} [{current}]: ").strip()
        if not val:
            return current
        ok, err = validate_url(val)
        if ok:
            return val
        print(f"  Error: {err}")


def _prompt_credentials(label: str, current_user: str = None):
    print(f"\n--- {label} Credentials ---")
    if current_user:
        print(f"Current username: {current_user}")
    while True:
        username = input("Username: ").strip()
        if not username and current_user:
            username = current_user           # keep existing
        password = getpass.getpass("Password: ")
        ok, err = validate_credentials(username, password)
        if ok:
            return username, password
        print(f"  Error: {err}")


def setup_full() -> None:
    _require_interactive()
    print("\nBMU Wi-Fi Auto-Login — First-time Setup")
    print("─" * 40)

    cfg = load_config()

    # Portal URL
    print("\nPortal login URL (press Enter to keep default):")
    login_url = _prompt_url("Login URL", get_login_url(cfg))
    check_url = get_check_url(cfg)   # keep existing / default

    cfg["portal"]      = {"login_url": login_url, "check_url": check_url}
    user1, pass1       = _prompt_credentials("Primary")
    cfg["credentials"] = {"username": user1, "password": pass1}

    add_secondary = input("\nAdd secondary/backup credentials? [y/N]: ").strip().lower()
    if add_secondary == "y":
        user2, pass2   = _prompt_credentials("Secondary")
        cfg["secondary"] = {"username": user2, "password": pass2}
    elif "secondary" in cfg:
        cfg.remove_section("secondary")

    save_config(cfg)
    print(f"\nConfig saved to {CONFIG_FILE}")
    print("Setup complete. Run without arguments to start auto-login.\n")


def setup_primary() -> None:
    _require_interactive()
    cfg = load_config()
    current = get_primary_credentials(cfg)
    current_user = current[0] if current else None
    user, pwd = _prompt_credentials("Update Primary", current_user)
    cfg["credentials"] = {"username": user, "password": pwd}
    save_config(cfg)
    print("Primary credentials updated.")


def setup_secondary() -> None:
    _require_interactive()
    cfg = load_config()
    current = get_secondary_credentials(cfg)
    current_user = current[0] if current else None
    print("(Leave username blank to clear secondary credentials)")
    user, pwd = _prompt_credentials("Secondary", current_user)
    if not user.strip():
        clear_secondary()
        return
    cfg["secondary"] = {"username": user, "password": pwd}
    save_config(cfg)
    print("Secondary credentials updated.")


def clear_secondary() -> None:
    cfg = load_config()
    if "secondary" in cfg:
        cfg.remove_section("secondary")
        save_config(cfg)
        print("Secondary credentials cleared.")
    else:
        print("No secondary credentials to clear.")


def show_config() -> None:
    if not os.path.exists(CONFIG_FILE):
        print("No credentials.ini found. Run --setup first.")
        return
    cfg = load_config()
    for section in cfg.sections():
        print(f"[{section}]")
        for key, val in cfg[section].items():
            display = "***" if key == "password" else val
            print(f"  {key} = {display}")
    if not cfg.sections():
        print("(empty config)")


# ─────────────────────────── network ─────────────────────────────

def get_wifi_state() -> NetworkState:
    """Check if the WiFi adapter is associated to any network (layer 2)."""
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["netsh", "wlan", "show", "interfaces"],
                capture_output=True, text=True, timeout=5
            )
            output = result.stdout.lower()
            if "state" in output and "connected" in output:
                return NetworkState.CAPTIVE_PORTAL   # L2 up, internet TBD
            return NetworkState.NO_WIFI
        else:
            sock = socket.create_connection(("8.8.8.8", 53), timeout=3)
            sock.close()
            return NetworkState.CAPTIVE_PORTAL
    except OSError as e:
        err = str(e).lower()
        if "unreachable" in err or "no route" in err or "network is down" in err:
            return NetworkState.NO_WIFI
        return NetworkState.CAPTIVE_PORTAL
    except Exception:
        return NetworkState.UNKNOWN


def check_connectivity(check_url: str) -> NetworkState:
    """Three-way check: INTERNET_OK, CAPTIVE_PORTAL, NO_WIFI, or UNKNOWN."""
    try:
        r = requests.get(check_url, timeout=5, allow_redirects=False)
        if r.status_code == 204 and len(r.content) == 0:
            return NetworkState.INTERNET_OK
        ct = r.headers.get("content-type", "")
        if r.status_code in (301, 302, 303, 307, 308) or "text/html" in ct:
            return NetworkState.CAPTIVE_PORTAL
        if r.status_code == 200 and len(r.content) == 0:
            return NetworkState.CAPTIVE_PORTAL
        return NetworkState.UNKNOWN
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        return get_wifi_state()
    except Exception:
        return NetworkState.UNKNOWN


# ─────────────────────────── login engine ────────────────────────

def parse_portal_response(text: str):
    try:
        root = ET.fromstring(text)
        msg = (root.findtext("Message") or root.findtext(".//Message") or "").strip()
    except ET.ParseError:
        return LoginResult.UNEXPECTED, f"Non-XML response: {text[:120]}"

    low = msg.lower()
    if low == "live":
        return LoginResult.ALREADY_LIVE, msg
    if "signed in" in low or "you are signed" in low:
        return LoginResult.SUCCESS, msg
    if "login failed" in low or "username or password" in low:
        return LoginResult.WRONG_CREDS, msg
    if "maximum login" in low or "limit reached" in low:
        return LoginResult.LIMIT_REACHED, msg
    return LoginResult.UNEXPECTED, msg


def do_login(username: str, password: str, login_url: str) -> LoginResult:
    payload = {
        "mode": "191",
        "username": username,
        "password": password,
        "a": str(int(time.time() * 1000)),
        "producttype": "0",
    }
    try:
        r = requests.post(login_url, data=payload, verify=False, timeout=TIMEOUT)
        result, msg = parse_portal_response(r.text)
        logger.info("Login (%s): %s — %s", username, result.value, msg or "(no message)")
        return result
    except requests.exceptions.Timeout:
        logger.warning("Login request timed out (url=%s)", login_url)
        return LoginResult.NETWORK_ERROR
    except requests.exceptions.ConnectionError as e:
        logger.warning("Login connection error: %s", e)
        return LoginResult.NETWORK_ERROR
    except Exception as e:
        logger.error("Unexpected login error: %s", e)
        return LoginResult.NETWORK_ERROR


def do_logout(username: str, password: str, login_url: str) -> bool:
    payload = {
        "mode": "193",
        "username": username,
        "password": password,
        "a": str(int(time.time() * 1000)),
        "producttype": "0",
    }
    try:
        r = requests.post(login_url, data=payload, verify=False, timeout=TIMEOUT)
        logger.info("Logout (%s): %s", username, r.text.strip()[:120])
        return True
    except Exception as e:
        logger.warning("Logout failed: %s", e)
        return False


def try_login_with_fallback(cfg: configparser.ConfigParser) -> LoginResult:
    """Try primary credentials; fall back to secondary only on WRONG_CREDS / UNEXPECTED."""
    login_url = get_login_url(cfg)
    primary   = get_primary_credentials(cfg)

    if primary is None:
        logger.error("No primary credentials configured.")
        return LoginResult.WRONG_CREDS

    result = do_login(primary[0], primary[1], login_url)

    if result in (LoginResult.SUCCESS, LoginResult.ALREADY_LIVE):
        return result

    # Only fall back to secondary when it's a credentials problem
    if result in (LoginResult.WRONG_CREDS, LoginResult.UNEXPECTED):
        secondary = get_secondary_credentials(cfg)
        if secondary is not None:
            logger.warning("Primary login failed (%s) — trying secondary credentials.", result.value)
            result2 = do_login(secondary[0], secondary[1], login_url)
            return result2
        else:
            logger.warning("Primary login failed (%s) and no secondary configured.", result.value)

    return result


# ─────────────────────────── backoff ─────────────────────────────

def backoff_delay(attempt: int) -> float:
    return min(BACKOFF_BASE * (BACKOFF_FACTOR ** attempt), BACKOFF_MAX)


# ─────────────────────────── signal handler ───────────────────────

def _shutdown_handler(signum, frame):
    logger.info("Received signal %d — shutting down.", signum)
    if _lock_fd:
        release_lock(_lock_fd)
    sys.exit(0)


# ─────────────────────────── run modes ───────────────────────────

def run_auto(cfg: configparser.ConfigParser, interval: int = 15) -> None:
    lock = acquire_lock()
    if lock is None:
        logger.error("Another instance is already running. Exiting.")
        sys.exit(1)

    try:
        signal.signal(signal.SIGTERM, _shutdown_handler)
    except (OSError, ValueError):
        pass  # signal not available in some environments

    check_url = get_check_url(cfg)
    logger.info("Auto-login started. Checking every %ds. Press Ctrl-C to stop.", interval)

    consecutive_failures = 0

    # Initial check
    state = check_connectivity(check_url)
    if state == NetworkState.INTERNET_OK:
        logger.info("Already connected to the internet.")
    elif state == NetworkState.CAPTIVE_PORTAL:
        logger.info("Captive portal detected on startup — logging in.")
        result = try_login_with_fallback(cfg)
        if result not in (LoginResult.SUCCESS, LoginResult.ALREADY_LIVE):
            consecutive_failures += 1
    elif state == NetworkState.NO_WIFI:
        logger.warning("Wi-Fi not connected. Waiting for network...")
        consecutive_failures += 1

    try:
        while True:
            sleep_time = interval if consecutive_failures == 0 else backoff_delay(consecutive_failures - 1)
            if consecutive_failures > 0:
                logger.debug("Backing off for %.0fs (failure #%d).", sleep_time, consecutive_failures)
            time.sleep(sleep_time)

            state = check_connectivity(check_url)

            if state == NetworkState.INTERNET_OK:
                if consecutive_failures > 0:
                    logger.info("Connection restored.")
                consecutive_failures = 0

            elif state == NetworkState.CAPTIVE_PORTAL:
                logger.info("Captive portal detected — logging in.")
                result = try_login_with_fallback(cfg)
                if result in (LoginResult.SUCCESS, LoginResult.ALREADY_LIVE):
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1

            elif state == NetworkState.NO_WIFI:
                logger.warning("Wi-Fi not connected (attempt %d).", consecutive_failures + 1)
                consecutive_failures += 1

            else:
                logger.debug("Network state unknown.")
                consecutive_failures += 1

    except KeyboardInterrupt:
        logger.info("Stopped by user.")
    finally:
        release_lock(lock)


def run_loop(cfg: configparser.ConfigParser, minutes: int) -> None:
    lock = acquire_lock()
    if lock is None:
        logger.error("Another instance is already running. Exiting.")
        sys.exit(1)

    try:
        signal.signal(signal.SIGTERM, _shutdown_handler)
    except (OSError, ValueError):
        pass

    interval = max(minutes, 1) * 60
    logger.info("Loop mode: re-login every %d min. Press Ctrl-C to stop.", minutes)
    consecutive_failures = 0

    try:
        while True:
            result = try_login_with_fallback(cfg)
            if result in (LoginResult.SUCCESS, LoginResult.ALREADY_LIVE):
                consecutive_failures = 0
                time.sleep(interval)
            else:
                consecutive_failures += 1
                sleep_time = backoff_delay(consecutive_failures - 1)
                logger.debug("Login failed — retrying in %.0fs.", sleep_time)
                time.sleep(sleep_time)
    except KeyboardInterrupt:
        logger.info("Stopped by user.")
    finally:
        release_lock(lock)


def do_logout_command(cfg: configparser.ConfigParser) -> None:
    primary = get_primary_credentials(cfg)
    if primary is None:
        print("No credentials configured. Run --setup first.")
        return
    do_logout(primary[0], primary[1], get_login_url(cfg))


# ─────────────────────────── stop / startup ──────────────────────

def _get_running_pid() -> int | None:
    """Read the PID written into the lock file by the running instance, or return None."""
    try:
        with open(LOCK_FILE, "r") as f:
            text = f.read().strip()
        return int(text) if text else None
    except (OSError, ValueError):
        return None


def stop_background() -> None:
    """Kill the running background instance."""
    pid = _get_running_pid()
    if pid is None:
        print("No running instance found (lock file missing or empty).")
        return
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                print(f"Stopped background instance (PID {pid}).")
            else:
                print(f"Could not stop PID {pid}: {result.stderr.strip()}")
                print("It may have already stopped.")
        else:
            os.kill(pid, signal.SIGTERM)
            print(f"Sent SIGTERM to background instance (PID {pid}).")
    except (OSError, subprocess.SubprocessError) as e:
        print(f"Could not stop PID {pid}: {e}")
        print("It may have already stopped.")


def _get_startup_folder() -> str:
    """Return the OS startup/autostart folder path."""
    if sys.platform == "win32":
        return os.path.join(
            os.environ.get("APPDATA", ""),
            "Microsoft", "Windows", "Start Menu", "Programs", "Startup"
        )
    elif sys.platform == "darwin":
        return os.path.expanduser("~/Library/LaunchAgents")
    else:
        return os.path.expanduser("~/.config/autostart")


def enable_startup() -> None:
    """Register the script to run automatically at login."""
    pyw_path = os.path.join(BASE_DIR, "bmu_wifi.pyw")
    if not os.path.exists(pyw_path):
        print(f"ERROR: {pyw_path} not found.")
        return

    if sys.platform == "win32":
        shortcut_path = os.path.join(_get_startup_folder(), "bmu_wifi.lnk")
        # Use PowerShell with base64-encoded command to avoid all quoting issues
        ps_script = (
            "$ws = New-Object -ComObject WScript.Shell\n"
            f"$sc = $ws.CreateShortcut('{shortcut_path.replace(chr(39), chr(39)+chr(39))}')\n"
            "$sc.TargetPath = 'pythonw.exe'\n"
            f"$sc.Arguments = '\"{pyw_path.replace(chr(39), chr(39)+chr(39))}\"'\n"
            "$sc.WindowStyle = 7\n"
            "$sc.Save()"
        )
        encoded = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            print(f"Startup enabled. Shortcut created at:\n  {shortcut_path}")
            print("The script will now start automatically when you log in.")
        else:
            print(f"Failed to create startup shortcut:\n  {result.stderr.strip()}")

    elif sys.platform == "darwin":
        plist_path = os.path.join(_get_startup_folder(), "bmu_wifi.plist")
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>       <string>bmu_wifi</string>
  <key>ProgramArguments</key>
  <array>
    <string>python3</string>
    <string>{pyw_path}</string>
  </array>
  <key>RunAtLoad</key>   <true/>
  <key>KeepAlive</key>   <false/>
</dict>
</plist>"""
        os.makedirs(_get_startup_folder(), exist_ok=True)
        with open(plist_path, "w") as f:
            f.write(plist)
        subprocess.run(["launchctl", "load", plist_path], capture_output=True)
        print(f"Startup enabled (launchd plist created at {plist_path}).")

    else:
        # Linux — create a .desktop autostart entry
        desktop_path = os.path.join(_get_startup_folder(), "bmu_wifi.desktop")
        os.makedirs(_get_startup_folder(), exist_ok=True)
        desktop = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=BMU Wi-Fi Auto-Login\n"
            f"Exec=python3 {pyw_path}\n"
            "Hidden=false\n"
            "NoDisplay=false\n"
            "X-GNOME-Autostart-enabled=true\n"
        )
        with open(desktop_path, "w") as f:
            f.write(desktop)
        print(f"Startup enabled (autostart entry created at {desktop_path}).")


def disable_startup() -> None:
    """Remove the script from login startup."""
    if sys.platform == "win32":
        shortcut_path = os.path.join(_get_startup_folder(), "bmu_wifi.lnk")
        if os.path.exists(shortcut_path):
            os.remove(shortcut_path)
            print(f"Startup disabled. Shortcut removed:\n  {shortcut_path}")
        else:
            print("Startup shortcut not found — already disabled or never enabled.")

    elif sys.platform == "darwin":
        plist_path = os.path.join(_get_startup_folder(), "bmu_wifi.plist")
        if os.path.exists(plist_path):
            subprocess.run(["launchctl", "unload", plist_path], capture_output=True)
            os.remove(plist_path)
            print(f"Startup disabled (plist removed: {plist_path}).")
        else:
            print("Startup entry not found — already disabled or never enabled.")

    else:
        desktop_path = os.path.join(_get_startup_folder(), "bmu_wifi.desktop")
        if os.path.exists(desktop_path):
            os.remove(desktop_path)
            print(f"Startup disabled (autostart entry removed: {desktop_path}).")
        else:
            print("Startup entry not found — already disabled or never enabled.")


# ─────────────────────────── entry point ─────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="BMU Cyberoam Wi-Fi auto-login",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--setup",            action="store_true", help="Full first-time setup wizard")
    ap.add_argument("--setup-primary",    action="store_true", help="Update primary credentials only")
    ap.add_argument("--setup-secondary",  action="store_true", help="Add or update secondary credentials")
    ap.add_argument("--clear-secondary",  action="store_true", help="Remove secondary credentials")
    ap.add_argument("--show-config",      action="store_true", help="Print current config (passwords masked)")
    ap.add_argument("--logout",           action="store_true", help="Logout from portal and exit")
    ap.add_argument("--stop",             action="store_true", help="Stop the background instance")
    ap.add_argument("--enable-startup",   action="store_true", help="Add to login startup (runs on boot)")
    ap.add_argument("--disable-startup",  action="store_true", help="Remove from login startup")
    ap.add_argument("--loop",             type=int, metavar="MINUTES",
                    help="Force re-login every N minutes")
    ap.add_argument("--interval",         type=int, default=15, metavar="SECONDS",
                    help="Connectivity check interval in seconds (default: 15)")
    ap.add_argument("--verbose",          action="store_true", help="Enable DEBUG console output")
    args = ap.parse_args()

    # Commands that don't need logging or credentials
    if args.setup:
        setup_full(); return
    if args.setup_primary:
        setup_primary(); return
    if args.setup_secondary:
        setup_secondary(); return
    if args.clear_secondary:
        clear_secondary(); return
    if args.show_config:
        show_config(); return
    if args.stop:
        stop_background(); return
    if args.enable_startup:
        enable_startup(); return
    if args.disable_startup:
        disable_startup(); return

    global logger
    logger = setup_logging(LOG_FILE, verbose=args.verbose)

    cfg = load_credentials_or_setup()

    if args.logout:
        do_logout_command(cfg)
    elif args.loop:
        run_loop(cfg, args.loop)
    else:
        run_auto(cfg, args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
