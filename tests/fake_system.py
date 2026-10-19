#!/usr/bin/python3
"""Fake systemctl, nginx, dpkg, dpkg-query, apt-get and ss for the App Store's uninstall tests.

Installed under those names in a folder put first in PATH. They share a JSON state (fake-state.json, next to
the fake root $STORE_TEST_ROOT) and note every call in fake-calls.log, so that the tests can check what was run.
State: {"units": {name: {"active", "enabled", "user", "file" (path of its unit file under the fake root),
"package"}}, "packages": {name: status}, "rdeps": {package: [packages removed along]}, "apt_check": code,
"audit": text, "nginx_ok": bool, "apt_daily": state, "apt_daily_on_stop": the apt_daily that the next systemctl stop
sets, "active_in_phase": {unit: the uninstaller's step during which systemctl is-active says active},
"sim_extra": [lines added to apt-get -s], "listening": [ports], "listening_pids": [[port, pid]],
"listening_cgroups": [[port, cgroup]]}.
"""
import fcntl
import fnmatch
import json
import os
import sys

ROOT = os.environ["STORE_TEST_ROOT"]  # the only variable that reaches Swizzin's scripts too
STATE = os.path.join(os.path.dirname(ROOT.rstrip("/")), "fake-state.json")
LOG = os.path.join(os.path.dirname(ROOT.rstrip("/")), "fake-calls.log")


def load():
    with open(STATE) as f:
        return json.load(f)


def save(state):
    with open(STATE + ".tmp", "w") as f:
        json.dump(state, f)
    os.replace(STATE + ".tmp", STATE)


def installed(status):
    return len(status) >= 2 and status[1] in "iUFWt"


def loaded(state, name):
    unit = state["units"].get(name)
    if "@" in name and not (unit or {}).get("file"):  # an instance exists as long as its template file does
        template = name.split("@", 1)[0] + "@.service"
        return os.path.exists(os.path.join(ROOT, "etc/systemd/system", template))
    if unit is None:
        return False
    if unit.get("package") and not installed(state["packages"].get(unit["package"], "")):
        return False
    return not unit.get("file") or os.path.exists(os.path.join(ROOT, unit["file"].lstrip("/")))


def runner_phase():
    """The uninstaller's step, from the state file that test_remove_runner gives it (next to the fake root)."""
    try:
        with open(os.path.join(os.path.dirname(ROOT.rstrip("/")), "state.json")) as f:
            return json.load(f).get("phase")
    except (OSError, ValueError):
        return None


def systemctl(state, args):
    args = [a for a in args if a not in ("-q", "--quiet", "--no-pager")]
    cmd = args[0]
    if cmd == "show":
        prop, name = args[args.index("-p") + 1], args[-1]
        unit = state["units"].get(name, {})
        if prop == "LoadState":
            print("loaded" if loaded(state, name) else "not-found")
        elif prop == "User":
            print(unit.get("user", ""))
        else:
            print("")
        return 0
    if cmd == "is-active":
        name = args[1]
        if name in ("apt-daily.service", "apt-daily-upgrade.service"):
            value = state.get("apt_daily", "inactive")
        else:
            value = state["units"].get(name, {}).get("active", "inactive") if loaded(state, name) else "inactive"
        phase = state.get("active_in_phase", {}).get(name)
        if phase and phase == runner_phase():  # someone started it meanwhile (the panel's switch)
            value = "active"
        print(value)
        return 0 if value == "active" else 3
    if cmd == "is-enabled":
        value = state["units"].get(args[1], {}).get("enabled", "disabled") if loaded(state, args[1]) else "not-found"
        print(value)
        return 0 if value == "enabled" else 1
    if cmd == "list-units":
        pattern = args[-1]
        for name in sorted(state["units"]):
            if fnmatch.fnmatchcase(name, pattern) and loaded(state, name):
                print("%s loaded %s running fake" % (name, state["units"][name].get("active", "inactive")))
        return 0
    if cmd in ("stop", "start", "enable", "disable"):
        if cmd == "stop" and "apt_daily_on_stop" in state:  # the automatic updates start meanwhile
            state["apt_daily"] = state.pop("apt_daily_on_stop")
        for name in args[1:]:
            unit = state["units"].setdefault(name, {})
            if cmd == "stop":
                unit["active"] = "inactive"
            elif cmd == "start" and loaded(state, name):
                unit["active"] = "active"
            elif cmd == "enable":
                unit["enabled"] = "enabled"
            elif cmd == "disable":
                unit["enabled"] = "disabled"
        save(state)
        return 0
    if cmd in ("daemon-reload", "reset-failed", "kill", "reload", "restart", "cat"):
        return 0
    return 1


