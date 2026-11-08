"""Customize, on the Overview layout: the route that keeps an account's arrangement (POST /layout/arrange) and the page
the panel renders with it (templates/overview.html). The editor itself is tested in tests/test_arrange_js.py, the file
of choices in tests/test_layouts.py.

Run from the repository root: python tests/test_arrange_page.py
(uses a throwaway htpasswd file, a throwaway file of choices and fake disks: nothing on the machine is touched)."""
import base64
import json
import os
import re
import sys
import unittest
from unittest import mock

import jinja2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# The panel set up by the tests of the layouts: a throwaway htpasswd file (the admin "tester", and "friend") and
# swizzin.cfg, removed at the end.
from test_layouts_page import ROOT, Ids, LayoutTest, auth, swizzin  # noqa: E402
from core import overview_blocks  # noqa: E402
from core.middleware import PrefixMiddleware  # noqa: E402

ARRANGED = {"order": ["tile-ram", "tile-cpu", "tile-net", "tile-disk", "services", "bandwidth", "disks", "cpu", "ram",
                      "quota", "network"],
            "hidden": ["ram", "tile-disk"], "other_column": ["disks"]}
ADMIN_ONLY = ["system-status", "tile-net", "bandwidth"]
EXTRA = {"id": "extra", "zone": "right", "template": "overview/extra.html", "label": "Extra <b>block</b>"}
EXTRA_HTML = ('<details class="card ov-section" data-section="extra"><summary class="card-header">Extra</summary>'
              '<p id="extra-text">extra</p></details>')


def kept(user):
    """(layout, arrangement) kept for `user`, as the dashboard reads them."""
    return swizzin.layout_choices.get(user), swizzin.layout_choices.arrangement(user)


def editor(page):
    """What the Customize button gives the editor (static/js/arrange.js): its data-blocks, as the browser reads it."""
    m = re.search(r"<button type=\"button\" class=\"btn btn-sm btn-secondary ov-customize\" id=\"ov-customize\" "
                  r"data-url=\"([^\"]*)\" data-logout=\"([^\"]*)\" data-blocks='([^']*)' hidden>Customize</button>",
                  page)
    return m and dict(json.loads(m.group(3)), url=m.group(1), logout=m.group(2))


def places(page):
    """The blocks of the page in its order: tiles and sections by id, the bandwidth card, Service Info, the columns
    (with " hidden" when hidden), and the elements that hide a block ("hidden:<id>")."""
    found = re.findall(r'id="(tile-[a-z]+|ov-status)"|<section class="card (ov-network)"|class="card-header">'
                       r'(Service Info)<|<div class="(ov-column)"( hidden)?|data-section="([\w-]+)"|'
                       r'<div class="ov-hidden-block" data-block="([\w-]+)" hidden>', page)
    out = []
    for tile, network, services, column, column_hidden, section, hidden in found:
        out.append(tile or network or services or section or (column + column_hidden) or "hidden:" + hidden)
    return out


def details(page):
    """places() of Details only: its two columns and their sections."""
    found = places(page)
    first = next(i for i, x in enumerate(found) if x.startswith("ov-column"))
    return found[first:]


class ArrangeTest(LayoutTest):
    def arrange(self, user, body=None, content_type="application/json", data=None, path="/layout/arrange", **kw):
        headers = auth(user) if user else {"X-Requested-With": "XMLHttpRequest"}
        if body is not None:
            data = json.dumps(body)
        return self.client.post(path, data=data, content_type=content_type, headers=headers, **kw)

    def overview(self, user, arrangement=None, **kw):
        swizzin.layout_choices.set(user, "overview")
        if arrangement is not None:
            r = self.arrange(user, arrangement)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return self.page(user, **kw)


