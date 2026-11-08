"""The Logs buttons of Service Info (core/service_logs.py): the route POST /apps/logs, the unit it asks for, what
systemd says without root, every answer of the App Store's root service, the settings, and the pages.

The root service is the real one (store-helper/swizzin_store.py), started without root on a temporary socket with a
fake journalctl; systemctl is a fake one too. Run from the repository root: python tests/test_service_logs.py
(uses a throwaway htpasswd file: nothing on the machine is touched)."""
import base64
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
os.chdir(ROOT)

# The panel reads its users from /etc/htpasswd: point it to a throwaway file before importing it.
_tmp = tempfile.mkdtemp()
from passlib.apache import HtpasswdFile  # noqa: E402
_ht = HtpasswdFile(os.path.join(_tmp, "htpasswd"), new=True)
_ht.set_password("tester", "tester")
_ht.set_password("friend", "friend")
_ht.save()
with open(os.path.join(ROOT, "swizzin.cfg"), "w") as f:
    f.write(f"FLASK_HTPASSWD_PATH = '{_tmp}/htpasswd'\nADMIN_USER = 'tester'\n")

import swizzin  # noqa: E402  (first: it patches the whole process for eventlet)
import flask  # noqa: E402
from core import service_logs  # noqa: E402
from test_store_helper import Helper, fake_swizzin  # noqa: E402
from test_store_journal import FAKE  # noqa: E402

TRAP = "<img src=x onerror=alert(1)> it's \"mine\" & yours"
# How the panel says to set up or update the root service, with this edition installed by heliedan.sh or by box
HOW = ((False, "sudo bash heliedan.sh install"), (True, "sudo ALTDASH_STORE=yes box upgrade altdash"))
PAGES = [{"name": "sonarr", "pretty_name": "Sonarr", "url": "https://demo.invalid/sonarr", "systemd": "sonarr",
          "img": "sonarr"},
         {"name": "rtorrent", "pretty_name": "rTorrent", "url": False, "systemd": "rtorrent@", "img": "rtorrent"},
         {"name": "plex", "pretty_name": "Plex", "url": "https://demo.invalid/plex", "systemd": "plexmediaserver",
          "img": "plex"},
         {"name": "wireguard", "pretty_name": "Wireguard", "url": False, "systemd": "wg-quick@wg1000",
          "img": "wireguard"},
         {"name": "rutorrent", "pretty_name": "ruTorrent", "url": "https://demo.invalid/rutorrent", "systemd": False,
          "img": "rutorrent"},
         {"name": "mytile", "pretty_name": TRAP, "url": False, "systemd": "mytile", "img": "mytile"},
         {"name": "odd", "pretty_name": "Odd", "url": False, "systemd": "../../etc/x", "img": "odd"},
         {"name": "dash", "pretty_name": "Dash", "url": False, "systemd": "-x", "img": "dash"}]
LINES = ["2026-11-08T02:20:01+0100 sonarr[812]: [Info] Bootstrap: Starting Sonarr",
         "2026-11-08T02:20:04+0100 systemd[1]: Started Sonarr Daemon."]
# systemctl show answers in its own order, whatever the order asked: the panel reads the pairs by name
SHOW = ["InactiveEnterTimestampMonotonic=0", "NRestarts=4", "SubState=running", "Result=success",
        "ActiveEnterTimestampMonotonic={active}", "LoadState=loaded", "ActiveState=active"]
FAKE_SYSTEMCTL = r"""#!{python} -I
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "systemctl.log"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
with open(os.path.join(here, "systemctl.json")) as f:
    b = json.load(f)
sys.stdout.write("".join(line + "\n" for line in b["lines"]))
sys.stdout.flush()
time.sleep(b.get("sleep", 0))
sys.exit(b.get("status", 0))
"""


def auth(user, xhr=True):
    headers = {"Authorization": "Basic " + base64.b64encode(f"{user}:{user}".encode()).decode()}
    if xhr:
        headers["X-Requested-With"] = "XMLHttpRequest"
    return headers


class LogsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.mkdtemp()
        fake_swizzin(cls.base)
        cls.bin = os.path.join(cls.base, "bin")
        os.mkdir(cls.bin)
        for name, source in (("journalctl", FAKE), ("systemctl", FAKE_SYSTEMCTL)):
            with open(os.path.join(cls.bin, name), "w") as f:
                f.write(source.format(python=sys.executable))
            os.chmod(os.path.join(cls.bin, name), 0o755)
        cls.helper = Helper(cls.base, extra=("--journalctl", os.path.join(cls.bin, "journalctl"),
                                             "--journal-timeout", "1"))

    @classmethod
    def tearDownClass(cls):
        cls.helper.stop()
        shutil.rmtree(cls.base)

    def setUp(self):
        self.journal(output="".join(line + "\n" for line in LINES))
        self.active = int((time.monotonic() - 3600) * 1e6)  # an hour ago
        self.systemctl([line.format(active=self.active) for line in SHOW])
        for name in ("calls.log", "systemctl.log"):
            try:
                os.unlink(os.path.join(self.bin, name))
            except FileNotFoundError:
                pass
        for patcher in (mock.patch.dict(swizzin.app.config, {"STORE_SOCKET": self.helper.path}),
                        mock.patch.dict(os.environ, {"PATH": self.bin + os.pathsep + os.environ["PATH"]}),
                        mock.patch("core.util.generate_page_list", return_value=PAGES),
                        mock.patch.object(swizzin.limiter, "enabled", False)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = swizzin.app.test_client()

    def journal(self, **behaviour):
        with open(os.path.join(self.bin, "behaviour.json"), "w") as f:
            json.dump(behaviour, f)

    def systemctl(self, lines, status=0, sleep=0):
        with open(os.path.join(self.bin, "systemctl.json"), "w") as f:
            json.dump({"lines": lines, "status": status, "sleep": sleep}, f)

    def journal_calls(self):
        try:
            with open(os.path.join(self.bin, "calls.log")) as f:
                return [json.loads(line)["argv"] for line in f]
        except FileNotFoundError:
            return []

    def systemctl_calls(self):
        with open(os.path.join(self.bin, "systemctl.log")) as f:
            return [json.loads(line) for line in f]

    def post(self, user="tester", body=None, content_type="application/json", data=None):
        headers = auth(user) if user else {"X-Requested-With": "XMLHttpRequest"}
        if body is not None:
            data = json.dumps(body)
        return self.client.post("/apps/logs", data=data, content_type=content_type, headers=headers)

    def logs(self, application="sonarr", user="tester"):
        r = self.post(user, {"application": application})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.headers["Cache-Control"], "no-store")  # a log can hold secrets
        self.assertEqual(r.mimetype, "application/json")
        return r.get_json()


