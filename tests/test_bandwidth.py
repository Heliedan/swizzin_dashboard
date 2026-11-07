"""Bandwidth totals and history of the Network Info card (/stats/vnstat), with a fake vnstat (tests/fake_vnstat.py).

The fake is a small program named vnstat, first in PATH: it prints what each test gives it and notes how it was
called. The real vnstat of the machine is never run.

Run from the repository root: python tests/test_bandwidth.py
(uses a throwaway htpasswd file and a fake vnstat: nothing on the machine is touched)."""
import base64
import json
import os
import sys
import tempfile
import time
import unittest
from html.parser import HTMLParser
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
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

import swizzin  # noqa: E402
import core.util as util  # noqa: E402
from fake_vnstat import EMPTY, NOT_IN_DATABASE, FakeVnstat, traffic, vnstat_json  # noqa: E402

# The totals and top days of templates/top.html before this change, for traffic(): read off the page that the
# previous /stats/vnstat rendered (ten calls of vnstat); see test_totals_and_top_days_as_before.
TOTALS = [["This Hour", "420.00 MB", "70.00 MB", "490.00 MB"],
          ["Last Hour", "1.50 GB", "250.00 MB", "1.75 GB"],
          ["This Day", "34.00 GB", "4.80 GB", "38.80 GB"],
          ["This Month", "3.00 TB", "20.00 GB", "3.02 TB"],
          ["All Time", "123.46 TB", "98.77 TB", "222.22 TB"]]
TOP = [["Mar 14, 2026", "900.00 GB", "100.00 GB", "1000.00 GB"],
       ["Jan 2, 2026", "512.00 GB", "8.00 GB", "520.00 GB"],
       ["Dec 25, 2025", "300.00 GB", "33.00 GB", "333.00 GB"],
       ["Jun 2, 2026", "70.00 GB", "4.00 GB", "74.00 GB"],
       ["Jul 19, 2026", "69.00 GB", "3.00 GB", "72.00 GB"]]
LAST_10_DAYS = [["Nov 7, 2026", "34.00 GB", "4.80 GB", "38.80 GB"],
                ["Nov 6, 2026", "67.00 GB", "3.50 GB", "70.50 GB"],
                ["Nov 5, 2026", "30.00 GB", "2.20 GB", "32.20 GB"],
                ["Nov 4, 2026", "63.00 GB", "900.00 MB", "63.90 GB"],
                ["Nov 3, 2026", "26.00 GB", "4.60 GB", "30.60 GB"],
                ["Nov 2, 2026", "59.00 GB", "3.30 GB", "62.30 GB"],
                ["Nov 1, 2026", "22.00 GB", "2.00 GB", "24.00 GB"],
                ["Oct 31, 2026", "55.00 GB", "700.00 MB", "55.70 GB"],
                ["Oct 30, 2026", "18.00 GB", "4.40 GB", "22.40 GB"],
                ["Oct 29, 2026", "51.00 GB", "3.10 GB", "54.10 GB"]]


class Page(HTMLParser):
    """What templates/top.html shows: every table (its rows of cell texts, the pane it is in, the widths of its bars),
    the tabs and panes, and the text outside the tables."""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.tables, self.tabs, self.panes, self.text = [], {}, {}, []
        self.pane = None
        self.row = None
        self.cell = None
        self.depth = 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get("class") or "").split()
        if tag == "div":
            self.depth += 1
            if "tab-pane" in cls:
                self.panes[a["id"]] = {"cls": cls, "labelledby": a.get("aria-labelledby"), "role": a.get("role")}
                self.pane = (a["id"], self.depth)
            if "progress-bar" in cls:
                self.tables[-1]["bars"][-1].append((cls[1], a["style"]))
        elif tag == "a" and a.get("data-toggle") == "tab":
            self.tabs[a["id"]] = {"cls": cls, "href": a["href"], "controls": a.get("aria-controls"),
                                  "selected": a.get("aria-selected"), "role": a.get("role")}
        elif tag == "table":
            self.tables.append({"pane": self.pane and self.pane[0], "rows": [], "bars": [], "colspans": []})
        elif tag == "tr":
            if "bw-bar" in cls:
                self.tables[-1]["bars"].append([])
                self.row = None
            else:
                self.row = []
                self.tables[-1]["rows"].append(self.row)
        elif tag in ("td", "th"):
            self.cell = []
            if a.get("colspan"):
                self.tables[-1]["colspans"].append(a["colspan"])

    def handle_endtag(self, tag):
        if tag == "div":
            if self.pane and self.pane[1] == self.depth:
                self.pane = None
            self.depth -= 1
        elif tag in ("td", "th") and self.cell is not None:
            if self.row is not None:
                self.row.append("".join(self.cell).strip())
            self.cell = None
        elif tag == "tr":
            self.row = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)
        elif data.strip():
            self.text.append(data.strip())

    def body(self, pane=None):
        """The rows of a table, without its header; pane: the table of that pane, else the first one outside."""
        table = next(t for t in self.tables if t["pane"] == pane)
        return table["rows"][1:]

    def top(self):
        """The rows of the top days table: the last table, after the "Top Transfer Days" heading."""
        return self.tables[-1]["rows"][1:] if "Top Transfer Days" in self.text else None


