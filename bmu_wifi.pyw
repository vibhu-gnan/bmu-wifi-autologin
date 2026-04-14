# Silent background version — no console window (run with pythonw.exe)
# Double-click this or add it to Windows Startup folder.
# Run --setup in a console window first if you haven't already.
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bmu_wifi import main
main()