class Route(LogsTest):
    def test_the_lines_and_the_state_of_a_service(self):
        data = self.logs()
        self.assertEqual(data["name"], "Sonarr")
        self.assertEqual(data["unit"], "sonarr.service")
        self.assertEqual((data["journal"], data["lines"], data["message"]), ("ok", LINES, None))
        since = data["state"].pop("since")
        self.assertLessEqual(abs(since - (time.time() - 3600)), 2)
        self.assertEqual(data["state"], {"load": "loaded", "active": "active", "sub": "running", "result": "success",
                                         "restarts": 4})
        self.assertEqual(self.journal_calls(), [["--unit=sonarr.service", "--lines=100", "--output=short-iso",
                                                 "--no-hostname", "--all", "--quiet", "--no-pager"]])
        (show,) = self.systemctl_calls()
        self.assertEqual(show[:2], ["show", "--no-pager"])
        self.assertEqual(show[-2:], ["--", "sonarr.service"])
        self.assertEqual(sorted(show[2:-2]), sorted("--property=" + p for p in service_logs.PROPERTIES))

    def test_the_unit_is_the_one_restart_acts_on(self):
        """A template takes the account's name, as for the Restart button (/apps/service)."""
        for application, unit in (("rtorrent", "rtorrent@tester.service"), ("plex", "plexmediaserver.service"),
                                  ("wireguard", "wg-quick@wg1000.service")):
            with self.subTest(application):
                self.assertEqual(self.logs(application)["unit"], unit)
                self.assertEqual(self.journal_calls()[-1][0], "--unit=" + unit)
                self.assertEqual(self.systemctl_calls()[-1][-1], unit)

    def test_only_the_admin(self):
        r = self.post("friend", {"application": "sonarr"})
        self.assertEqual(r.status_code, 403)
        for headers in ({"X-Requested-With": "XMLHttpRequest"}, {},
                        {"Authorization": "Basic " + base64.b64encode(b"tester:wrong").decode()}):
            with self.subTest(headers=headers):
                r = self.client.post("/apps/logs", data='{"application": "sonarr"}', content_type="application/json",
                                     headers=headers)
                self.assertIn(r.status_code, (302, 401))  # the login page, or 401 for a script
        self.assertEqual(self.journal_calls(), [])

    def test_json_only(self):
        """A form of another site can send a form or text, never JSON: nothing is read for these."""
        cases = [("application/x-www-form-urlencoded", "application=sonarr"),
                 ("multipart/form-data; boundary=x", '--x\r\nContent-Disposition: form-data; name="application"'
                                                     '\r\n\r\nsonarr\r\n--x--\r\n'),
                 ("text/plain", '{"application": "sonarr"}'),
                 ("application/json", '{"application": "son'),
                 ("application/json", '["sonarr"]'),
                 ("application/json", '"sonarr"'),
                 ("application/json", ""),
                 ("application/json", "{}"),
                 ("application/json", '{"application": 1}'),
                 ("application/json", '{"application": ["sonarr"]}')]
        for content_type, data in cases:
            with self.subTest(content_type=content_type, data=data):
                self.assertEqual(self.post(content_type=content_type, data=data).status_code, 400)
        self.assertEqual(self.client.get("/apps/logs", headers=auth("tester")).status_code, 405)
        self.assertEqual(self.journal_calls(), [])

    def test_only_a_service_of_service_info(self):
        """An app that is not installed or not shown to the admin, one without a service, and profiles whose service
        is not a name the root service takes: 404, and nothing is run."""
        for application in ("radarr", "rutorrent", "odd", "dash", "../sonarr", "sonarr.service", "", "SONARR"):
            with self.subTest(application):
                r = self.post(body={"application": application})
                self.assertEqual((r.status_code, r.get_json()), (404, {"error": "unknown service"}))
        self.assertEqual(self.journal_calls(), [])

    def test_the_rate_limit_spares_the_admin(self):
        with mock.patch.object(swizzin.limiter, "enabled", True):
            address = {"REMOTE_ADDR": "203.0.113.8"}  # documentation address (RFC 5737), for this test only
            codes = [self.client.post("/apps/logs", data='{"application": "sonarr"}', content_type="application/json",
                                      headers=auth("tester"), environ_base=address).status_code for _ in range(7)]
            self.assertEqual(codes, [200] * 7)


