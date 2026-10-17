"""One uninstallation through the real panel, as the page does it: Basic login, the page's token, the /store
Socket.IO namespace, the confirmation's ticket, the app's name and the admin password (see remove.py).

Run inside the container with the panel's own Python, which has python-socketio and requests:
  /opt/.venv/swizzin/bin/python panel_remove.py <app> <user:password> <transcript>

Before the real removal it checks the refusals: no token, a wrong password, a wrong name. Prints one JSON line.
"""
import base64
import json
import queue
import sys
import time

import requests
import socketio
import urllib3

urllib3.disable_warnings()
URL = "https://127.0.0.1"


def client(login, token):
    """A connection to /store, and the queue of what the helper sends through the panel."""
    user, password = login.split(":", 1)
    session = requests.Session()
    session.verify = False
    session.auth = (user, password)
    received = queue.Queue()
    sio = socketio.Client(http_session=session, ssl_verify=False, reconnection=False)
    sio.on("helper", lambda msg: received.put(msg), namespace="/store")
    header = "Basic " + base64.b64encode(login.encode()).decode()
    sio.connect(URL, headers={"Authorization": header}, auth={"token": token} if token else None,
                transports=["polling"], namespaces=["/store"], wait_timeout=20)
    return sio, received


def answer(received, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            msg = received.get(timeout=1)
        except queue.Empty:
            continue
        if "ok" in msg:
            return msg
    return None


def main():
    app, login, transcript = sys.argv[1:4]
    password = login.split(":", 1)[1]
    out = {"app": app}
    token = requests.get(URL + "/store/token", auth=tuple(login.split(":", 1)), verify=False, timeout=20).json()["token"]
    try:  # without the page's token, the namespace refuses the connection
        sio, _ = client(login, None)
        sio.disconnect()
        out["without_token"] = "connected"
    except socketio.exceptions.ConnectionError as e:
        out["without_token"] = "refused: %s" % e
    sio, received = client(login, token)
    try:
        sio.emit("remove-preview", {"app": app}, namespace="/store")
        preview = answer(received)
        out["preview"] = preview and {k: preview.get(k) for k in ("ok", "preview", "error")}
        ticket = (preview or {}).get("ticket")
        sio.emit("remove", {"app": app, "ticket": ticket, "confirm": app.upper(), "password": password + "-faux"},
                 namespace="/store")
        out["wrong_password"] = (answer(received) or {}).get("error")
        sio.emit("remove", {"app": app, "ticket": ticket, "confirm": app + "x", "password": password}, namespace="/store")
        out["wrong_name"] = (answer(received) or {}).get("error")
        sio.emit("remove", {"app": app, "ticket": ticket, "confirm": " %s " % app.upper(), "password": password,
                            "rows": 40, "cols": 120}, namespace="/store")
        started = answer(received)
        out["started"] = started and {k: started.get(k) for k in ("ok", "error")}
        phases, done = [], None
        end = time.monotonic() + 3600
        with open(transcript, "w", encoding="utf-8") as log:
            while started and started.get("ok") and done is None and time.monotonic() < end:
                try:
                    msg = received.get(timeout=1)
                except queue.Empty:
                    continue
                if "output" in msg:
                    log.write(msg["output"])
                elif "phase" in msg:
                    phases.append(msg["phase"])
                elif "done" in msg:
                    done = msg["done"]
        out.update(phases=phases, done=done)
    finally:
        sio.disconnect()
    print(json.dumps(out))


if __name__ == "__main__":
    main()
