import time
import threading

last_hearbeat_time = time.time()
HEARTBEAT_TIMEOUT = 10
HEARTBEAT_INTERVAL = 3

def update_heartbeat():
    global last_hearbeat_time
    last_hearbeat_time = time.time()

def check_hearbeat(interval_s: int, send_func) :
    
    return 