def basic(user):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{user}".encode()).decode(),
            "X-Requested-With": "XMLHttpRequest"}


class VnstatTest(unittest.TestCase):
    def setUp(self):
        self.vnstat = FakeVnstat()
        self.path(self.vnstat.dir + os.pathsep + os.environ.get("PATH", ""))
        self.patch(mock.patch.object(swizzin, "get_default_interface", return_value="eth0"))
        self.client = swizzin.app.test_client()

    def path(self, value):
        self.patch(mock.patch.dict(os.environ, {"PATH": value}))

    def patch(self, patcher):
        patcher.start()
        self.addCleanup(patcher.stop)

    def config(self, **settings):
        self.patch(mock.patch.dict(swizzin.app.config, settings))

    def get(self, user="tester", status=200):
        r = self.client.get("/stats/vnstat", headers=basic(user) if user else {"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(r.status_code, status, r.get_data(as_text=True)[:500])
        return r.get_data(as_text=True)

    def page(self):
        return Page(self.get())


class Data(VnstatTest):
    def test_one_vnstat_call_for_everything(self):
        self.get()
        self.assertEqual(self.vnstat.calls(), [["-i", "eth0", "--json"]])

    def test_totals_and_top_days_as_before(self):
        """The same values as the previous page for the same vnstat data (TOTALS and TOP were read off it)."""
        page = self.page()
        self.assertEqual(page.body(), TOTALS)
        self.assertEqual(page.top(), TOP)

    def test_the_days_tab_lists_62_days_newest_first(self):
        days = self.page().body("bw-days")
        self.assertEqual(len(days), 62)
        self.assertEqual(days[:10], LAST_10_DAYS)  # the table of the last 10 days that the Days tab replaces
        self.assertEqual(days[-1][0], "Sep 7, 2026")  # 62 days, Sep 7 to Nov 7: the 8 older ones are left out

    def test_the_months_tab_lists_25_months_newest_first(self):
        months = self.page().body("bw-months")
        self.assertEqual(len(months), 25)
        self.assertEqual(months[:2], [["Nov 2026", "3.00 TB", "20.00 GB", "3.02 TB"],
                                      ["Oct 2026", "2.90 TB", "10.00 GB", "2.91 TB"]])
        self.assertEqual(months[-1], ["Nov 2024", "600.00 GB", "60.00 GB", "660.00 GB"])

    def test_each_bar_is_its_total_compared_with_the_largest(self):
        page = self.page()
        days = next(t for t in page.tables if t["pane"] == "bw-days")
        self.assertEqual(len(days["bars"]), 62)
        # Nov 7: 34 GB received and 4.8 GB sent; the largest total of the 62 days: Oct 3, 69 GB + 4.3 GB = 73.3 GB
        self.assertEqual(days["bars"][0], [("bg-info", "width: 46.38%"), ("bg-success", "width: 6.55%")])
        largest = days["rows"][1:].index(["Oct 3, 2026", "69.00 GB", "4.30 GB", "73.30 GB"])
        self.assertEqual(days["bars"][largest], [("bg-info", "width: 94.13%"), ("bg-success", "width: 5.87%")])
        # Nov 2026 is the largest month: 3 TB + 20 GB; Nov 2024: 600 GB + 60 GB
        months = next(t for t in page.tables if t["pane"] == "bw-months")
        self.assertEqual(months["bars"][0], [("bg-info", "width: 99.34%"), ("bg-success", "width: 0.66%")])
        self.assertEqual(months["bars"][-1], [("bg-info", "width: 19.87%"), ("bg-success", "width: 1.99%")])

    def test_two_tabs_days_open(self):
        page = self.page()
        self.assertEqual(page.tabs, {
            "bw-days-tab": {"cls": ["nav-link", "active"], "href": "#bw-days", "controls": "bw-days",
                            "selected": "true", "role": "tab"},
            "bw-months-tab": {"cls": ["nav-link"], "href": "#bw-months", "controls": "bw-months",
                              "selected": "false", "role": "tab"}})
        self.assertEqual(page.panes, {
            "bw-days": {"cls": ["tab-pane", "active", "bw-history"], "labelledby": "bw-days-tab", "role": "tabpanel"},
            "bw-months": {"cls": ["tab-pane", "bw-history"], "labelledby": "bw-months-tab", "role": "tabpanel"}})

    def test_without_totals_no_total_column_anywhere(self):
        self.config(NETWORK_INFO_TOTALS=False)
        page = self.page()
        for table in page.tables:
            self.assertEqual({len(row) for row in table["rows"]}, {3})
            self.assertEqual(set(table["colspans"]), {"3"} if table["pane"] else set())
        self.assertEqual(page.body("bw-days")[0], ["Nov 7, 2026", "34.00 GB", "4.80 GB"])

    def test_without_top_days_no_top_table(self):
        self.config(NETWORK_INFO_TOP=False)
        page = self.page()
        self.assertIsNone(page.top())
        self.assertEqual(len(page.tables), 3)

    def test_top_stats_setting(self):
        self.config(NETWORK_INFO_TOPSTATS=2)
        self.assertEqual(self.page().top(), TOP[:2])

    def test_a_cutoff_ranks_the_days_since_then(self):
        """As `vnstat -b <cutoff>` did: its top list then comes from the days since the cutoff, the largest first."""
        self.config(NETWORK_INFO_CUTOFF="2026-11-01")
        self.assertEqual(self.page().top(), [LAST_10_DAYS[1], LAST_10_DAYS[3], LAST_10_DAYS[5],
                                             LAST_10_DAYS[0], LAST_10_DAYS[2]])
        self.assertEqual(self.vnstat.calls(), [["-i", "eth0", "--json"]])

    def test_a_wrong_cutoff_is_ignored_and_logged(self):
        self.config(NETWORK_INFO_CUTOFF="1st of November")
        with self.assertLogs(swizzin.app.logger, "WARNING") as logs:
            self.assertEqual(self.page().top(), TOP)
        self.assertIn("NETWORK_INFO_CUTOFF '1st of November' is not a date", logs.output[0])

    def test_a_missing_period_shows_a_dash(self):
        """A single hour recorded yet: no last hour (the page used to fail with HTTP 500)."""
        data = traffic()
        data["hour"] = data["hour"][-1:]
        self.vnstat.say(vnstat_json(data))
        self.assertEqual(self.page().body()[:2], [TOTALS[0], ["Last Hour", "—", "—", "—"]])

    def test_a_list_without_entries_says_so(self):
        data = traffic()
        data["month"] = []
        self.vnstat.say(vnstat_json(data))
        page = self.page()
        self.assertEqual(page.body("bw-months"), [["No data yet"]])
        self.assertEqual(page.body()[3], ["This Month", "—", "—", "—"])
        self.assertEqual(len(page.body("bw-days")), 62)

    def test_dates_and_sizes_are_escaped(self):
        """Text from vnstat goes into the page as text (the panel never builds HTML from it)."""
        self.patch(mock.patch.object(swizzin, "get_default_interface", return_value="<b>eth0</b>"))
        self.vnstat.say(vnstat_json(EMPTY))
        html = self.get()
        self.assertIn("for &lt;b&gt;eth0&lt;/b&gt;.", html)
        self.assertNotIn("<b>", html)


class Unavailable(VnstatTest):
    """Whatever goes wrong with vnstat, the card says so in a sentence: never an HTTP 500."""

    def message(self):
        page = self.page()
        self.assertEqual(page.tables, [])
        self.assertEqual(page.tabs, {})
        return " ".join(page.text)

    def test_empty_database(self):
        """vnstat follows the interface but has saved nothing yet (just after it was installed)."""
        self.vnstat.say(vnstat_json(EMPTY))
        with self.assertNoLogs(swizzin.app.logger) if hasattr(self, "assertNoLogs") else nothing():
            self.assertEqual(self.message(), "No bandwidth data yet for eth0.")

    def test_interface_not_in_the_database(self):
        self.vnstat.say(stderr=NOT_IN_DATABASE, code=1)
        with self.assertLogs(swizzin.app.logger, "WARNING") as logs:
            self.assertEqual(self.message(), "No bandwidth data yet for eth0.")
        self.assertIn("Bandwidth of eth0: no-data: " + NOT_IN_DATABASE.strip(), logs.output[0])

    def test_vnstat_not_installed(self):
        self.path(tempfile.mkdtemp())  # a PATH where there is no vnstat at all
        self.assertEqual(self.message(), "No bandwidth data: vnstat is not installed.")
        self.assertEqual(self.vnstat.calls(), [])

    def test_invalid_json(self):
        self.vnstat.say(stdout='{"vnstatversion": "2.12", "jsonversion": "2", "interf')
        with self.assertLogs(swizzin.app.logger, "WARNING") as logs:
            self.assertEqual(self.message(), "No bandwidth data: the answer of vnstat could not be read.")
        self.assertIn("Bandwidth of eth0: unreadable: JSONDecodeError", logs.output[0])

    def test_vnstat_1(self):
        """JSON version 1 (vnstat 1.x, in KiB, other names): no supported system ships it."""
        self.vnstat.say(stdout=json.dumps({"vnstatversion": "1.18", "jsonversion": "1", "interfaces": [
            {"id": "eth0", "traffic": {"total": {"rx": 1, "tx": 1}, "days": [], "months": [], "tops": [],
                                       "hours": []}}]}))
        with self.assertLogs(swizzin.app.logger, "WARNING") as logs:
            self.assertEqual(self.message(), "No bandwidth data: the answer of vnstat could not be read.")
        self.assertIn("JSON version '1', not 2", logs.output[0])

    def test_unexpected_json(self):
        for case, odd in (("no interface", {"jsonversion": "2", "interfaces": []}),
                          ("a list", [1, 2]),
                          ("days not a list", {"jsonversion": "2", "interfaces": [
                              {"traffic": dict(traffic(), day="?")}]}),
                          ("a size not a number", {"jsonversion": "2", "interfaces": [
                              {"traffic": dict(traffic(), total={"rx": None, "tx": 1})}]})):
            with self.subTest(case):
                self.vnstat.say(stdout=json.dumps(odd))
                with self.assertLogs(swizzin.app.logger, "WARNING"):
                    self.assertEqual(self.message(), "No bandwidth data: the answer of vnstat could not be read.")

    def test_timeout(self):
        """A vnstat that does not answer is stopped: the page does not wait for it."""
        self.patch(mock.patch.object(util, "VNSTAT_TIMEOUT", 0.5))
        self.vnstat.say(vnstat_json(traffic()), sleep=30)
        start = time.monotonic()
        with self.assertLogs(swizzin.app.logger, "WARNING") as logs:
            self.assertEqual(self.message(), "No bandwidth data: vnstat did not answer in time.")
        self.assertLess(time.monotonic() - start, 5)
        self.assertIn("Bandwidth of eth0: timeout: no answer within 0.5 seconds", logs.output[0])
        with self.assertRaises(ProcessLookupError):  # stopped, not left running
            os.kill(self.vnstat.pid(), 0)

    def test_no_default_interface(self):
        self.patch(mock.patch.object(swizzin, "get_default_interface", return_value=None))
        self.assertEqual(self.message(), "No bandwidth data: the server has no default network interface.")
        self.assertEqual(self.vnstat.calls(), [])


class Access(VnstatTest):
    """The card is the admin's only (templates/netinfo.html): nobody else makes the panel run vnstat."""

    def test_another_user_is_refused_and_vnstat_not_run(self):
        self.get("friend", 403)
        self.assertEqual(self.vnstat.calls(), [])

    def test_not_logged_in(self):
        self.get(None, 401)
        self.assertEqual(self.vnstat.calls(), [])

    def test_the_admin(self):
        self.get("tester", 200)
        self.assertEqual(len(self.vnstat.calls()), 1)


class nothing:
    """assertNoLogs() appeared in Python 3.10: on older ones, the check is left out."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        os.remove(os.path.join(ROOT, "swizzin.cfg"))
