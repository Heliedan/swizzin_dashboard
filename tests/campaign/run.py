#!/usr/bin/env python3
"""App Store compatibility campaign: every app the store offers is installed through the helper,
exactly as from the page, each in a fresh throwaway Swizzin container, then checked with `box test`.

Needs Docker 28 or later (writable cgroups without --privileged) and an image of a fresh Swizzin
(nginx + panel) with this repository's App Store installed: see tests/campaign/README.md.

  python3 tests/campaign/run.py --image swz-camp:base --out results/ [app ...]

One line per app is printed and appended to results.jsonl, with the transcript and Swizzin's own
log of each installation; results.md is written at the end. An app already in results.jsonl is
skipped, so a stopped campaign carries on where it was. Apps that need another one first (rutorrent
needs rtorrent...) are installed in a copy of the container where that one was installed.
"""
import argparse
import ast
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
RUN = ["run", "-d", "--cgroupns=private", "--security-opt", "writable-cgroups=true",
       "--tmpfs", "/run", "--tmpfs", "/run/lock", "--stop-signal", "SIGRTMIN+3",
       "--memory", "4g", "--cpus", "2", "--pids-limit", "4096",
       # box test's generic check runs lsof, which must see the sockets of the apps' own users
       "--cap-add", "SYS_PTRACE"]


# Answers that differ from the installer's default (client.py: [[pattern matched on the question, answer]]).
ANSWERS = {
    # Calibre's own installer fails on Ubuntu 24.04 (swizzin/swizzin#1212); Calibre-Web installs without it
    "calibreweb": [["Install Calibre through swizzin now", "n"]],
    # ZNC's configuration wizard: no default for the port and the user name; no IRC network set up
    "znc": [["Listen on port", "6697"], ["Username", "test"], ["Set up a network", "no"]],
}

# What helps to understand a failure without running the installation again.
DIAGNOSTICS = r"""echo "== box test log"; L=$(ls -t /root/logs/tests-*.txt 2>/dev/null | head -1); [ -n "$L" ] && tail -n 40 "$L"
echo "== failed units"; systemctl --failed --no-legend --plain
echo "== listening"; ss -ltnpH
echo "== nginx error.log"; tail -n 20 /var/log/nginx/error.log 2>/dev/null
echo "== journal of the app's services"; journalctl --no-pager -o short-monotonic -n 40 -u "$1*" -u "$1@*" 2>/dev/null
if [ -n "$2" ]; then echo "== start of the page $2"; curl -sk -L --max-redirs 5 -m 20 -u "$3" "https://127.0.0.1$2" | head -c 800; echo; fi"""


def requires():
    """REQUIRES from core/custom/store/__init__.py: what an installer expects but does not install."""
    with open(os.path.join(ROOT, "core", "custom", "store", "__init__.py")) as f:
        tree = ast.parse(f.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "REQUIRES" for t in node.targets):
            return ast.literal_eval(node.value)
    return {}


