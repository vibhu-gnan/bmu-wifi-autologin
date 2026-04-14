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

This saves your credentials locally in `credentials.ini` (gitignored — never uploaded anywhere).

## Usage

| Command | What it does |
|---|---|
| `python bmu_wifi.py` | Login + auto-reconnect every 15 sec |
| `python bmu_wifi.py --interval 30` | Same, but check every 30 sec |
| `python bmu_wifi.py --loop 5` | Force re-login every 5 minutes |
| `python bmu_wifi.py --logout` | Logout from the portal |
| `python bmu_wifi.py --setup` | Change saved credentials |

## Run silently on startup (Windows)

1. Press `Win + R`, type `shell:startup`, hit Enter
2. Create a shortcut to `bmu_wifi.pyw` in that folder
3. Done — it will start automatically on every login, no console window

## How it works

- Checks real internet connectivity via `connectivitycheck.gstatic.com/generate_204` (HTTP 204 = real internet, anything else = captive portal or no connection)
- Posts login form to `https://bmunet.bmu.edu.in:8090/login.xml`
- Your credentials stay only on your machine in `credentials.ini`