class State(LogsTest):
    def test_stopped_failed_and_unknown(self):
        inactive = int((time.monotonic() - 120) * 1e6)
        self.systemctl(["ActiveState=failed", "SubState=failed", "Result=exit-code", "NRestarts=5", "LoadState=loaded",
                        "ActiveEnterTimestampMonotonic=%d" % self.active,
                        "InactiveEnterTimestampMonotonic=%d" % inactive])
        state = self.logs()["state"]
        self.assertEqual((state["active"], state["result"], state["restarts"]), ("failed", "exit-code", 5))
        self.assertLessEqual(abs(state["since"] - (time.time() - 120)), 2)  # when it stopped, not when it started
        self.systemctl(["LoadState=not-found", "ActiveState=inactive", "SubState=dead", "Result=success",
                        "NRestarts=0", "ActiveEnterTimestampMonotonic=0", "InactiveEnterTimestampMonotonic=0"])
        self.assertEqual(self.logs()["state"], {"load": "not-found", "active": "inactive", "sub": "dead",
                                                "result": "success", "restarts": 0, "since": None})

    def test_odd_values_are_left_out(self):
        """An older systemd without NRestarts (before 235), and values that are not systemd's words."""
        self.systemctl(["ActiveState=<script>", "SubState=running fine", "Result=", "LoadState=loaded",
                        "ActiveEnterTimestampMonotonic=-5", "Weird line", "=x"])
        self.assertEqual(self.logs()["state"], {"load": "loaded", "active": None, "sub": None, "result": None,
                                                "restarts": None, "since": None})

    def test_without_systemctl_the_lines_still_come(self):
        self.systemctl([], status=1)
        data = self.logs()
        self.assertEqual((data["state"], data["lines"]), (None, LINES))
        with mock.patch.dict(os.environ, {"PATH": os.path.join(self.base, "nothing")}):
            data = self.logs()
        self.assertEqual((data["state"], data["lines"]), (None, LINES))

    def test_a_systemctl_that_does_not_answer(self):
        self.systemctl([line.format(active=self.active) for line in SHOW], sleep=30)  # its output, then it hangs
        with mock.patch.object(service_logs, "SYSTEMCTL_TIMEOUT", 0.5):
            started = time.monotonic()
            data = self.logs()
        self.assertEqual((data["state"], data["journal"]), (None, "ok"))
        self.assertLess(time.monotonic() - started, 5)


