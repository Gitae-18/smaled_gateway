import os
import time
import subprocess

from store import NodeStore

NODES_STORE_PATH = "/home/pi/config/nodes_store.bin"
BACKUP_FLAG_FILE = "/tmp/backup_done"

def log(msg : str):
    print(f"[BACKUP] {msg}", flush=True)

def main():
    log("Emergency backup start")

    try:
        ns = NodeStore(path = NODES_STORE_PATH, cap = 20)
        ns.flush_to_disk()
        log("NodeStore flush_to_disk() done.")
    except Exception as e:
        log(f"NodeStore flush error: {e!r}")
        
    try:
        log("Calling sync()...")
        subprocess.run(["sync"])
        log("sync() done.")
    except Exception as e:
        log(f"sync() failed: {e!r}")

    try:
        with open(BACKUP_FLAG_FILE, "w") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
        log(f"Created BACKUP_FLAG_FILE: {BACKUP_FLAG_FILE}")        
    except Exception as e:
        log(f"Create BACKUP_FILE failed: {e!r}")
    
if __name__ == "__main__":
    main()