class Route(ArrangeTest):
    """POST /layout/arrange: the arrangement of the account logged in, in JSON only."""

    def test_kept_for_the_account_logged_in(self):
        swizzin.layout_choices.set("tester", "classic")
        r = self.arrange("friend", ARRANGED)
        self.assertEqual((r.status_code, r.get_json()), (200, {"arrangement": ARRANGED}))
        self.assertEqual(self.choices(), {"tester": "classic", "friend": {"overview": ARRANGED}})
        self.assertEqual(kept("friend"), ("classic", ARRANGED))

    def test_never_for_an_account_named_in_the_request(self):
        r = self.arrange("friend", dict(ARRANGED, user="tester"))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.choices(), {})

    def test_not_logged_in_nothing_is_kept(self):
        for headers in ({"X-Requested-With": "XMLHttpRequest"}, {},
                        {"Authorization": "Basic " + base64.b64encode(b"tester:wrong").decode()}):
            with self.subTest(headers=headers):
                r = self.client.post("/layout/arrange", data=json.dumps(ARRANGED), content_type="application/json",
                                     headers=headers)
                self.assertIn(r.status_code, (302, 401))
        self.assertEqual(self.choices(), {})

    def test_json_only(self):
        """A form of another site can send a form or text, never JSON: none of these is kept."""
        cases = [("application/x-www-form-urlencoded", "order=cpu&hidden=ram"),
                 ("multipart/form-data; boundary=x", '--x\r\nContent-Disposition: form-data; name="hidden"\r\n\r\n'
                                                     'ram\r\n--x--\r\n'),
                 ("text/plain", json.dumps(ARRANGED)),
                 ("application/json", '{"order": ["cpu"'),
                 ("application/json", '["cpu"]'),
                 ("application/json", '"reset"'),
                 ("application/json", ""),
                 ("application/json", "{}")]
        for content_type, data in cases:
            with self.subTest(content_type=content_type, data=data):
                r = self.arrange("tester", content_type=content_type, data=data)
                self.assertEqual(r.status_code, 400)
        self.assertEqual(self.choices(), {})

    def test_what_is_not_an_arrangement_is_400(self):
        too_long = {"hidden": ["cpu"] * 65}
        for body in ({"order": ["future-block"]}, {"order": ["cpu", "cpu"]}, {"hidden": "ram"}, too_long,
                     {"other_column": ["tile-cpu"]}, {"order": [3]}, {"reset": False}, {"reset": 1},
                     {"reset": "true"}, {"reset": True, "order": []}, {"order": [], "colour": "teal"}):
            with self.subTest(body=body):
                r = self.arrange("tester", body)
                self.assertEqual(r.status_code, 400)
                self.assertIsInstance(r.get_json()["error"], str)
        self.assertEqual(self.choices(), {})

    def test_reset_and_the_default_arrangement(self):
        """Both give the account its layout's name back; where there is nothing to remove, nothing is written."""
        swizzin.layout_choices.set("tester", "overview")
        self.arrange("tester", ARRANGED)
        r = self.arrange("tester", {"reset": True})
        self.assertEqual((r.status_code, r.get_json()), (200, {"arrangement": None}))
        self.assertEqual(self.choices(), {"tester": "overview"})
        self.arrange("tester", ARRANGED)
        r = self.arrange("tester", {"order": [block["id"] for block in overview_blocks.BLOCKS]})
        self.assertEqual((r.status_code, r.get_json()), (200, {"arrangement": None}))
        self.assertEqual(self.choices(), {"tester": "overview"})
        with mock.patch.object(swizzin.layout_choices, "write", side_effect=AssertionError("written")):
            self.assertEqual(self.arrange("tester", {"reset": True}).status_code, 200)
            self.assertEqual(self.arrange("friend", {"reset": True}).status_code, 200)

    def test_an_arrangement_that_cannot_be_written_is_503_and_the_page_shows(self):
        swizzin.layout_choices.set("tester", "overview")
        missing = os.path.join(self.dir, "missing", "layouts.json")
        with mock.patch.object(swizzin.layout_choices, "path", missing):
            with self.assertLogs(swizzin.app.logger, "WARNING") as logs:
                r = self.arrange("tester", ARRANGED)
            self.assertEqual((r.status_code, r.get_json()), (503, {"error": "not saved"}))
            self.assertIn("could not be saved", logs.output[0])
        self.assertEqual(places(self.page("tester"))[:3], ["ov-status", "tile-cpu", "tile-ram"])  # as it was

    def test_post_only(self):
        self.assertEqual(self.client.get("/layout/arrange", headers=auth("tester")).status_code, 405)

    def test_under_urlbase(self):
        """The page gives the script the route's address (url_for): under URLBASE too."""
        middleware = swizzin.app.wsgi_app
        while not isinstance(middleware, PrefixMiddleware):
            middleware = middleware.wsgi_app
        with mock.patch.object(middleware, "prefix", "/panel"), \
                mock.patch.dict(swizzin.app.config, {"URLBASE": "/panel"}):
            swizzin.layout_choices.set("tester", "overview")
            page = self.page("tester", path="/panel/")
            self.assertEqual((editor(page)["url"], editor(page)["logout"]), ("/panel/layout/arrange", "/panel/logout"))
            r = self.arrange("tester", ARRANGED, path="/panel/layout/arrange")
            self.assertEqual(r.status_code, 200)
        self.assertEqual(kept("tester"), ("overview", ARRANGED))

    def test_the_rate_limit_spares_the_accounts_logged_in(self):
        with mock.patch.object(swizzin.limiter, "enabled", True):
            address = {"REMOTE_ADDR": "203.0.113.8"}  # documentation address (RFC 5737), for this test only
            codes = [self.arrange("tester", ARRANGED, environ_base=address).status_code for _ in range(8)]
            self.assertEqual(codes, [200] * 8)


