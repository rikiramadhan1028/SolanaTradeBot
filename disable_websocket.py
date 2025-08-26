# file: disable_websocket.py
"""
Temporary script to disable WebSocket functionality if needed
This will make the client use only HTTP RPC like before
"""

import os
import shutil

def disable_websocket():
    """Temporarily disable WebSocket by renaming the file"""
    ws_file = "blockchain_clients/websocket_manager.py"
    backup_file = "blockchain_clients/websocket_manager.py.backup"
    
    if os.path.exists(ws_file):
        shutil.move(ws_file, backup_file)
        print(f"✅ WebSocket disabled. File moved to: {backup_file}")
        print("The client will now use HTTP-only mode (like before WebSocket integration)")
        print("To re-enable WebSocket, run: python enable_websocket.py")
    else:
        print("❌ WebSocket manager file not found")

def enable_websocket():
    """Re-enable WebSocket by restoring the file"""
    ws_file = "blockchain_clients/websocket_manager.py"
    backup_file = "blockchain_clients/websocket_manager.py.backup"
    
    if os.path.exists(backup_file):
        shutil.move(backup_file, ws_file)
        print(f"✅ WebSocket re-enabled. File restored: {ws_file}")
        print("The client will now use hybrid HTTP+WebSocket mode")
    else:
        print("❌ WebSocket backup file not found")

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "enable":
        enable_websocket()
    else:
        disable_websocket()