class Journal(LogsTest):
    def test_without_the_root_service(self):
        """heliedan.sh remove-store, or never installed: the panel says how to set it up, and shows the state."""
        missing = os.path.join(self.base, "no-such.sock")
        real_isfile = os.path.isfile
        for altdash, how in HOW:
            with self.subTest(altdash=altdash), \
                    mock.patch.dict(swizzin.app.config, {"STORE_SOCKET": missing}), \
                    mock.patch.object(swizzin.os.path, "isfile", side_effect=lambda p: altdash if
                                      p == "/install/.altdash.lock" else real_isfile(p)):
                with self.assertLogs(swizzin.app.logger, "WARNING") as logs:
                    data = self.logs()
                self.assertIn("App Store service does not answer", logs.output[0])
                self.assertEqual((data["journal"], data["lines"]), ("missing", []))
                self.assertIn(how, data["message"])
                self.assertEqual(data["state"]["restarts"], 4)

    def test_an_older_root_service(self):
        """One from before the operation "journal" answers bad-request: the panel says to update it."""
        real_isfile = os.path.isfile
        for altdash, how in HOW:
            with self.subTest(altdash=altdash), \
                    mock.patch.object(service_logs, "helper_call",
                                      return_value={"ok": False, "error": "bad-request"}), \
                    mock.patch.object(swizzin.os.path, "isfile", side_effect=lambda p: altdash if
                                      p == "/install/.altdash.lock" else real_isfile(p)):
                data = self.logs()
            self.assertEqual((data["journal"], data["lines"]), ("outdated", []))
            self.assertIn("older than the panel", data["message"])
            self.assertIn(how + " updates it", data["message"])
            self.assertEqual(data["state"]["active"], "active")

    def test_a_service_the_root_service_does_not_read(self):
        """A custom tile (core/custom/profiles.py): its service is not in the root service's list."""
        data = self.logs("mytile")
        self.assertEqual((data["name"], data["unit"], data["journal"]), (TRAP, "mytile.service", "not-allowed"))
        self.assertIn("services the panel knows only", data["message"])
        self.assertEqual(self.journal_calls(), [])

    def test_every_answer_has_its_sentence(self):
        for reply, journal in (({"ok": False, "error": "busy"}, "busy"), ({"ok": False, "error": "timeout"}, "timeout"),
                               ({"ok": False, "error": "no-journal"}, "no-journal"),
                               ({"ok": False, "error": "failed"}, "error"),
                               ({"ok": False, "error": "invalid"}, "error"),
                               ({"ok": False}, "error"), (["x"], "error"), ({"ok": "yes"}, "error"),
                               ({"ok": True, "lines": "abc"}, "empty")):
            with self.subTest(reply), mock.patch.object(service_logs, "helper_call", return_value=reply):
                data = self.logs()
                self.assertEqual((data["journal"], data["lines"], data["message"]),
                                 ("ok" if journal == "empty" else journal, [], service_logs.MESSAGES[journal]))

    def test_a_journalctl_that_does_not_answer(self):
        self.journal(output="", sleep=30)
        data = self.logs()
        self.assertEqual((data["journal"], data["message"]), ("timeout", service_logs.MESSAGES["timeout"]))

    def test_no_lines_cut_lines_and_errors(self):
        self.journal(output="")
        data = self.logs()
        self.assertEqual((data["journal"], data["lines"], data["message"]), ("ok", [], service_logs.MESSAGES["empty"]))
        self.journal(output="Failed to open the journal\n", status=1)
        data = self.logs()
        self.assertEqual(data["lines"], ["Failed to open the journal"])
        self.assertIn("(code 1)", data["message"])
        self.journal(output="x" * 900 + "\n", repeat=400)
        data = self.logs()
        self.assertEqual((data["journal"], data["message"]), ("ok", service_logs.MESSAGES["cut"]))
        self.assertEqual(set(data["lines"]), {"x" * 900})

    def test_lines_from_swizzin_cfg_and_who_asked(self):
        with mock.patch.object(service_logs, "helper_call", wraps=service_logs.helper_call) as call:
            self.logs()
        request = call.call_args[0][0]
        self.assertEqual((request["op"], request["unit"], request["lines"]), ("journal", "sonarr.service", 100))
        self.assertRegex(request["requester"], r"^tester from \S+$")

    def test_one_read_at_a_time(self):
        """While another request reads, a second one waits its turn, then says the journal is busy."""
        lock = swizzin.app.extensions["service_logs"]["lock"]
        with mock.patch.object(service_logs, "TURN", 0.2):
            lock.acquire()
            try:
                data = self.logs()
            finally:
                lock.release()
        self.assertEqual((data["journal"], data["message"]), ("busy", service_logs.MESSAGES["busy"]))
        self.assertEqual(self.journal_calls(), [])
        self.assertEqual(self.logs()["journal"], "ok")


class Settings(unittest.TestCase):
    def test_defaults_and_invalid_values(self):
        self.assertEqual(service_logs.settings({}), (True, 100))
        self.assertEqual(service_logs.settings({"SERVICE_LOGS": False, "SERVICE_LOGS_LINES": 200}), (False, 200))
        logger = mock.Mock()
        for value in ("False", 0, None, "yes"):
            self.assertEqual(service_logs.settings({"SERVICE_LOGS": value}, logger)[0], True)
        for value in (0, -1, 201, 10000, "50", True, 50.0, None):
            self.assertEqual(service_logs.settings({"SERVICE_LOGS_LINES": value}, logger)[1], 100)
        self.assertEqual(logger.warning.call_count, 12)
        self.assertIn("SERVICE_LOGS_LINES = '50' is invalid", logger.warning.call_args_list[-4][0][0] %
                      logger.warning.call_args_list[-4][0][1:])

    def test_turned_off_there_is_no_route(self):
        for on in (False, True):
            with self.subTest(on=on):
                app = flask.Flask("test-%s" % on)
                app.config.update(SERVICE_LOGS=on, ADMIN_USER="tester")
                service_logs.register(app, swizzin.htpasswd)
                self.assertEqual(app.config["SERVICE_LOGS"], on)
                self.assertEqual("/apps/logs" in [r.rule for r in app.url_map.iter_rules()], on)

    def test_unit_of(self):
        for systemd, unit in (("sonarr", "sonarr.service"), ("rtorrent@", "rtorrent@tester.service"),
                              ("x.service", "x.service"), ("", None), (False, None), (None, None), ("-x", None),
                              ("../x", None), ("a..b", None), ("a b", None), ("a*", None), ("\u00e9", None)):
            with self.subTest(systemd):
                self.assertEqual(service_logs.unit_of(systemd, "tester"), unit)


