# Silent background version — no console window (run with pythonw.exe)
# Double-click this or add it to Windows Startup folder.
# It will prompt for credentials on first run, then stay silent.

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bmu_wifi import load_credentials, is_connected, do_login, run_auto

username, password = load_credentials()
run_auto(username, password, interval=15)