class Campaign:
    def __init__(self, opts):
        self.opts = opts
        self.docker_cmd = shlex.split(opts.docker)
        os.makedirs(opts.out, exist_ok=True)
        self.results_path = os.path.join(opts.out, "results.jsonl")
        self.results = {}
        if os.path.exists(self.results_path):
            with open(self.results_path) as f:
                for line in f:
                    r = json.loads(line)
                    self.results[r["app"]] = r
        self.images = {}  # prerequisite -> image of a container where it was installed
        for app, r in self.results.items():  # a campaign carried on: images kept from the previous run
            image = "swz-camp:with-" + app
            if r.get("result") == "installed" and self.docker("image", "inspect", image, check=False).returncode == 0:
                self.images[app] = image

    def docker(self, *args, check=True, timeout=600, stdin=None):
        return subprocess.run(self.docker_cmd + list(args), capture_output=True, text=True, check=check,
                              timeout=timeout, stdin=stdin)

    def start(self, name, image):
        self.docker("rm", "-f", name, check=False)
        self.docker(*RUN, "--name", name, "--hostname", name, image)
        end = time.time() + 240
        while time.time() < end:
            r = self.docker("exec", name, "bash", "-c",
                            "systemctl is-system-running; test -S /run/swizzin-store/store.sock && echo socket-ready",
                            check=False, timeout=30)
            if "socket-ready" in r.stdout and re.search(r"^(running|degraded)$", r.stdout, re.M):
                self.docker("cp", os.path.join(HERE, "client.py"), name + ":/root/campaign-client.py")
                return True
            time.sleep(2)
        return False

    def catalogue(self):
        name = "swz-camp-catalogue"
        try:
            if not self.start(name, self.opts.image):
                sys.exit("the base container did not start (systemd or the App Store helper)")
            r = self.docker("exec", name, "python3", "-I", "/root/campaign-client.py", "--catalogue")
            return json.loads(r.stdout)
        finally:
            self.docker("rm", "-f", name, check=False)

    def free_gb(self):
        return shutil.disk_usage(self.opts.disk).free / 1e9

    def web_check(self, name, app):
        """HTTP status of the app's nginx location, if it has one (followed redirects, panel login).
        The location named after the app if there is one, else the first one outside /.well-known."""
        script = r"""f=/etc/nginx/apps/%s.conf; [ -f "$f" ] || exit 0
locs=$(grep -oP '^\s*location\s+(=\s+|\^~\s+)?\K/[^\s{]+' "$f")
loc=$(echo "$locs" | grep -m1 -i "^/$(basename "$f" .conf)" || echo "$locs" | grep -m1 -v '^/\.well-known'); [ -n "$loc" ] || exit 0
code=$(curl -sk -o /dev/null -L --max-redirs 5 -m 20 -w '%%{http_code}' -u %s "https://127.0.0.1$loc")
echo "$loc $code" """ % (app, shlex.quote(self.opts.login))
        r = self.docker("exec", name, "bash", "-c", script, check=False, timeout=60)
        return r.stdout.strip() or None

    def run_app(self, app, image):
        name = "swz-camp-" + app
        record = {"app": app, "image": image, "started_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        try:
            if not self.start(name, image):
                record.update(result="container-not-ready")
                return record
            r = self.docker("exec", name, "python3", "-I", "/root/campaign-client.py", app,
                            "/root/campaign-transcript.txt", json.dumps(ANSWERS.get(app, [])),
                            check=False, timeout=110 * 60)
            try:
                record.update(json.loads(r.stdout.strip().splitlines()[-1]))
            except (ValueError, IndexError):
                record.update(result="client-error", client_stderr=r.stderr[-2000:])
            if record.get("result") == "installed" and record.get("test") == "failed":
                # some apps take a while to open their port after their first start: same checks a minute later
                time.sleep(60)
                again = self.docker("exec", "-e", "TERM=xterm", name, "/usr/local/bin/swizzin/box", "test", app,
                                    check=False, timeout=300)
                record["retest"] = "passed" if again.returncode == 0 else "failed"
            record["web"] = self.web_check(name, app)
            location = record["web"].split()[0] if record["web"] else ""
            diag = self.docker("exec", name, "bash", "-c", DIAGNOSTICS, "diagnostics", app, location, self.opts.login,
                               check=False, timeout=90).stdout
            with open(os.path.join(self.opts.out, app + ".diag.txt"), "w") as f:
                f.write(diag)
            locks = self.docker("exec", name, "bash", "-c", "ls -A /install", check=False).stdout.split()
            record["locks"] = sorted(lock[1:-5] for lock in locks if lock.endswith(".lock"))
            for src, dest in (("/root/campaign-transcript.txt", app + ".transcript.txt"),
                              ("/root/logs/swizzin.log", app + ".swizzin.log")):
                self.docker("cp", name + ":" + src, os.path.join(self.opts.out, dest), check=False)
            log = os.path.join(self.opts.out, app + ".swizzin.log")
            if os.path.exists(log):
                with open(log, errors="replace") as f:
                    record["github_rate_limited"] = "API rate limit exceeded" in f.read()
            if record.get("result") == "installed" and app in self.opts.prerequisites:
                self.docker("stop", "-t", "90", name, timeout=200)
                self.docker("commit", name, "swz-camp:with-" + app, timeout=900)
                self.images[app] = "swz-camp:with-" + app
        except subprocess.TimeoutExpired as e:
            record.update(result=record.get("result") or "campaign-timeout", errors=[str(e)])
        finally:
            self.docker("rm", "-f", "-v", name, check=False)
        return record

    def save(self, record):
        record["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.results[record["app"]] = record
        with open(self.results_path, "a") as f:
            f.write(json.dumps(record) + "\n")
        checks = (record.get("test") or "-") + ("/" + record["retest"] if record.get("retest") else "")
        print("{}  {:<12} {:<20} checks {:<7} {:>5} s  questions {:<2} web {}".format(
            time.strftime("%H:%M:%S"), record["app"], str(record.get("result")), checks, record.get("seconds", 0),
            len(record.get("prompts", [])), record.get("web") or "-"), flush=True)

    def main(self):
        catalogue = self.catalogue()
        offered = [a["name"] for a in catalogue["apps"] if not a.get("blocked")]
        base = {a["name"] for a in catalogue["apps"] if a.get("installed")}
        print("{} apps offered, already in the base image: {}".format(len(offered), ", ".join(sorted(base))), flush=True)
        needs = {app: [r for r in reqs if r not in base] for app, reqs in requires().items()}
        self.opts.prerequisites = {r for reqs in needs.values() for r in reqs}
        apps = self.opts.apps or offered
        unknown = set(apps) - set(offered)
        if unknown:
            sys.exit("not offered by the store: " + ", ".join(sorted(unknown)))
        # prerequisites first, so that the apps that need them can start from their container
        apps = sorted(apps, key=lambda a: (a not in self.opts.prerequisites, a))
        for app in apps:
            if app in self.results and not self.opts.again:
                continue
            if self.free_gb() < self.opts.min_free_gb:
                print("stopped: less than {} GB free on {}".format(self.opts.min_free_gb, self.opts.disk), flush=True)
                break
            missing = needs.get(app, [])
            if len(missing) > 1:
                self.save({"app": app, "result": "skipped", "errors": ["needs " + ", ".join(missing)]})
                continue
            image = self.opts.image
            if missing:
                image = self.images.get(missing[0])
                if image is None:
                    self.save({"app": app, "result": "skipped", "errors": [missing[0] + " not installed first"]})
                    continue
            self.save(self.run_app(app, image))
        if not self.opts.keep_images:
            for image in self.images.values():
                self.docker("rmi", image, check=False)
        self.report()

    def report(self):
        lines = ["| App | Result | Checks | Install time | Questions (answer) | Web |", "|---|---|---|---|---|---|"]
        for app in sorted(self.results):
            r = self.results[app]
            questions = "; ".join("{} ({})".format(re.sub(r"\s+", " ", p["question"])[-60:].strip(), p["answer"])
                                  for p in r.get("prompts", [])) or "-"
            minutes = "{:.0f} min".format(r["seconds"] / 60) if r.get("seconds") else "-"
            checks = (r.get("test") or "-") + (", a minute later: " + r["retest"] if r.get("retest") else "") + \
                (" (generic)" if r.get("generic_test") else "")
            lines.append("| {} | {} | {} | {} | {} | {} |".format(app, r.get("result"), checks, minutes,
                                                                   questions.replace("|", "/"), r.get("web") or "-"))
        with open(os.path.join(self.opts.out, "results.md"), "w") as f:
            f.write("\n".join(lines) + "\n")


def main():
    p = argparse.ArgumentParser(description="App Store compatibility campaign (throwaway Swizzin containers).")
    p.add_argument("apps", nargs="*", help="only these apps (default: every app the store offers)")
    p.add_argument("--image", required=True, help="Docker image of a fresh Swizzin with the App Store")
    p.add_argument("--out", required=True, help="results folder")
    p.add_argument("--docker", default="docker", help='the docker command, for example "sudo -n docker"')
    p.add_argument("--login", default="test:Essai-Swz-2026", help="the base image's Swizzin user:password")
    p.add_argument("--disk", default="/", help="the file system Docker writes to")
    p.add_argument("--min-free-gb", type=float, default=20, help="stop below this much free space")
    p.add_argument("--again", action="store_true", help="also run the apps already in results.jsonl")
    p.add_argument("--keep-images", action="store_true", help="keep the images made for prerequisites")
    Campaign(p.parse_args()).main()


if __name__ == "__main__":
    main()