class Pages(unittest.TestCase):
    """The Logs buttons: in Overview, for the admin, on each line of Service Info; never in Classic."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        for patcher in (mock.patch.object(swizzin.layout_choices, "path", os.path.join(self.dir, "layouts.json")),
                        mock.patch.object(swizzin.limiter, "enabled", False)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = swizzin.app.test_client()

    def page(self, user, layout="overview", shared=False, config=None, path="/"):
        swizzin.layout_choices.set(user, layout)
        real_isfile = os.path.isfile
        with mock.patch.object(swizzin, "get_mounts", return_value=["/"]), \
                mock.patch.object(swizzin, "generate_page_list", return_value=PAGES), \
                mock.patch.dict(swizzin.app.config, dict({"SHAREDSERVER": shared}, **(config or {}))), \
                mock.patch.object(swizzin.os.path, "isfile",
                                  side_effect=lambda p: False if p.startswith("/install/") else real_isfile(p)):
            r = self.client.get(path, headers=auth(user, xhr=False))
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True)

    def test_the_admin_in_overview(self):
        for shared in (False, True):
            with self.subTest(shared=shared):
                page = self.page("tester", shared=shared)
                self.assertEqual(re.findall(r'data-logs="([^"]*)"', page),
                                 ["mytile", "dash", "odd", "plex", "rtorrent", "sonarr", "wireguard"])  # by name
                self.assertIn('<button type="button" class="btn btn-xs btn-secondary" id="logs_sonarr" '
                              'data-logs="sonarr" aria-controls="ov-logs" aria-expanded="false">Logs<span '
                              'class="sr-only"> of Sonarr</span></button>', page)
                self.assertEqual(page.count('id="ov-logs"'), 1)
                self.assertIn('data-url="/apps/logs" data-logout="/logout" hidden>', page)
                self.assertEqual(page.count('src="/static/js/service_logs.js"'), 1)
                # the panel comes right after Service Info, in the row, before Details
                self.assertLess(page.index('>Service Info<'), page.index('id="ov-logs"'))
                self.assertLess(page.index('id="ov-logs"'), page.index('id="ov-details"'))

    def test_a_trapped_name_is_text(self):
        page = self.page("tester")
        self.assertNotIn("<img src=x", page)
        self.assertIn('<span class="sr-only"> of &lt;img src=x onerror=alert(1)&gt; it&#39;s &#34;mine&#34; &amp; '
                      'yours</span>', page)

    def test_nobody_else_and_never_in_classic(self):
        for user, layout, shared, config in (("friend", "overview", False, None), ("friend", "overview", True, None),
                                             ("tester", "classic", False, None), ("tester", "classic", True, None),
                                             ("tester", "overview", False, {"SERVICE_LOGS": False})):
            with self.subTest(user=user, layout=layout, shared=shared, config=config):
                page = self.page(user, layout, shared, config)
                for absent in ("data-logs", "ov-logs", "/apps/logs"):
                    self.assertNotIn(absent, page)
                self.assertEqual("service_logs.js" in page, layout == "overview")  # it does nothing without buttons

    def test_under_a_url_base(self):
        """The address given to the script follows URLBASE, as /layout's does."""
        prefix = swizzin.app.wsgi_app.wsgi_app  # under Socket.IO's middleware: core/middleware.py's
        with mock.patch.object(prefix, "prefix", "/panel"):  # URLBASE = "/panel"
            page = self.page("tester", path="/panel/")
        self.assertIn('data-url="/panel/apps/logs" data-logout="/panel/logout"', page)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        os.remove(os.path.join(ROOT, "swizzin.cfg"))