def apt_get(state, args):
    if args[0] == "check":
        return state.get("apt_check", 0)
    if args[0] == "update":
        return 0
    simulate = "-s" in args
    names = [a for a in args if not a.startswith("-") and a not in ("remove", "purge", "install", "-o")
             and "=" not in a and "::" not in a]
    removed, todo = [], list(names)
    while todo:
        name = todo.pop(0)
        if name in removed:
            continue
        if not any(fnmatch.fnmatchcase(p, name) for p in state["packages"]):
            if simulate and name in names:
                print("E: Unable to locate package " + name)
                return 100
            continue
        for p in sorted(state["packages"]):
            if fnmatch.fnmatchcase(p, name) and installed(state["packages"][p]) and p not in removed:
                removed.append(p)
                todo.extend(state.get("rdeps", {}).get(p, []))
    if simulate:
        for p in removed:
            print("Purg %s [1.0]" % p)
        for line in state.get("sim_extra", []):
            print(line)
        return 0
    for p in removed:
        state["packages"][p] = "un"
    save(state)
    return 0


def dpkg(state, args):
    if args[0] == "--audit":
        print(state.get("audit", ""), end="")
        return 0
    if args[0] in ("--purge", "-r", "--remove", "-P"):
        for name in args[1:]:
            if name in state["packages"]:
                state["packages"][name] = "un" if args[0] in ("--purge", "-P") else "rc"
        save(state)
        return 0
    return 1


def dpkg_query(state, args):
    for name, status in sorted(state["packages"].items()):
        print("%s\t%s" % (name, status))
    return 0


def nginx(state, args):
    if "-t" in args:
        return 0 if state.get("nginx_ok", True) else 1
    if "-T" in args:
        for folder in ("etc/nginx/apps", "etc/nginx/conf.d"):
            for name in sorted(os.listdir(os.path.join(ROOT, folder))) if os.path.isdir(os.path.join(ROOT, folder)) else []:
                print("# configuration file /%s/%s:" % (folder, name))
        return 0
    return 1


def ss(state, args):
    for port in state.get("listening", []):
        print("LISTEN 0 4096 127.0.0.1:%s 0.0.0.0:*" % port)
    for port, pid in state.get("listening_pids", []):  # with the process, as ss -p shows it
        print('LISTEN 0 4096 *:%s *:* users:(("app",pid=%d,fd=3))' % (port, pid))
    for port, cgroup in state.get("listening_cgroups", []):  # with the socket's cgroup, as ss -e shows it
        print("LISTEN 0 4096 127.0.0.1:%s 0.0.0.0:* uid:1001 ino:1 sk:1 cgroup:%s <->" % (port, cgroup))
    return 0


def main():
    tool, args = os.path.basename(sys.argv[0]), sys.argv[1:]
    with open(STATE + ".lock", "a") as lock:  # one fake call at a time, like the real tools would serialize
        fcntl.flock(lock, fcntl.LOCK_EX)
        with open(LOG, "a") as f:
            f.write(" ".join([tool] + args) + "\n")
        state = load()
        handler = {"systemctl": systemctl, "apt-get": apt_get, "dpkg": dpkg, "dpkg-query": dpkg_query,
                   "nginx": nginx, "ss": ss}[tool]
        return handler(state, args or [""])


if __name__ == "__main__":
    sys.exit(main())
