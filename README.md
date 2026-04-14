# BMU Wi-Fi Auto-Login

Automatically logs into the BMU Cyberoam captive portal and keeps you connected. If the session drops or times out, it re-logs in silently in the background.

## Requirements

```
pip install requests
```

## First-time setup

```
python bmu_wifi.py --setup
```

You'll be prompted for:
- Portal login URL (press Enter to keep the default BMU URL)
- Primary username & password
- Optional secondary/backup credentials

Credentials are saved locally in `credentials.ini` (gitignored — never uploaded anywhere).

## Usage

| Command | What it does |
|---|---|
| `python bmu_wifi.py` | Auto-reconnect (checks every 15 sec) |
| `python bmu_wifi.py --interval 30` | Auto-reconnect, check every 30 sec |
| `python bmu_wifi.py --loop 5` | Force re-login every 5 minutes |
| `python bmu_wifi.py --logout` | Logout from portal and exit |
| `python bmu_wifi.py --verbose` | Show DEBUG-level output |

### Stop / uninstall

| Command | What it does |
|---|---|
| `python bmu_wifi.py --stop` | Kill the running background instance |
| `python bmu_wifi.py --logout` | Logout from the portal |
| `python bmu_wifi.py --disable-startup` | Remove from login startup |

To fully stop using it: run `--stop`, then `--logout`, then `--disable-startup`.

### Startup

| Command | What it does |
|---|---|
| `python bmu_wifi.py --enable-startup` | Add to login startup (auto-runs on boot) |
| `python bmu_wifi.py --disable-startup` | Remove from login startup |

Works on Windows (Startup folder shortcut), macOS (launchd), and Linux (XDG autostart).

### Managing credentials

| Command | What it does |
|---|---|
| `python bmu_wifi.py --setup` | Full setup wizard (URL + all creds) |
| `python bmu_wifi.py --setup-primary` | Update primary credentials only |
| `python bmu_wifi.py --setup-secondary` | Add or update backup credentials |
| `python bmu_wifi.py --clear-secondary` | Remove backup credentials |
| `python bmu_wifi.py --show-config` | Print current config (passwords masked) |

## Secondary / backup credentials

If your primary login fails (wrong password, expired account), the script automatically tries your secondary credentials. This is useful if you have a guest account as backup.

Only credential failures trigger the fallback — network errors and "max login limit reached" do not, since those aren't credentials problems.

## Run silently on startup (Windows / macOS / Linux)

1. **Run setup first** (in a normal console window): `python bmu_wifi.py --setup`
2. Register it: `python bmu_wifi.py --enable-startup`
3. Done — it starts automatically on every login with no console window

To undo: `python bmu_wifi.py --disable-startup`

## How it works

- Detects **three network states**: real internet, captive portal, or Wi-Fi not connected
- Uses `connectivitycheck.gstatic.com/generate_204` — HTTP 204 means real internet; a redirect or HTML page means captive portal
- Parses the Cyberoam XML login response to confirm success (not fire-and-forget)
- **Exponential backoff** on repeated failures: 5s → 10s → 20s → ... → 5 min cap
- **Single-instance lock** — a second copy refuses to start if one is already running
- **Rotating log file** (`bmu_wifi.log`, max 4 MB) written next to the script — useful when running silently via `pythonw.exe`
- Credentials stay only on your machine in `credentials.ini`
