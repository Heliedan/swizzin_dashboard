#!/usr/bin/env python3
"""Uninstall campaign: every app of store-helper/removable.py is installed, uninstalled through the App Store
helper exactly as from the page, then installed again, each in a fresh throwaway Swizzin container. Neighbours
(two apps, one removed), real restores of the copy of the settings and failures caused on purpose are run the same
way. analyse.py judges each run from photos of the container taken before and after (snapshot.py).

  python3 tests/campaign/remove.py --base                         build swz-rm:base once (see README.md)
  python3 tests/campaign/remove.py --out DIR [--only KEY ...] [cycles] [neighbours] [failures] [panel]

Each container gets this working tree's App Store (store-helper, core, static, templates, swizzin.py), and its
helper reads a table where every app is marked tested: the campaign is what decides it. One line per run is
printed and appended to results.jsonl (a run already there is skipped, --again runs it again); results.md is
written at the end, and each run keeps its transcripts, photos' differences and logs in DIR/<key>/.
"""
import argparse
import dataclasses
import gzip
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "store-helper"))
import analyse  # noqa: E402
import removable  # noqa: E402
from run import ANSWERS, RUN  # noqa: E402


def panel_constant(name):
    """A constant of core/custom/store/__init__.py, read without importing the panel."""
    import ast
    with open(os.path.join(ROOT, "core", "custom", "store", "__init__.py")) as f:
        tree = ast.parse(f.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
            return ast.literal_eval(node.value)
    return {}


AFFECTS = panel_constant("REMOVE_AFFECTS")  # what the confirmation says may stop working

BASE = "swz-rm:base"
LOGIN = "test:Essai-Swz-2026"
SECOND_USER = ("alice", "Essai-Alice-2026")
CODE = ["store-helper", "core", "static", "templates", "swizzin.py"]
TOOLS = ["client.py", "snapshot.py", "panel_remove.py"]
PANEL_PYTHON = "/opt/.venv/swizzin/bin/python"
REQUIRES = {"flood": "rtorrent", "qui": "qbittorrent", "tautulli": "plex"}  # the panel's REQUIRES, nginx aside
WATCH_UNITS = {"deluge": ["deluged@", "deluge-web@"], "rtorrent": ["rtorrent@"]}  # neighbours outside the table

# A value drawn at random when the app is installed: after a reinstall and a restore of the copy, the old value
# must be back. {master} is Swizzin's master user.
RESTORE = {
    "autobrr": ("/home/{master}/.config/autobrr/config.toml", r'sessionSecret\s*=\s*"([^"]+)"'),
    "transmission": ("/home/{master}/.config/transmission-daemon/settings.json", r'"rpc-password":\s*"([^"]+)"'),
    "qbittorrent": ("/home/{master}/.config/qBittorrent/qBittorrent.conf", r'Password_PBKDF2="?(@ByteArray\([^)]+\))'),
    "lidarr": ("/home/{master}/.config/Lidarr/config.xml", r"<ApiKey>([^<]+)</ApiKey>"),
    "prowlarr": ("/home/{master}/.config/Prowlarr/config.xml", r"<ApiKey>([^<]+)</ApiKey>"),
    "plex": ("/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Preferences.xml",
             r'\bMachineIdentifier="([^"]+)"'),
}

HOLD_DPKG_LOCK = ("nohup python3 -c 'import fcntl, os, time; fd = os.open(\"/var/lib/dpkg/lock-frontend\", os.O_RDWR);"
                  " fcntl.lockf(fd, fcntl.LOCK_EX); time.sleep(900)' >/dev/null 2>&1 & sleep 1")
SLOW_APT_DAILY = ("mkdir -p /run/systemd/system/apt-daily.service.d && printf '[Service]\\nExecStartPre=/bin/sleep 900\\n'"
                  " > /run/systemd/system/apt-daily.service.d/campagne.conf && systemctl daemon-reload &&"
                  " systemctl start --no-block apt-daily.service && sleep 2 && systemctl is-active apt-daily.service || true")
FAKE_APT = "cp /bin/sleep /root/apt-get && (nohup /root/apt-get 900 >/dev/null 2>&1 &) && sleep 1"
PAUSE = lambda seconds: ["--test-env", "STORE_TEST_PAUSE=copy:%d" % seconds]  # noqa: E731

NEIGHBOURS = [
    dict(key="voisin-qbittorrent-qui", app="qbittorrent", preinstall=["qbittorrent", "qui"], watch=["qui"]),
    dict(key="voisin-qbittorrent-autobrr", app="qbittorrent", preinstall=["autobrr"], watch=["autobrr"]),
    dict(key="voisin-autobrr-qbittorrent", app="autobrr", preinstall=["qbittorrent"], watch=["qbittorrent"]),
    dict(key="voisin-plex-tautulli", app="plex", preinstall=["plex", "tautulli"], watch=["tautulli"]),
    dict(key="voisin-prowlarr-radarr", app="prowlarr", preinstall=["radarr"], watch=["radarr"]),
    dict(key="voisin-radarr-prowlarr", app="radarr", preinstall=["prowlarr"], watch=["prowlarr"]),
    dict(key="voisin-qbittorrent-deluge", app="qbittorrent", preinstall=["qbittorrent", "deluge"], watch=["deluge"],
         expect="refused", text="conflict"),  # refused at the preview since f4dc9c4; the card names Deluge
]

FAILURES = [
    dict(key="panne-verrou-dpkg", before=HOLD_DPKG_LOCK, expect="refused", text="is locked"),
    dict(key="panne-apt-daily", before=SLOW_APT_DAILY, expect="refused", text="automatic system updates"),
    dict(key="panne-processus-apt", before=FAKE_APT, expect="refused", text="apt-get is running"),
    dict(key="panne-script-modifie", before="echo '# campagne' >> /etc/swizzin/scripts/remove/autobrr.sh",
         expect="refused", text="script-changed"),
    dict(key="panne-source-modifiee", before="echo '# campagne' >> /etc/swizzin/sources/functions/utils",
         expect="refused", text="script-changed"),
    dict(key="panne-petit-disque", run=["--tmpfs", "/var/backups/swizzin-store:size=64m,mode=0700"],
         expect="refused", text="not enough free space"),
    dict(key="panne-disque-plein", run=["--tmpfs", "/var/backups/swizzin-store:size=300m,mode=0700"],
         helper_args=PAUSE(20), on_phase=[["copy", "sleep 2; dd if=/dev/zero of=/var/backups/swizzin-store/campagne-plein"
                                                   " bs=1M 2>/dev/null; true"]],
         expect="put-back", text="the copy of its settings failed"),
    dict(key="panne-stop-copie", helper_args=PAUSE(15), stop_at="copy", expect="put-back", text="stopped at your request"),
    dict(key="panne-redemarrage-copie", helper_args=PAUSE(30),
         on_phase=[["copy", "sleep 3; systemctl restart swizzin-store"]], expect="put-back", text=""),
    dict(key="panne-crash-copie", helper_args=PAUSE(30),
         on_phase=[["copy", "sleep 3; systemctl kill --kill-whom=main --signal=SIGKILL swizzin-store"]],
         expect="put-back", text="put back"),
    # the automatic updates start between the checks and Swizzin's script (a systemd reload can trigger them):
    # the removal waits for them, then goes on
    dict(key="panne-apt-pendant-copie", helper_args=PAUSE(10),
         on_phase=[["copy", SLOW_APT_DAILY.replace("sleep 900", "sleep 25")]], expect="consistent"),
    dict(key="panne-redemarrage-script", app="transmission",
         on_phase=[["remove", "sleep 2; systemctl restart swizzin-store"]], expect="consistent"),
    dict(key="panne-nginx-casse", before="echo 'campagne_casse;' > /etc/nginx/apps/campagne-casse.conf",
         expect="nginx-broken"),
]


def table_data():
    """removable.py's table, every app marked tested on the containers' system (Ubuntu 24.04), in the JSON shape
    of the helper's --table."""
    common, pins, apps = removable.load()
    return {"common": list(common), "pins": {k: list(v) for k, v in pins.items()},
            "apps": {name: dict(dataclasses.asdict(entry), tested=["noble"]) for name, entry in apps.items()}}


def plain_entry(entry):
    return json.loads(json.dumps(entry))  # tuples as lists, as analyse.py expects


class Campaign:
    def __init__(self, opts):
        self.opts = opts
        self.docker_cmd = shlex.split(opts.docker)
        self.table = table_data()
        self.results = {}
        if opts.out:
            os.makedirs(opts.out, exist_ok=True)
            self.results_path = os.path.join(opts.out, "results.jsonl")
            if os.path.exists(self.results_path):
                with open(self.results_path) as f:
                    for line in f:
                        r = json.loads(line)
                        self.results[r["key"]] = r
            self.table_file = os.path.join(opts.out, "table.json")
            with open(self.table_file, "w") as f:
                json.dump(self.table, f)
        head = subprocess.run(["git", "-C", ROOT, "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", ROOT, "status", "--porcelain", "--", *CODE], capture_output=True, text=True).stdout
        self.code = head + ("+changes" if dirty.strip() else "")

    # --- docker ------------------------------------------------------------------------------

    def docker(self, *args, check=True, timeout=600, data=None):
        r = subprocess.run(self.docker_cmd + list(args), capture_output=True, check=False, timeout=timeout, input=data)
        out = subprocess.CompletedProcess(r.args, r.returncode, r.stdout.decode(errors="replace"),
                                          r.stderr.decode(errors="replace"))
        if check and r.returncode != 0:
            raise RuntimeError("docker %s: %s" % (" ".join(args[:3]), out.stderr.strip()[-500:]))
        return out

    def sh(self, name, script, timeout=600, check=False):
        r = self.docker("exec", "-e", "TERM=xterm", name, "bash", "-c", script, check=False, timeout=timeout)
        if check and r.returncode != 0:
            raise RuntimeError("in the container: %s -> %s %s" % (script[:120], r.returncode, (r.stderr or r.stdout)[-500:]))
        return r.stdout

    def tool(self, name, *args, timeout=600, python=("python3", "-I")):
        r = self.docker("exec", name, *python, *args, check=False, timeout=timeout)
        try:
            return json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise RuntimeError("%s: %s" % (" ".join(args[:2]), (r.stderr or r.stdout)[-800:]))

    def put(self, name, path, text):
        self.docker("exec", "-i", name, "bash", "-c", "cat > " + shlex.quote(path), data=text.encode())

    def fetch(self, name, folder, *paths):
        """Files and folders of the container, copied into folder as the user running the campaign."""
        args = " ".join(shlex.quote(p) for p in paths)
        r = subprocess.run(self.docker_cmd + ["exec", name, "bash", "-c",
                                              "tar -c --ignore-failed-read -f - %s 2>/dev/null" % args],
                           capture_output=True, timeout=300)
        subprocess.run(["tar", "-x", "--no-same-owner", "--no-same-permissions", "-f", "-", "-C", folder,
                        "--transform", "s,.*/,,", "--show-transformed-names"], input=r.stdout, capture_output=True)

    def start(self, name, image, extra=()):
        self.docker("rm", "-f", "-v", name, check=False)
        self.docker(*RUN, *extra, "--name", name, "--hostname", name, image)
        end = time.time() + 240
        while time.time() < end:
            out = self.sh(name, "systemctl is-system-running; test -S /run/swizzin-store/store.sock && echo socket-ready",
                          timeout=30)
            if "socket-ready" in out and re.search(r"^(running|degraded)$", out, re.M):
                return
            time.sleep(2)
        raise RuntimeError("the container did not start (systemd or the App Store helper)")

    def setup(self, name, helper_args=()):
        """This working tree's panel and App Store, root's files only; the helper reads the campaign's table."""
        tar = subprocess.run(["tar", "-C", ROOT, "--exclude=__pycache__", "--exclude=*.pyc", "-c", *CODE],
                             capture_output=True, check=True).stdout
        self.docker("exec", "-i", name, "bash", "-c", "rm -rf /root/fork && mkdir -p /root/fork /root/campaign &&"
                    " tar -x --no-same-owner -C /root/fork && chmod -R go-w /root/fork", data=tar)
        for tool in TOOLS:
            self.docker("cp", os.path.join(HERE, tool), name + ":/root/campaign/" + tool)
        self.docker("cp", self.table_file, name + ":/root/campaign/table.json")
        self.sh(name, "chown -R root:root /root/campaign && chmod -R go-w /root/campaign", check=True)
        args = " ".join(shlex.quote(a) for a in ["--table", "/root/campaign/table.json", *helper_args])
        self.put(name, "/root/campaign/dropin.conf", "[Service]\nExecStart=\nExecStart=/usr/bin/python3 -I -B "
                 "/usr/local/lib/swizzin-store/swizzin_store.py %s\n" % args)
        self.sh(name, "mkdir -p /etc/systemd/system/swizzin-store.service.d && cp /root/campaign/dropin.conf"
                " /etc/systemd/system/swizzin-store.service.d/campagne.conf && cd /root/fork &&"
                " cp -a swizzin.py /opt/swizzin/ && cp -a core static templates store-helper /opt/swizzin/ &&"
                " chown -R swizzin:swizzin /opt/swizzin && bash store-helper/install.sh && systemctl restart panel",
                timeout=300, check=True)
        self.wait_helper(name)
        # Ubuntu's automatic updates start on their own in a fresh container and make a removal refuse, as they
        # should (panne-apt-daily checks it on purpose): the other runs wait for them to end
        self.sh(name, "systemctl stop apt-daily.timer apt-daily-upgrade.timer; for i in $(seq 1 180); do"
                " systemctl is-active -q apt-daily.service apt-daily-upgrade.service"
                " || break; sleep 5; done", timeout=960)
        catalogue = self.tool(name, "/root/campaign/client.py", "--catalogue")
        if "remove" not in catalogue.get("features", []):
            raise RuntimeError("the helper in the container cannot uninstall: %s" % catalogue.get("features"))
        end = time.time() + 120
        while self.panel_status(name) != "200":
            if time.time() > end:
                raise RuntimeError("the panel does not answer after the setup")
            time.sleep(3)

    def wait_helper(self, name, timeout=120):
        end = time.time() + timeout
        while time.time() < end:
            if "ready" in self.sh(name, "systemctl is-active -q swizzin-store && test -S /run/swizzin-store/store.sock"
                                  " && echo ready", timeout=30):
                return
            time.sleep(2)
        raise RuntimeError("the App Store helper did not come back")

    def panel_status(self, name):
        return self.sh(name, "curl -sk -o /dev/null -m 10 -w '%%{http_code}' -u %s https://127.0.0.1/" % shlex.quote(LOGIN),
                       timeout=30).strip()

    def web_check(self, name, app):
        """HTTP status of the app's page behind nginx (as run.py), or None when it has no nginx location."""
        script = r"""f=/etc/nginx/apps/%s.conf; [ -f "$f" ] || exit 0
locs=$(grep -oP '^\s*location\s+(=\s+|\^~\s+)?\K/[^\s{]+' "$f")
loc=$(echo "$locs" | grep -m1 -i "^/$(basename "$f" .conf)" || echo "$locs" | grep -m1 -v '^/\.well-known'); [ -n "$loc" ] || exit 0
code=$(curl -sk -o /dev/null -L --max-redirs 5 -m 20 -w '%%{http_code}' -u %s "https://127.0.0.1$loc")
echo "$loc $code" """ % (app, shlex.quote(LOGIN))
        return self.sh(name, script, timeout=60).strip() or None

    def github_quota(self):
        """Wait while the anonymous GitHub API quota of this address is low: many installers use it."""
        try:
            with urllib.request.urlopen("https://api.github.com/rate_limit", timeout=20) as r:
                core = json.load(r)["resources"]["core"]
        except (OSError, ValueError, KeyError):
            return
        if core["remaining"] < 12:
            wait = max(0, core["reset"] - time.time()) + 10
            print("  (GitHub quota low: %d left, waiting %d min)" % (core["remaining"], wait // 60 + 1), flush=True)
            time.sleep(wait)

    # --- one app -----------------------------------------------------------------------------

    def install(self, name, app, transcript):
        self.github_quota()
        r = self.docker("exec", name, "python3", "-I", "/root/campaign/client.py", app, transcript,
                        json.dumps(ANSWERS.get(app, [])), check=False, timeout=110 * 60)
        try:
            out = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            out = {"result": "client-error", "stderr": r.stderr[-1000:]}
        out.pop("tail", None)
        out["web"] = self.web_check(name, app) if out.get("result") == "installed" else None
        return out

    def ctx(self, name, entry):
        users = self.sh(name, "{ cut -d: -f1 /root/.master.info; for f in /root/*.info; do [ -e \"$f\" ] && cut -d: -f1"
                        " \"$f\"; done; cut -d: -f1 /etc/htpasswd; } 2>/dev/null | sort -u | while read -r u;"
                        " do [ -n \"$u\" ] && [ -d \"/home/$u\" ] && echo \"$u\"; done").split()
        master = self.sh(name, "cut -d: -f1 /root/.master.info").strip()
        owner = ""
        if entry.get("owner_unit"):
            owner = self.sh(name, "systemctl show -p User --value %s.service" % entry["owner_unit"]).strip()
        return {"users": sorted(set(users)), "master": master, "owner": owner if owner in users else master}

    def expanded(self, entry, ctx, key):
        return [p for pattern in entry.get(key, ()) for p in analyse.expand(pattern, ctx)]

    def restore_value(self, name, app, ctx):
        if app not in RESTORE:
            return None
        path, pattern = RESTORE[app]
        text = self.sh(name, "cat %s 2>/dev/null" % shlex.quote(path.replace("{master}", ctx["master"])))
        m = re.search(pattern, text)
        return m.group(1) if m else None

    def settle(self, name, app, entry, ctx):
        """Some apps write their settings at their first start, after the installer: wait for them (3 min at most)."""
        paths = self.expanded(entry, ctx, "copy")
        if not paths and app not in RESTORE:
            time.sleep(15)
            return True
        end = time.time() + 180
        while time.time() < end:
            ready = self.restore_value(name, app, ctx) if app in RESTORE else \
                any(self.tool(name, "/root/campaign/snapshot.py", "exists", *paths).values())
            if ready:
                time.sleep(5)
                return True
            time.sleep(5)
        return False

    def witnesses(self, name, entry, ctx, table_paths):
        """The users' files (never to be touched), files in the folders it keeps, and traps in its settings folder:
        links to a user's file and folder, a FIFO, a file of another user (none of them may enter the copy)."""
        files = []
        script = ["set -e"]
        for user in ctx["users"]:
            for folder in ("torrents", "Downloads", "media"):
                d = "/home/%s/%s" % (user, folder)
                script.append("[ -d %s ] || { install -d -o %s -g %s %s && echo created %s; }" % (d, user, user, d, d))
                f = d + "/campagne-temoin.bin"
                script.append("head -c 65536 /dev/urandom > %s && chown %s: %s" % (f, user, f))
                files.append(f)
        for path, kinds in table_paths.get("kept", {}).items():
            if kinds == ["d"]:
                f = path + "/campagne-garde.bin"
                script.append("head -c 4096 /dev/urandom > %s && chown --reference=%s %s" % (shlex.quote(f), shlex.quote(path), shlex.quote(f)))
                files.append(f)
        target = "/home/%s/media/campagne-film.mkv" % ctx["master"]
        script.append("head -c 131072 /dev/urandom > %s && chown %s: %s" % (target, ctx["master"], target))
        files.append(target)
        traps = None
        folders = [p for p, kinds in table_paths.get("copy", {}).items() if kinds == ["d"] and "*" not in p]
        if folders:
            d = folders[0]
            q = lambda s: shlex.quote(s)  # noqa: E731
            traps = {"folder": d, "link_file": d + "/campagne-lien-fichier", "link_dir": d + "/campagne-lien-dossier",
                     "fifo": d + "/campagne-fifo", "foreign": d + "/campagne-autre.bin", "target": target}
            script += ["ln -s %s %s && chown -h --reference=%s %s" % (target, q(traps["link_file"]), q(d), q(traps["link_file"])),
                       "ln -s /home/%s/media %s && chown -h --reference=%s %s" % (ctx["master"], q(traps["link_dir"]), q(d),
                                                                               q(traps["link_dir"])),
                       "mkfifo %s && chown --reference=%s %s" % (q(traps["fifo"]), q(d), q(traps["fifo"])),
                       "echo campagne > %s && chown 65534:65534 %s" % (q(traps["foreign"]), q(traps["foreign"]))]
        created = [line.split(" ", 1)[1] for line in self.sh(name, "\n".join(script), check=True).splitlines()
                   if line.startswith("created ")]
        hashes = self.tool(name, "/root/campaign/snapshot.py", "hash", *files)
        if traps:
            traps["target_sha"] = hashes[target]
        return hashes, traps, created

    def app_state(self, name, app, entry):
        """Its lock and the state of its units: what 'put back as it was' means."""
        units = []
        for unit in entry.get("units", ()):
            pattern = unit + "*.service" if unit.endswith("@") else unit + ".service"
            units += self.sh(name, "systemctl list-units --all --plain --no-legend --type=service %s | awk '{print $1}';"
                             " ls /etc/systemd/system/*.wants/ 2>/dev/null | grep -x %s || true"
                             % (shlex.quote(pattern), shlex.quote(pattern.replace("*", ".*")))).split()
        state = {}
        for unit in sorted(set(u for u in units if u.endswith(".service") and not u.endswith("@.service"))):
            state[unit] = self.sh(name, "systemctl is-active %s; systemctl is-enabled %s" % (unit, unit)).split()
        return {"lock": "yes" in self.sh(name, "test -e /install/.%s.lock && echo yes" % app), "units": state}

    def watch_state(self, name, apps):
        return {a: dict(self.app_state(name, a, self.table["apps"].get(a) or {"units": WATCH_UNITS.get(a, [])}),
                        web=self.web_check(name, a)) for a in apps}

    def restore(self, name, app, ctx, restore_txt):
        """RESTORE.txt followed to the letter, after the reinstall."""
        steps = {}
        for key, pattern in (("stop", r"Stop it:\s+(systemctl stop .+)$"), ("tar", r"Put the files back:\s+(tar .+)$"),
                             ("start", r"Start it again:\s+(systemctl start .+)$")):
            m = re.search(pattern, restore_txt, re.M)
            if not m:
                return {"error": "RESTORE.txt has no %s step" % key}
            steps[key] = m.group(1).strip()
        out = {"steps": steps, "before_restore": self.restore_value(name, app, ctx)}
        out["outputs"] = [self.sh(name, steps["stop"] + " 2>&1"), self.sh(name, steps["tar"] + " 2>&1")]
        if "nginx -t" in restore_txt:
            out["outputs"].append(self.sh(name, "nginx -t 2>&1 && systemctl reload nginx 2>&1"))
        out["outputs"].append(self.sh(name, steps["start"] + " 2>&1"))
        time.sleep(30)
        out["after"] = self.restore_value(name, app, ctx)
        units = steps["start"].split()[2:]
        out["active"] = all(self.sh(name, "systemctl is-active " + u).strip() == "active" for u in units)
        out["web"] = self.web_check(name, app)
        return out

    def via_panel(self, name, app):
        """The removal through the real panel (panel_remove.py), in the shape of client.py's."""
        start = time.monotonic()
        out = self.tool(name, "/root/campaign/panel_remove.py", app, LOGIN, "/root/campaign/remove.txt",
                        timeout=70 * 60, python=(PANEL_PYTHON,))
        done = out.get("done") or {}
        preview = out.get("preview") or {}
        return {"app": app, "panel": out, "preview": preview.get("preview"), "phases": out.get("phases"), "errors": [],
                "result": done.get("result"), "checks": done.get("checks"), "archive": done.get("archive"),
                "message": done.get("message"), "seconds": round(time.monotonic() - start)}

    def image_with(self, apps):
        """An image of a container where these apps were installed (made once, kept for the campaign)."""
        image = BASE.split(":")[0] + ":with-" + "-".join(apps)
        if self.docker("image", "inspect", image, check=False).returncode == 0:
            return image
        name = "swz-rm-image"
        print("  (making %s)" % image, flush=True)
        try:
            self.start(name, BASE)
            self.setup(name)
            for app in apps:
                r = self.install(name, app, "/root/campaign/image-%s.txt" % app)
                if r.get("result") != "installed":
                    raise RuntimeError("%s did not install for %s: %s" % (app, image, r.get("result")))
            self.docker("stop", "-t", "90", name, timeout=200)
            self.docker("commit", name, image, timeout=900)
        finally:
            self.docker("rm", "-f", "-v", name, check=False)
        return image

    # --- one run -----------------------------------------------------------------------------

    def run(self, spec):
        key, app = spec["key"], spec["app"]
        entry = plain_entry(self.table["apps"][app])
        folder = os.path.join(self.opts.out, key)
        os.makedirs(folder, exist_ok=True)
        record = {"key": key, "app": app, "kind": spec["kind"], "code": self.code, "started_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        name = "swz-rm-" + key
        detail = {}
        try:
            prereq = spec.get("image_apps")
            image = self.image_with(prereq) if prereq else BASE
            record["image"] = image
            self.start(name, image, spec.get("run", ()))
            self.setup(name, spec.get("helper_args", ()))
            for other in spec.get("preinstall", ()):
                r = self.install(name, other, "/root/campaign/preinstall-%s.txt" % other)
                record.setdefault("preinstall", {})[other] = r
                if r.get("result") != "installed":
                    raise RuntimeError("%s did not install first: %s" % (other, r.get("result")))
            self.tool(name, "/root/campaign/snapshot.py", "take", "/root/campaign/s0.json.gz", timeout=900)
            if app in spec.get("preinstall", ()) or spec.get("image_apps") and app in spec["image_apps"]:
                record["install"] = record.get("preinstall", {}).get(app) or {"result": "installed", "web": self.web_check(name, app)}
            else:
                record["install"] = self.install(name, app, "/root/campaign/install.txt")
                if record["install"].get("result") != "installed":
                    raise Skip("the installation failed: %s" % record["install"].get("result"))
            ctx = self.ctx(name, entry)
            record["settled"] = self.settle(name, app, entry, ctx)
            ctx = record["ctx"] = self.ctx(name, entry)
            record["table_paths"] = {k: self.tool(name, "/root/campaign/snapshot.py", "exists", *self.expanded(entry, ctx, k))
                                     for k in ("copy", "gone", "kept")}
            record["witness"] = {}
            record["witness"]["before"], record["traps"], record["created"] = \
                self.witnesses(name, entry, ctx, record["table_paths"])
            if spec.get("before"):
                record["before_output"] = self.sh(name, spec["before"] + " 2>&1", timeout=120)[-500:]
            record["state_before"] = self.app_state(name, app, entry)
            record["watch_before"] = self.watch_state(name, spec.get("watch", ()))
            restore_before = self.restore_value(name, app, ctx)
            self.tool(name, "/root/campaign/snapshot.py", "take", "/root/campaign/s1.json.gz", timeout=900)
            self.sh(name, "nohup bash -c 'echo $$ > /root/campaign/panel.pid; while :; do curl -sk -o /dev/null -m 5"
                    " -w \"%%{http_code}\\n\" -u %s https://127.0.0.1/ >> /root/campaign/panel.txt; sleep 2; done'"
                    " >/dev/null 2>&1 &" % shlex.quote(LOGIN))
            if spec.get("via_panel"):
                removal = self.via_panel(name, app)
            else:
                extra = {k: spec[k] for k in ("stop_at", "on_phase") if spec.get(k)}
                removal = self.tool(name, "/root/campaign/client.py", "--remove", app, "/root/campaign/remove.txt",
                                    json.dumps(extra), timeout=70 * 60)
            if "helper-closed" in removal.get("errors", []):
                self.wait_helper(name, timeout=700)
                record["last"] = self.tool(name, "/root/campaign/client.py", "--catalogue").get("last")
            self.sh(name, "kill $(cat /root/campaign/panel.pid) 2>/dev/null; true")
            record["panel"] = self.sh(name, "cat /root/campaign/panel.txt").split()
            runner = self.sh(name, "f=$(ls -t /var/log/swizzin-store/*.result 2>/dev/null | head -1); [ -n \"$f\" ] && cat \"$f\"")
            removal["runner_result"] = json.loads(runner) if runner.strip() else None
            removal["skipped"] = (removal["runner_result"] or {}).get("skipped", [])
            record["removal"] = removal
            record["state_after"] = self.app_state(name, app, entry)
            record["watch_after"] = self.watch_state(name, spec.get("watch", ()))
            record["archives_after"] = self.sh(name, "ls -A /var/backups/swizzin-store 2>/dev/null").split()
            self.tool(name, "/root/campaign/snapshot.py", "take", "/root/campaign/s2.json.gz", timeout=900)
            detail["d12"] = self.tool(name, "/root/campaign/snapshot.py", "diff", "/root/campaign/s1.json.gz",
                                      "/root/campaign/s2.json.gz", timeout=900)
            detail["d02"] = self.tool(name, "/root/campaign/snapshot.py", "diff", "/root/campaign/s0.json.gz",
                                      "/root/campaign/s2.json.gz", timeout=900)
            record["witness"]["after"] = self.tool(name, "/root/campaign/snapshot.py", "hash", *record["witness"]["before"])
            archive = removal.get("archive") or (removal.get("runner_result") or {}).get("archive")
            if archive:
                spec_file = {"copy": self.expanded(entry, ctx, "copy"), "skip": entry.get("skip", []),
                             "skipped": removal["skipped"]}
                self.put(name, "/root/campaign/spec.json", json.dumps(spec_file))
                try:
                    record["archive_report"] = self.tool(name, "/root/campaign/snapshot.py", "archive", archive,
                                                         "/root/campaign/s1.json.gz", "/root/campaign/spec.json")
                except RuntimeError as e:
                    record["archive_report"] = {"error": str(e)}
            if spec.get("expect") == "nginx-broken":
                record["after_fix"] = self.sh(name, "rm -f /etc/nginx/apps/campagne-casse.conf && nginx -t 2>&1 &&"
                                              " systemctl reload nginx && sleep 2 && nginx -T 2>/dev/null | grep -ci"
                                              " 'location.*/%s' || true" % app).strip()
            removed = removal.get("result") == "removed"
            if spec.get("reinstall") and removed:
                record["reinstall"] = self.install(name, app, "/root/campaign/reinstall.txt")
                if spec.get("restore") and app in RESTORE and record["reinstall"].get("result") == "installed":
                    report = record.get("archive_report") or {}
                    record["restore"] = self.restore(name, app, ctx, report.get("restore_txt", ""))
                    record["restore"]["before"] = restore_before
            self.fetch(name, folder, "/root/campaign/install.txt", "/root/campaign/remove.txt",
                       "/root/campaign/reinstall.txt", "/root/logs/swizzin.log", "/var/log/swizzin-store")
        except Skip as e:
            record["error"] = str(e)
        except Exception as e:  # the campaign's own trouble: noted, the next run goes on
            record["error"] = "%s: %s" % (type(e).__name__, e)
        finally:
            self.docker("rm", "-f", "-v", name, check=False, timeout=300)
        for k, v in detail.items():
            with gzip.open(os.path.join(folder, k + ".json.gz"), "wt") as f:
                json.dump(v, f)
        record["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if "error" in record:
            record["verdict"] = {"verdict": "not-tested" if "installation failed" in record["error"] else "error",
                                 "fail": [record["error"]], "review": []}
            return record
        try:
            record["verdict"] = analyse.verdict_for(self.judged(spec), entry, dict(record, **detail))
        except Exception as e:  # a bug of the analysis: the run stays, to be judged again
            record["verdict"] = {"verdict": "error", "fail": ["analysis: %s: %s" % (type(e).__name__, e)], "review": []}
        return record

    def save(self, record):
        self.results[record["key"]] = record
        with open(self.results_path, "a") as f:
            f.write(json.dumps(record) + "\n")
        v = record["verdict"]
        print("{}  {:<28} {:<10} {} {}".format(time.strftime("%H:%M:%S"), record["key"], v["verdict"],
                                             "; ".join(v["fail"])[:150], "(%d to review)" % len(v["review"]) if v["review"] else ""),
              flush=True)

    def specs(self):
        out = []
        if "cycles" in self.opts.groups:
            for app in sorted(self.table["apps"]):
                spec = dict(key=app, kind="cycle", app=app, reinstall=True, restore=app in RESTORE)
                if app in REQUIRES:
                    spec.update(image_apps=[REQUIRES[app]], watch=[REQUIRES[app]])
                out.append(spec)
        if "neighbours" in self.opts.groups:
            out += [dict(spec, kind="neighbour", reinstall=spec.get("expect") is None) for spec in NEIGHBOURS]
        if "panel" in self.opts.groups:
            out.append(dict(key="panel-autobrr", kind="panel", app="autobrr", via_panel=True, image_apps=["autobrr"],
                            reinstall=False))
        if "failures" in self.opts.groups:
            for spec in FAILURES:
                app = spec.get("app", "autobrr")
                out.append(dict(spec, kind="failure", app=app, image_apps=[app]))
        if self.opts.only:
            out = [s for s in out if s["key"] in self.opts.only]
        return out

    def main(self):
        for spec in self.specs():
            if spec["key"] in self.results and not self.opts.again:
                continue
            if shutil.disk_usage(self.opts.disk).free / 1e9 < self.opts.min_free_gb:
                print("stopped: less than %s GB free on %s" % (self.opts.min_free_gb, self.opts.disk), flush=True)
                break
            self.save(self.run(spec))
        self.report()

    def judged(self, spec):
        """The spec as analyse.py wants it: what the confirmation names as affected, and the neighbours' entries."""
        others = [(a, plain_entry(self.table["apps"][a])) for a in spec.get("watch", ()) if a in self.table["apps"]]
        return dict(spec, affected=AFFECTS.get(spec["app"], []), others=others)

    def rejudge(self):
        """The verdicts again, with today's analyse.py, from what each run kept (no container)."""
        specs = {s["key"]: s for s in self.specs()}
        for key, record in self.results.items():
            spec = specs.get(key)
            if spec is None or "error" in record:
                continue
            detail = {}
            for k in ("d12", "d02"):
                path = os.path.join(self.opts.out, key, k + ".json.gz")
                if os.path.exists(path):
                    with gzip.open(path, "rt") as f:
                        detail[k] = json.load(f)
            entry = plain_entry(self.table["apps"][record["app"]])
            record["verdict"] = analyse.verdict_for(self.judged(spec), entry, dict(record, **detail))
        with open(self.results_path, "w") as f:
            for record in self.results.values():
                f.write(json.dumps(record) + "\n")
        self.report()

    def report(self):
        lines = ["| Run | App | Verdict | Failures | To review | Removal |", "|---|---|---|---|---|---|"]
        for key in sorted(self.results, key=lambda k: (self.results[k]["kind"], k)):
            r = self.results[key]
            v = r["verdict"]
            removal = r.get("removal") or {}
            lines.append("| {} | {} | {} | {} | {} | {} |".format(
                key, r["app"], v["verdict"], "<br>".join(v["fail"]).replace("|", "/") or "-",
                "<br>".join(v["review"]).replace("|", "/") or "-",
                "%s in %s s" % (removal.get("result"), removal.get("seconds")) if removal else "-"))
        with open(os.path.join(self.opts.out, "results.md"), "w") as f:
            f.write("\n".join(lines) + "\n")

    def build_base(self):
        """swz-camp:base (README.md) with a second Swizzin user, so that every removal concerns two users."""
        name = "swz-rm-base"
        try:
            self.start(name, "swz-camp:base")
            user, password = SECOND_USER
            print(self.sh(name, "/usr/local/bin/swizzin/box adduser %s %s </dev/null 2>&1 | tail -n 5" % (user, password), timeout=900))
            self.sh(name, "id %s && test -d /home/%s && grep -q '^%s:' /etc/htpasswd && test -e /root/%s.info"
                    % (user, user, user, user), check=True)
            self.docker("stop", "-t", "90", name, timeout=200)
            self.docker("commit", name, BASE, timeout=900)
        finally:
            self.docker("rm", "-f", "-v", name, check=False)
        print("built " + BASE)


class Skip(Exception):
    pass


def main():
    p = argparse.ArgumentParser(description="App Store uninstall campaign (throwaway Swizzin containers).")
    p.add_argument("groups", nargs="*", default=["cycles", "neighbours", "failures", "panel"])
    p.add_argument("--out", help="results folder")
    p.add_argument("--only", nargs="+", help="only these runs (keys: the app's name for its cycle, voisin-..., panne-...)")
    p.add_argument("--base", action="store_true", help="build %s from swz-camp:base and stop" % BASE)
    p.add_argument("--docker", default="docker", help='the docker command, for example "sudo -n docker"')
    p.add_argument("--disk", default="/", help="the file system Docker writes to")
    p.add_argument("--min-free-gb", type=float, default=20, help="stop below this much free space")
    p.add_argument("--again", action="store_true", help="also run what is already in results.jsonl")
    p.add_argument("--rejudge", action="store_true", help="judge the runs of --out again (after a change of analyse.py)")
    opts = p.parse_args()
    campaign = Campaign(opts)
    if opts.base:
        campaign.build_base()
    elif not opts.out:
        p.error("--out is needed")
    elif opts.rejudge:
        campaign.rejudge()
    else:
        campaign.main()


if __name__ == "__main__":
    main()
