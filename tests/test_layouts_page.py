"""The layouts of the dashboard, Classic and Overview: the account menu's choice (route /layout) and the two pages.

Run from the repository root: python tests/test_layouts_page.py
(uses a throwaway htpasswd file, a throwaway file of choices and fake disks: nothing on the machine is touched)."""
import base64
import json
import os
import re
import shutil
import sys
import tempfile
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
from core import overview_blocks  # noqa: E402

TRAP = "/mnt/<img src=x onerror=alert(1)> it's \"mine\" & yours"
PAGES = [{"name": "rtorrent", "pretty_name": "rTorrent", "url": False, "systemd": "rtorrent@", "img": "rtorrent"},
         {"name": "autobrr", "pretty_name": "autobrr", "url": "https://demo.invalid/autobrr", "systemd": "autobrr@",
          "img": "autobrr"}]


def auth(user, xhr=True):
    headers = {"Authorization": "Basic " + base64.b64encode(f"{user}:{user}".encode()).decode()}
    if xhr:
        headers["X-Requested-With"] = "XMLHttpRequest"
    return headers


class Ids(HTMLParser):
    """The ids of a page, in its order."""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.ids = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id") is not None:
            self.ids.append(a["id"])


class LayoutTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        for patcher in (mock.patch.object(swizzin.layout_choices, "path", os.path.join(self.dir, "layouts.json")),
                        mock.patch.object(swizzin.layout_choices, "default", "classic"),
                        mock.patch.object(swizzin.limiter, "enabled", False)):  # Flask-Limiter caps anonymous requests
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = swizzin.app.test_client()

    def choices(self):
        """The file of choices, {} when there is none."""
        try:
            with open(swizzin.layout_choices.path) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def post(self, user, body=None, content_type="application/json", data=None, **kw):
        headers = auth(user) if user else {"X-Requested-With": "XMLHttpRequest"}
        if body is not None:
            data = json.dumps(body)
        return self.client.post("/layout", data=data, content_type=content_type, headers=headers, **kw)

    def assertInOrder(self, expected, found):
        """Each of `expected` is in `found`, in this order; others may come between them (the blocks of Overview to
        come, with their scripts)."""
        rest = iter(found)
        self.assertEqual([e for e in expected if e not in rest], [], found)

    def page(self, user, mounts=("/", "/mnt/data"), quota=False, shared=False, sbio=False, path="/"):
        locks = {"/install/.quota.lock": quota, "/install/.sbio.lock": sbio}
        real_isfile = os.path.isfile
        with mock.patch.object(swizzin, "get_mounts", return_value=list(mounts)), \
                mock.patch.object(swizzin, "generate_page_list", return_value=PAGES), \
                mock.patch.dict(swizzin.app.config, {"SHAREDSERVER": shared}), \
                mock.patch.object(swizzin.os.path, "isfile",
                                  side_effect=lambda p: locks[p] if p in locks else real_isfile(p)):
            r = self.client.get(path, headers=auth(user, xhr=False))
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True)


