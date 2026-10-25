# fake_esp.py - Pretends to be the ESP32: posts fake telemetry to the backend
# and prints the command it gets back. Useful for testing without hardware.
#
# Examples (from the backend folder):
#   ..\venv\Scripts\python.exe tools\fake_esp.py --pir 1          one cycle with motion
#   ..\venv\Scripts\python.exe tools\fake_esp.py --pir 0 --loop   every 2 s, no motion (Ctrl+C to stop)

import argparse
import json
import time
import urllib.request
from datetime import datetime

# FAKE power, same idea as the real firmware: watts of whatever is switched on
LIGHT_W = 40
FAN_W = 60


def post_telemetry(url, data):
    req = urllib.request.Request(url + "/telemetry", data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def get_command(url):
    with urllib.request.urlopen(url + "/command", timeout=5) as resp:
        return json.loads(resp.read())


def main():
    parser = argparse.ArgumentParser(description="Fake ESP32 for testing the backend")
    parser.add_argument("--pir", type=int, choices=[0, 1], default=1, help="1 = motion, 0 = none")
    parser.add_argument("--loop", action="store_true", help="repeat every --interval seconds")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--url", default="http://127.0.0.1:5000", help="backend address")
    args = parser.parse_args()

    light, fan = False, False   # what the "relays" are doing right now
    while True:
        power = (LIGHT_W if light else 0) + (FAN_W if fan else 0)
        reading = {"pir": args.pir, "ldr": 1500, "temp": 29.0, "hum": 60.0, "power_w": power}
        try:
            post_telemetry(args.url, reading)
            cmd = get_command(args.url)
            light, fan = cmd["light"], cmd["fan"]
            print("%s  pir=%d power=%dW  ->  light=%s fan=%s  (%s)" % (
                datetime.now().strftime("%H:%M:%S"), args.pir, power,
                "ON" if light else "OFF", "ON" if fan else "OFF", cmd["reason"]), flush=True)
        except OSError as e:
            print("%s  backend not reachable: %s" % (datetime.now().strftime("%H:%M:%S"), e), flush=True)

        if not args.loop:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("stopped")
