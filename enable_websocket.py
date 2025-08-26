# file: enable_websocket.py
"""
Script to re-enable WebSocket functionality
"""

import subprocess
import sys

if __name__ == "__main__":
    subprocess.run([sys.executable, "disable_websocket.py", "enable"])