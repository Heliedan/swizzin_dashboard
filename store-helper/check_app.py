#!/usr/bin/python3
"""The App Store's own check of a freshly installed app, for the apps whose Swizzin check (box test) is wrong.

Swizzin's check fails for these although the app works: it asks the app's port at the wrong address, or through
nginx without the password, or looks for a service under the wrong name. Its red lines would only scare the
user, so the helper runs this instead, in the same terminal: the app's services (found by systemd) must be
active and listening on a TCP port, within WAIT seconds (some apps take a while to start).

  python3 -I check_app.py <app> [--wait SECONDS]      exit code 0: checks passed, 1: failed

Standard library only, run as root by swizzin_store.py.
"""
import argparse
import os
import re
import subprocess
import sys
import time

WAIT = 120  # seconds an app may take to start (Emby's first start is the slowest)

# app -> its systemd service: "name" for name.service, "name@" for the instances name@<user>.service.
# Organizr has no service of its own: PHP (php*-fpm) runs it behind nginx.
SERVICES = {
    "bazarr": "bazarr",
    "btsync": "resilio-sync",
    "emby": "emby-server",
    "filebrowser": "filebrowser",
    "flood": "flood@",
    "jackett": "jackett@",
    "lidarr": "lidarr",
    "medusa": "medusa",
    "organizr": None,
    "prowlarr": "prowlarr",
    "quassel": "quasselcore",
    "qui": "qui@",
    "radarr": "radarr",
    "sickgear": "sickgear",
    "sonarr": "sonarr",
    "syncthing": "syncthing@",
}


def say(text):
    sys.stdout.write("[App Store] " + text + "\r\n")
    sys.stdout.flush()


def command(*args):
    try:
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return p.returncode, p.stdout.decode("utf-8", "replace")


def units(patterns):
    """The services systemd knows under these names (instances included)."""
    found = []
    for pattern in patterns:
        out = command("systemctl", "list-units", "--all", "--plain", "--no-legend", "--type=service", pattern)[1]
        for line in out.splitlines():
            fields = line.split()
            if fields and fields[0].endswith(".service") and fields[0] not in found:
                found.append(fields[0])
    return found


def active(unit):
    return command("systemctl", "is-active", unit)[1].strip() or "unknown"


def cgroup_has(pid, unit):
    try:
        with open("/proc/%s/cgroup" % pid) as f:
            return "/" + unit in f.read()
    except OSError:
        return False


def ports(unit):
    """The TCP ports a process of this service listens on."""
    out = command("ss", "-ltnpH")[1]
    found = set()
    for line in out.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        if any(cgroup_has(pid, unit) for pid in re.findall(r"pid=(\d+)", line)):
            found.add(fields[3].rsplit(":", 1)[-1])
    return sorted(found, key=lambda p: (len(p), p))


def check_services(app, base, wait):
    patterns = [base[:-1] + "@*.service"] if base.endswith("@") else [base + ".service"]
    deadline = time.monotonic() + wait
    waited = False
    while True:
        found = units(patterns)
        states = {u: active(u) for u in found}
        listening = {u: ports(u) for u in found if states[u] == "active"}
        if found and all(states[u] == "active" and listening.get(u) for u in found):
            break
        if time.monotonic() >= deadline:
            if not found:
                say("✗ No service of %s was found (%s)" % (app, ", ".join(patterns)))
                return False
            for u in found:
                if states[u] != "active":
                    say("✗ Its service does not run: %s (%s)" % (u, states[u]))
                elif not listening.get(u):
                    say("✗ Its service runs but listens on no port: %s" % u)
            return False
        if not waited:
            say("Waiting for %s to start (%d seconds at most)..." % (app, wait))
            waited = True
        time.sleep(2)
    for u in found:
        say("✓ Its service runs: " + u)
        say("✓ It is listening on port " + ", ".join(listening[u]))
    return True


def check_organizr(nginx_apps):
    ok = True
    conf = os.path.join(nginx_apps, "organizr.conf")
    if os.path.isfile(conf):
        say("✓ Its page is set up in the web server (%s)" % conf)
    else:
        say("✗ Its page is not set up in the web server (%s is missing)" % conf)
        ok = False
    if command("nginx", "-t")[0] == 0:
        say("✓ The web server accepts its configuration")
    else:
        say("✗ The web server refuses its configuration (nginx -t)")
        ok = False
    php = [u for u in units(["php*-fpm.service"]) if active(u) == "active"]
    if php:
        say("✓ PHP runs: " + ", ".join(php))
    else:
        say("✗ PHP (php-fpm), which runs Organizr, does not run")
        ok = False
    return ok


def main(argv=None):
    p = argparse.ArgumentParser(description="The App Store's check of an installed app (root).")
    p.add_argument("app", choices=sorted(SERVICES))
    p.add_argument("--wait", type=int, default=WAIT, help="seconds the app may take to start")
    p.add_argument("--nginx-apps", default="/etc/nginx/apps", help="nginx's folder of app pages")
    opts = p.parse_args(argv)
    say("Swizzin's own check does not work for %s: the App Store checks it instead." % opts.app)
    if SERVICES[opts.app] is None:
        ok = check_organizr(opts.nginx_apps)
    else:
        ok = check_services(opts.app, SERVICES[opts.app], opts.wait)
    say("%s: checks passed." % opts.app if ok else "%s does not seem to run: see above." % opts.app)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