class Route(LayoutTest):
    """POST /layout: the choice of the account logged in, in JSON only."""

    def test_the_choice_is_kept_for_the_account_logged_in(self):
        r = self.post("friend", {"layout": "overview"})
        self.assertEqual((r.status_code, r.get_json()), (200, {"layout": "overview"}))
        self.assertEqual(self.choices(), {"friend": "overview"})
        self.assertIn('class="container-fluid overview"', self.page("friend"))
        self.assertIn('class="card-columns live-layout"', self.page("tester"))  # the others keep theirs
        self.assertEqual(self.post("friend", {"layout": "classic"}).status_code, 200)
        self.assertEqual(self.choices(), {"friend": "classic"})

    def test_never_for_an_account_named_in_the_request(self):
        r = self.post("friend", {"layout": "overview", "user": "tester"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.choices(), {"friend": "overview"})

    def test_not_logged_in_nothing_is_kept(self):
        for headers in ({"X-Requested-With": "XMLHttpRequest"}, {},
                        {"Authorization": "Basic " + base64.b64encode(b"tester:wrong").decode()}):
            with self.subTest(headers=headers):
                r = self.client.post("/layout", data='{"layout": "overview"}', content_type="application/json",
                                     headers=headers)
                self.assertIn(r.status_code, (302, 401))  # the login page, or 401 for a script
        self.assertEqual(self.choices(), {})

    def test_json_only(self):
        """A form of another site can send a form or text, never JSON: none of these is kept."""
        cases = [("application/x-www-form-urlencoded", "layout=overview"),
                 ("multipart/form-data; boundary=x", '--x\r\nContent-Disposition: form-data; name="layout"\r\n\r\n'
                                                     'overview\r\n--x--\r\n'),
                 ("text/plain", '{"layout": "overview"}'),
                 ("application/json", '{"layout": "over'),
                 ("application/json", '["overview"]'),
                 ("application/json", '"overview"'),
                 ("application/json", ""),
                 ("application/json", "{}")]
        for content_type, data in cases:
            with self.subTest(content_type=content_type, data=data):
                r = self.post("tester", content_type=content_type, data=data)
                self.assertEqual(r.status_code, 400)
        self.assertEqual(self.choices(), {})

    def test_only_a_layout_of_this_version(self):
        for value in ("fancy", "Overview", "", None, 1, ["overview"], {"overview": True}):
            with self.subTest(value):
                r = self.post("tester", {"layout": value})
                self.assertEqual((r.status_code, r.get_json()), (400, {"error": "unknown layout"}))
        self.assertEqual(self.choices(), {})

    def test_post_only(self):
        self.assertEqual(self.client.get("/layout", headers=auth("tester")).status_code, 405)

    def test_a_choice_that_cannot_be_written_is_503_and_the_page_shows(self):
        missing = os.path.join(self.dir, "missing", "layouts.json")  # a folder that does not exist: root too
        with mock.patch.object(swizzin.layout_choices, "path", missing):
            with self.assertLogs(swizzin.app.logger, "WARNING") as logs:
                r = self.post("tester", {"layout": "overview"})
            self.assertEqual((r.status_code, r.get_json()), (503, {"error": "not saved"}))
            self.assertIn("could not be saved", logs.output[0])
            self.assertIn('class="card-columns live-layout"', self.page("tester"))

    def test_the_rate_limit_spares_the_accounts_logged_in(self):
        """RATELIMIT_DEFAULT (5 per minute) applies to requests without a login, as on every route; the browser
        sends its login with each request, so choosing is never limited."""
        with mock.patch.object(swizzin.limiter, "enabled", True):
            address = {"REMOTE_ADDR": "203.0.113.7"}  # documentation address (RFC 5737), for this test only
            codes = [self.post("tester", {"layout": "overview"}, environ_base=address).status_code for _ in range(8)]
            self.assertEqual(codes, [200] * 8)
            codes = [self.post(None, {"layout": "overview"}, environ_base=address).status_code for _ in range(6)]
            self.assertEqual(codes, [401] * 5 + [429])


class Pages(LayoutTest):
    def overview(self, user, **kw):
        swizzin.layout_choices.set(user, "overview")
        return self.page(user, **kw)

    def test_classic_by_default(self):
        page = self.page("tester")
        self.assertIn('class="card-columns live-layout"', page)
        for overview in ("overview.css", "overview.js", "ov-tiles", "ov-section", "tile-cpu", "cpu-spark"):
            self.assertNotIn(overview, page)
        for name in overview_blocks.scripts() + overview_blocks.stylesheets():
            self.assertNotIn(name, page)

    def test_the_default_of_swizzin_cfg(self):
        with mock.patch.object(swizzin.layout_choices, "default", "overview"):
            self.assertIn('class="container-fluid overview"', self.page("tester"))
            swizzin.layout_choices.set("tester", "classic")
            self.assertIn('class="card-columns live-layout"', self.page("tester"))

    def test_a_broken_file_of_choices_still_shows_the_dashboard(self):
        for data in ('{"tester": "over', "[" * 100000, '{"tester": "fancy"}'):
            with self.subTest(data[:20]):
                with open(swizzin.layout_choices.path, "w") as f:
                    f.write(data)
                self.assertIn('class="card-columns live-layout"', self.page("tester"))

    def test_the_menu_ticks_the_layout_shown(self):
        for layout in ("classic", "overview"):
            with self.subTest(layout):
                swizzin.layout_choices.set("tester", layout)
                buttons = re.findall(r'<button type="button" class="dropdown-item layout-choice([^"]*)" '
                                     r'data-layout="(\w+)" data-url="/layout"( aria-current="true")?>'
                                     r'<span class="layout-tick"[^>]*>(✓?)</span>(\w+)</button>', self.page("tester"))
                self.assertEqual(buttons, [(" active" if name == layout else "", name,
                                            ' aria-current="true"' if name == layout else "", "✓" if name == layout
                                            else "", label) for name, label in (("classic", "Classic"),
                                                                                ("overview", "Overview"))])

    def test_the_menu_is_in_every_navbar_of_the_dashboard(self):
        for kw in ({}, {"shared": True}, {"shared": True, "sbio": True}):
            for user in ("tester", "friend"):
                with self.subTest(user=user, **kw):
                    page = self.page(user, **kw)
                    self.assertEqual(page.count('id="layout-menu-title"'), 1)
                    self.assertEqual(page.count('id="layout-error" role="alert" hidden'), 1)

    def test_no_menu_on_the_other_pages(self):
        """The Stats page and the App Store page do not show the dashboard: no layout to choose there."""
        swizzin.layout_choices.set("tester", "overview")
        for path in ("/stats", "/apps"):
            with self.subTest(path), mock.patch("core.util.generate_page_list", return_value=PAGES):
                self.assertNotIn("layout-choice", self.page("tester", path=path))

    def test_the_overview_of_the_admin(self):
        page = self.overview("tester")
        ids = Ids(page).ids
        for element in ("tile-cpu", "tile-ram", "tile-disk", "tile-net", "cpu-spark", "ram-spark", "disk-spark",
                        "net-spark", "load1m", "ov-network", "net-graph", "net-graph-rx-toggle", "ov-hour-rx",
                        "ov-alltime-tx", "ov-periods-note", "top10", "current_rx", "disk-card", "disk-alert",
                        "status_rtorrent", "enabled_autobrr", "uptime"):
            self.assertIn(element, ids)
        # The bandwidth graph at the top, not in the Network Info card of the details.
        self.assertLess(ids.index("net-graph"), ids.index("ov-details"))
        self.assertGreater(ids.index("current_rx"), ids.index("ov-details"))
        self.assertInOrder(["cpu", "ram", "disks", "network"], re.findall(r'data-section="([\w-]+)"', page))
        self.assertIn('<details class="card ov-section" data-section="cpu"><summary class="card-header">CPU Info'
                      '</summary>', page)
        # Open as the list says: the sections open by default, and only them.
        sections = re.findall(r'<details class="card ov-section" data-section="([\w-]+)"( open)?>', page)
        opened = {block["id"] for block in overview_blocks.BLOCKS if block.get("open")}
        self.assertTrue(opened)
        self.assertEqual([i for i, open_ in sections if open_], [i for i, _ in sections if i in opened])
        for name in overview_blocks.stylesheets() + overview_blocks.scripts():  # the layout's own, and its blocks'
            self.assertEqual(page.count('="/static/%s"' % name), 1, name)
        files = re.findall(r'(?:src|href)="/static/((?:css|js)/[\w-]+\.(?:css|js))"', page)
        self.assertInOrder(["css/overview.css", "js/swizzin.js", "js/live_graphs.js", "js/live_stats.js",
                            "js/overview.js"], files)

    def test_the_details_in_two_columns_then_across(self):
        """Two columns on a wide screen (static/css/overview.css), each stacking on its own: the machine on the left, the
        disks on the right. In one column, on a narrow screen, the same order as Classic's cards. Then the sections
        across the whole width of Details (Top processes, Network Info). A column whose blocks are all hidden by
        default (the machine's: they repeat the tiles) is hidden, and the other one takes the width."""
        page = self.overview("tester", quota=True)
        details = page[page.index('id="ov-details"'):]
        self.assertIn('<h2 class="ov-title" id="ov-details">Details</h2>\n      <span class="ov-hint">More about the '
                      'server</span>', page)
        found = re.findall(r'class="(live-stack|ov-column|ov-wide)"( hidden)?|data-section="([\w-]+)"', details)
        self.assertInOrder(["live-stack", "ov-column", "cpu", "ram", "ov-column", "disks", "quota", "ov-wide",
                            "processes", "network"], [a or c for a, _, c in found])
        wide = [b["id"] for b in overview_blocks.zone("wide")]
        self.assertEqual([c for a, _, c in found][[a for a, _, c in found].index("ov-wide"):][1:], wide)
        boxes = [f for f in found if f[0] in ("ov-column", "ov-wide")]
        hidden = {a + str(n): bool(h) for n, (a, h, _) in enumerate(boxes)}
        everything_hidden = all(b.get("hidden") for b in overview_blocks.zone("left"))
        self.assertEqual(hidden, {"ov-column0": everything_hidden, "ov-column1": False, "ov-wide2": False})

    def test_the_page_follows_the_list_of_blocks(self):
        """core/overview_blocks.py makes the page: a block moved in the list, or to another zone, moves on the page;
        the script and the stylesheet of a block are loaded after the layout's own, on Overview only."""
        blocks = {block["id"]: dict(block) for block in overview_blocks.BLOCKS}
        blocks["quota"]["zone"] = "left"
        blocks["network"].update(zone="right", script="js/network-block.js", stylesheet="css/network-block.css")
        order = ["tile-ram", "tile-cpu", "tile-net", "tile-disk", "services", "bandwidth", "ram", "quota", "cpu",
                 "network", "disks"]
        with mock.patch.object(overview_blocks, "BLOCKS", [blocks[i] for i in order]):
            page = self.overview("tester", quota=True)
            found = re.findall(r'id="(tile-\w+|ov-network)"|class="card-header">(Service Info)<|class="(ov-column)"|'
                               r'data-section="([\w-]+)"', page)
            self.assertEqual([next(x for x in f if x) for f in found],
                             ["tile-ram", "tile-cpu", "tile-net", "tile-disk", "Service Info", "ov-network",
                              "ov-column", "ram", "quota", "cpu", "ov-column", "network", "disks"])
            self.assertIn('<div class="ov-wide" hidden>', page)  # nothing left across the whole width
            files = re.findall(r'(?:src|href)="/static/((?:css|js)/[\w-]+\.(?:css|js))"', page)
            self.assertInOrder(["css/live_stats.css", "css/overview.css", "css/network-block.css", "js/live_stats.js",
                                "js/overview.js", "js/network-block.js"], files)
            swizzin.layout_choices.set("tester", "classic")
            self.assertNotIn("network-block", self.page("tester", quota=True))

    def test_the_overview_of_another_account(self):
        """As in Classic: no bandwidth for them (graph, speed table, totals), the Disk Info card without a quota."""
        ids = Ids(self.overview("friend")).ids
        for element in ("tile-cpu", "tile-ram", "tile-disk", "disk-card", "disk-alert", "uptime"):
            self.assertIn(element, ids)
        for element in ("tile-net", "net-spark", "ov-network", "net-graph", "ov-hour-rx", "top10", "current_rx"):
            self.assertNotIn(element, ids)

    def test_with_a_quota(self):
        """Disk Info, its tile and the banner: without a quota, or the admin; Disk Quota Info when it is installed."""
        for user, disks in (("tester", True), ("friend", False)):
            with self.subTest(user):
                page = self.overview(user, quota=True)
                ids = Ids(page).ids
                for element in ("tile-disk", "disk-spark", "disk-card", "disk-alert", "disk-alert-summary"):
                    self.assertEqual(element in ids, disks, element)
                self.assertIn("quotafree", ids)
                self.assertIn('data-section="quota"', page)

    def test_on_a_shared_server(self):
        """Network Info for every account (their bandwidth quota); the speeds for the admin only."""
        for user, admin in (("tester", True), ("friend", False)):
            with self.subTest(user):
                page = self.overview(user, shared=True)
                ids = Ids(page).ids
                self.assertIn('data-section="network"', page)
                self.assertIn("netused", ids)
                for element in ("tile-net", "net-graph", "current_rx", "top10"):
                    self.assertEqual(element in ids, admin, element)

    def test_the_disk_tile_follows_the_root(self):
        self.assertNotIn('id="tile-disk"', self.overview("tester", mounts=("/srv", "/mnt/data")))

    def test_every_element_of_classic_is_in_the_overview(self):
        """Same scripts, same element ids: everything Classic's scripts fill is there, once."""
        for kw in ({}, {"quota": True}, {"shared": True}):
            for user in ("tester", "friend"):
                with self.subTest(user=user, **kw):
                    swizzin.layout_choices.set(user, "classic")
                    classic = Ids(self.page(user, **kw))
                    page = self.overview(user, **kw)
                    overview = Ids(page)
                    self.assertEqual(set(classic.ids) - set(overview.ids), set())
                    twice = {i for i in overview.ids if overview.ids.count(i) > 1}
                    self.assertEqual(twice, {i for i in classic.ids if classic.ids.count(i) > 1})  # the navbar's own
                    # static/js/live_stats.js opens the websocket from the element of this class
                    self.assertEqual(page.count('class="live-stack" data-urlbase=""'), 1)

    def test_a_trapped_mount_path_is_escaped(self):
        page = self.overview("tester", mounts=("/", TRAP))
        self.assertNotIn("<img src=x", page)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", page)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        os.remove(os.path.join(ROOT, "swizzin.cfg"))