class Page(ArrangeTest):
    """The page of an account that arranged Overview: in its order, rendered by the panel."""

    def test_in_the_order_of_the_account(self):
        page = self.overview("tester", ARRANGED, quota=True)
        self.assertEqual(places(page),
                         ["ov-status", "tile-ram", "tile-cpu", "tile-net", "hidden:tile-disk", "tile-disk",
                          "Service Info", "ov-network", "ov-column", "disks", "cpu", "hidden:ram", "ram", "ov-column",
                          "quota", "network"])

    def test_a_hidden_block_stays_in_the_page(self):
        """In an element with the hidden attribute: the scripts that fill it or read it keep working, and nothing more
        or less is sent to the account."""
        before = Ids(self.overview("tester", quota=True)).ids
        page = self.overview("tester", {"hidden": ["ram", "tile-disk", "network", "bandwidth", "system-status"]},
                             quota=True)
        self.assertEqual(sorted(Ids(page).ids), sorted(before))
        for block in ("ram", "tile-disk", "network", "bandwidth", "system-status"):
            self.assertEqual(page.count('<div class="ov-hidden-block" data-block="%s" hidden>' % block), 1, block)
        self.assertIn('<div class="ov-hidden-block" data-block="ram" hidden><details class="card ov-section" '
                      'data-section="ram">', page.replace("\n", "").replace("  ", ""))
        self.assertEqual(page.count("ov-hidden-block"), 5)

    def test_a_column_with_nothing_to_show_is_hidden(self):
        """The other one then takes the whole width of Details; its hidden sections stay in the page."""
        page = self.overview("tester", {"other_column": ["cpu", "ram"]})  # not in an order: that of the list
        self.assertEqual(details(page), ["ov-column hidden", "ov-column", "cpu", "ram", "disks", "network"])
        page = self.overview("tester", {"hidden": ["disks", "network"]})
        self.assertEqual(details(page), ["ov-column", "cpu", "ram", "ov-column hidden", "hidden:disks", "disks",
                                         "hidden:network", "network"])
        self.assertEqual(details(self.overview("tester", {"reset": True})),
                         ["ov-column", "cpu", "ram", "ov-column", "disks", "network"])

    def test_the_blocks_of_the_admin_never_for_another_account(self):
        """Whatever its arrangement says: listed first, hidden, or in another column, they are not rendered at all,
        not even hidden, and the editor does not list them."""
        friend = {"order": ADMIN_ONLY + ["network", "tile-ram"], "hidden": ADMIN_ONLY + ["network"],
                  "other_column": ["network"]}
        page = self.overview("friend", friend)
        ids = Ids(page).ids
        for element in ("ov-status", "tile-net", "net-spark", "ov-network", "net-graph", "ov-hour-rx", "top10",
                        "current_rx"):
            self.assertNotIn(element, ids)
        self.assertNotIn('data-section="network"', page)
        self.assertNotIn("ov-hidden-block", page)
        self.assertEqual(places(page), ["tile-ram", "tile-cpu", "tile-disk", "Service Info", "ov-column", "cpu",
                                        "ram", "ov-column", "disks"])
        self.assertEqual([b["id"] for b in editor(page)["blocks"]],
                         ["tile-ram", "tile-cpu", "tile-disk", "services", "cpu", "ram", "disks"])
        self.assertEqual(editor(page)["order"], ["tile-cpu", "tile-ram", "tile-disk", "services", "cpu", "ram",
                                                 "disks"])

    def test_the_editor_lists_the_blocks_of_the_page(self):
        page = self.overview("tester", quota=True)
        data = editor(page)
        shown = ["system-status", "tile-cpu", "tile-ram", "tile-disk", "tile-net", "bandwidth", "services", "cpu",
                 "ram", "disks", "quota", "network"]
        labels = {block["id"]: block["label"] for block in overview_blocks.BLOCKS}
        homes = {block["id"]: block["zone"] for block in overview_blocks.BLOCKS}
        self.assertEqual(data["blocks"], [{"id": i, "label": labels[i], "zone": homes[i], "home": homes[i],
                                           "hidden": False} for i in shown])
        self.assertEqual(data["order"], [block["id"] for block in overview_blocks.BLOCKS])
        self.assertEqual(data["zones"], list(overview_blocks.ZONES))
        self.assertEqual((data["url"], data["logout"]), ("/layout/arrange", "/logout"))
        data = editor(self.overview("tester", ARRANGED, quota=True))
        self.assertEqual([(b["id"], b["zone"], b["hidden"]) for b in data["blocks"]][5:],
                         [("services", "row", False), ("bandwidth", "row", False), ("disks", "left", False),
                          ("cpu", "left", False), ("ram", "left", True), ("quota", "right", False),
                          ("network", "right", False)])
        self.assertEqual([b["home"] for b in data["blocks"]][7], "right")  # disks, at home on the right

    def test_the_button_and_its_script_on_overview_only(self):
        """Hidden until static/js/arrange.js shows it: without JavaScript, the page has no button that does nothing."""
        page = self.overview("tester")
        self.assertEqual(page.count('id="ov-customize"'), 1)
        self.assertIsNotNone(editor(page))
        self.assertEqual(page.count('<script src="/static/js/arrange.js"></script>'), 1)
        self.assertLess(page.index("js/overview.js"), page.index("js/arrange.js"))
        swizzin.layout_choices.set("tester", "classic")
        page = self.page("tester")
        for name in ("ov-customize", "arrange.js", "ov-hidden-block", "data-blocks"):
            self.assertNotIn(name, page)

    def test_classic_is_the_same_with_an_arrangement(self):
        swizzin.layout_choices.set("tester", "classic")
        before = self.page("tester", quota=True)
        self.arrange("tester", ARRANGED)
        self.assertEqual(kept("tester"), ("classic", ARRANGED))
        self.assertEqual(self.page("tester", quota=True), before)

    def test_after_a_reset_the_page_of_before(self):
        before = self.overview("tester", quota=True)
        self.assertNotEqual(self.overview("tester", ARRANGED, quota=True), before)
        self.assertEqual(self.overview("tester", {"reset": True}, quota=True), before)

    def test_a_block_added_to_the_list_can_be_arranged(self):
        """A new block is one more line in core/overview_blocks.py and its template: the editor lists it, and the page
        puts it where the account says, hidden or not, with nothing else to write."""
        loader = swizzin.app.jinja_env.loader
        with mock.patch.object(overview_blocks, "BLOCKS", overview_blocks.BLOCKS + [EXTRA]), \
                mock.patch.object(swizzin.app.jinja_env, "loader",
                                  jinja2.ChoiceLoader([jinja2.DictLoader({EXTRA["template"]: EXTRA_HTML}), loader])):
            page = self.overview("tester")
            self.assertEqual(details(page), ["ov-column", "cpu", "ram", "ov-column", "disks", "network", "extra"])
            self.assertEqual(editor(page)["blocks"][-1],
                             {"id": "extra", "label": "Extra <b>block</b>", "zone": "right", "home": "right",
                              "hidden": False})
            self.assertNotIn("<b>block</b>", page)  # escaped in the attribute; the editor writes it as text
            page = self.overview("tester", {"order": ["extra", "cpu"], "hidden": ["extra"], "other_column": ["extra"]})
            self.assertEqual(details(page), ["ov-column", "hidden:extra", "extra", "cpu", "ram", "ov-column", "disks",
                                             "network"])
            self.assertIn("extra-text", Ids(page).ids)
        swizzin.app.jinja_env.cache.clear()  # the page without it again
        page = self.page("tester")  # once the block is gone, its id is left out
        self.assertNotIn('data-section="extra"', page)
        self.assertNotIn("extra-text", page)
        self.assertEqual(details(page), ["ov-column", "cpu", "ram", "ov-column", "disks", "network"])


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        os.remove(os.path.join(ROOT, "swizzin.cfg"))
