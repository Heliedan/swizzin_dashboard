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
from core import layouts, overview_blocks  # noqa: E402
from core.middleware import PrefixMiddleware  # noqa: E402

# An arrangement as the panel keeps it (core/layouts.py): RAM Info, hidden by default, shown again.
ARRANGED = {"order": ["tile-ram", "tile-cpu", "tile-net", "tile-disk", "services", "bandwidth", "disks", "cpu", "ram",
                      "quota", "network"],
            "hidden": ["tile-disk"], "shown": ["ram"], "other_column": ["disks"]}
BY_DEFAULT = [block["id"] for block in overview_blocks.BLOCKS if block.get("hidden")]  # hidden by default
WIDE = [block["id"] for block in overview_blocks.zone("wide")]  # across the whole width of Details
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
    and the whole width of Details (with " hidden" when hidden), and the elements that hide a block ("hidden:<id>")."""
    found = re.findall(r'id="(tile-[a-z]+|ov-status)"|<section class="card (ov-network)"|class="card-header">'
                       r'(Service Info)<|<div class="(ov-column|ov-wide)"( hidden)?|data-section="([\w-]+)"|'
                       r'<div class="ov-hidden-block" data-block="([\w-]+)" hidden>', page)
    out = []
    for tile, network, services, column, column_hidden, section, hidden in found:
        out.append(tile or network or services or section or (column + column_hidden) or "hidden:" + hidden)
    return out


def layout(page):
    """The page by zone, as the panel rendered it: the ids of the blocks of each zone in their order (the sections by
    their data-section, the tiles by their id, the pills and the two cards of the row by what they are); "hidden", the
    blocks in an element with the hidden attribute, in the order of the page; "hidden columns", the columns of Details
    hidden, and "wide" when its whole width is."""
    out = {zone: [] for zone in overview_blocks.ZONES}
    out.update({"hidden": [], "hidden columns": []})
    columns, zone = iter(("left", "right", "wide")), None
    for item in places(page):
        if item.startswith(("ov-column", "ov-wide")):
            zone = next(columns)
            if item.endswith(" hidden"):
                out["hidden columns"].append(zone)
        elif item.startswith("hidden:"):
            out["hidden"].append(item[len("hidden:"):])
        elif zone:
            out[zone].append(item)
        elif item == "ov-status":
            out["glance"].append("system-status")
        elif item.startswith("tile-"):
            out["tiles"].append(item)
        else:
            out["row"].append({"ov-network": "bandwidth", "Service Info": "services"}[item])
    return out


def seen(page):
    """The blocks that layout() finds on a page."""
    return {i for zone in overview_blocks.ZONES for i in layout(page)[zone]}


def hidden_on(page_order, arrangement=None):
    """Those of `page_order` that the page hides with `arrangement`: hidden by it, or hidden by default and not shown by
    it."""
    arrangement = arrangement or {}
    return [i for i in page_order if i in arrangement.get("hidden", []) or
            (i in BY_DEFAULT and i not in arrangement.get("shown", []))]


def unlisted(zone, arrangement, shown):
    """The blocks of `zone` that `arrangement` does not list, among `shown`, in the order of core/overview_blocks.py:
    the page puts them after those it lists (the blocks added to the list since it was made, say)."""
    other = arrangement.get("other_column", [])
    return [b["id"] for b in overview_blocks.BLOCKS if b["id"] in shown and b["id"] not in arrangement.get("order", [])
            and (layouts.OTHER_COLUMN.get(b["zone"], b["zone"]) if b["id"] in other else b["zone"]) == zone]


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
        """The blocks it lists first, in its order, hidden or in the other column as it says; then, in each zone, those
        it does not list, in the order of core/overview_blocks.py (Top processes and Panel sign-ins, added since). A
        block hidden by default stays hidden unless it shows it (RAM Info shown, CPU Info not)."""
        shown = seen(self.overview("tester", quota=True))
        found = layout(self.overview("tester", ARRANGED, quota=True))
        listed = {"glance": [], "tiles": ["tile-ram", "tile-cpu", "tile-net", "tile-disk"],
                  "row": ["services", "bandwidth"], "left": ["disks", "cpu", "ram"], "right": ["quota"],
                  "wide": ["network"]}
        for zone, ids in listed.items():
            with self.subTest(zone):
                self.assertEqual(found[zone], ids + unlisted(zone, ARRANGED, shown))
        order = [i for zone in overview_blocks.ZONES for i in found[zone]]
        self.assertEqual((found["hidden"], found["hidden columns"]), (hidden_on(order, ARRANGED), []))
        self.assertIn("tile-disk", found["hidden"])
        self.assertNotIn("ram", found["hidden"])
        self.assertIn("processes", found["wide"][1:])
        self.assertIn("signins", found["right"][1:])

    def test_a_hidden_block_stays_in_the_page(self):
        """In an element with the hidden attribute: the scripts that fill it or read it keep working, and nothing more
        or less is sent to the account."""
        before = Ids(self.overview("tester", quota=True)).ids
        hiding = ["disks", "tile-disk", "network", "bandwidth", "system-status"]
        page = self.overview("tester", {"hidden": hiding}, quota=True)
        self.assertEqual(sorted(Ids(page).ids), sorted(before))
        everything = hiding + [i for i in BY_DEFAULT if i not in hiding]  # hidden by the account, or by default
        for block in everything:
            self.assertEqual(page.count('<div class="ov-hidden-block" data-block="%s" hidden>' % block), 1, block)
        self.assertIn('<div class="ov-hidden-block" data-block="disks" hidden><details class="card ov-section" '
                      'data-section="disks">', page.replace("\n", "").replace("  ", ""))
        self.assertEqual(page.count("ov-hidden-block"), len(everything))

    def test_a_column_with_nothing_to_show_is_hidden(self):
        """The other one then takes the whole width of Details; its hidden sections stay in the page. Whatever the
        sections of each column are: all of one column go to the other, or all of one are hidden."""
        shown = seen(self.overview("tester"))
        left = [b["id"] for b in overview_blocks.zone("left")]
        right = [b["id"] for b in overview_blocks.zone("right")]
        on_left, on_right = [i for i in left if i in shown], [i for i in right if i in shown]
        on_wide = [i for i in WIDE if i in shown]
        self.assertTrue(on_left and on_right and on_wide)
        everything = {"shown": BY_DEFAULT}  # each block of the page shown, those hidden by default too

        def hiding(ids):  # each block of the page shown, but `ids`, whatever they are by default
            return {"hidden": ids, "shown": [i for i in BY_DEFAULT if i not in ids]}
        found = layout(self.overview("tester", dict(everything, other_column=left)))  # not in an order: the list's
        self.assertEqual((found["hidden columns"], found["left"]), (["left"], []))
        self.assertEqual(found["right"], [b["id"] for b in overview_blocks.BLOCKS if b["id"] in on_left + on_right])
        found = layout(self.overview("tester", hiding(right)))
        self.assertEqual((found["hidden columns"], found["left"], found["right"], found["hidden"]),
                         (["right"], on_left, on_right, on_right))
        found = layout(self.overview("tester", hiding(WIDE)))
        self.assertEqual((found["hidden columns"], found["wide"], found["hidden"]), (["wide"], on_wide, on_wide))
        found = layout(self.overview("tester", everything))
        self.assertEqual((found["hidden columns"], found["left"], found["right"], found["wide"], found["hidden"]),
                         ([], on_left, on_right, on_wide, []))
        # Without an arrangement: a column whose blocks are all hidden by default is hidden too.
        found = layout(self.overview("tester", {"reset": True}))
        empty = [zone for zone, ids in (("left", on_left), ("right", on_right), ("wide", on_wide))
                 if all(i in BY_DEFAULT for i in ids)]
        self.assertEqual((found["hidden columns"], found["left"], found["right"], found["wide"], found["hidden"]),
                         (empty, on_left, on_right, on_wide, hidden_on(on_left + on_right + on_wide)))

    def test_the_blocks_of_the_admin_never_for_another_account(self):
        """Whatever its arrangement says: listed first, hidden, or in another column, they are not rendered at all,
        not even hidden, and the editor does not list them. They are the blocks the admin's page has and this
        account's has not: the pills, the Network tile and graph, Network Info (not a shared server), Panel sign-ins."""
        admin = [b["id"] for b in editor(self.overview("tester"))["blocks"]]
        plain = editor(self.overview("friend"))
        own = [b["id"] for b in plain["blocks"]]
        admin_only = [i for i in admin if i not in own]
        for block in ADMIN_ONLY + ["network", "signins"]:
            self.assertIn(block, admin_only)
        self.assertIn("processes", own)  # their own programs
        zone = {b["id"]: b["zone"] for b in overview_blocks.BLOCKS}
        friend = {"order": admin_only + ["tile-ram"], "hidden": admin_only,
                  "other_column": [i for i in admin_only if zone[i] in layouts.OTHER_COLUMN]}
        page = self.overview("friend", friend)
        ids = Ids(page).ids
        for element in ("ov-status", "tile-net", "net-spark", "ov-network", "net-graph", "ov-hour-rx", "top10",
                        "current_rx", "ov-signins"):
            self.assertNotIn(element, ids)
        for block in admin_only:
            self.assertNotIn('data-section="%s"' % block, page)
            self.assertNotIn('data-block="%s"' % block, page)
        self.assertEqual(re.findall(r'class="ov-hidden-block" data-block="([\w-]+)"', page),
                         [i for i in own if i in BY_DEFAULT])  # only those hidden by default
        self.assertEqual(layout(page)["tiles"][0], "tile-ram")
        data = editor(page)
        self.assertEqual(sorted(b["id"] for b in data["blocks"]), sorted(own))
        self.assertEqual(data["blocks"][0]["id"], "tile-ram")
        self.assertEqual(data["order"], plain["order"])

    def test_the_editor_lists_the_blocks_of_the_page(self):
        """Each block of the page, with its label, its zone and its zone by default, in the order of the page; those
        added to the list since Customize too (Top processes, Panel sign-ins)."""
        page = self.overview("tester", quota=True)
        data = editor(page)
        ids = [b["id"] for b in data["blocks"]]
        for block in ("system-status", "tile-cpu", "tile-ram", "tile-disk", "tile-net", "bandwidth", "services", "cpu",
                      "ram", "disks", "quota", "network", "processes", "signins"):
            self.assertIn(block, ids)
        registry = {block["id"]: block for block in overview_blocks.BLOCKS}
        self.assertEqual(ids, [b["id"] for z in overview_blocks.ZONES for b in overview_blocks.zone(z)
                               if b["id"] in ids])
        self.assertEqual(data["blocks"], [{"id": i, "label": registry[i]["label"], "zone": registry[i]["zone"],
                                           "home": registry[i]["zone"], "hidden": i in BY_DEFAULT,
                                           "default_hidden": i in BY_DEFAULT} for i in ids])
        self.assertEqual(data["order"], [block["id"] for block in overview_blocks.BLOCKS if block["id"] in ids])
        self.assertEqual(data["zones"], list(overview_blocks.ZONES))
        self.assertEqual((data["url"], data["logout"]), ("/layout/arrange", "/logout"))
        page = self.overview("tester", ARRANGED, quota=True)
        data, found = editor(page), layout(page)
        where = {b["id"]: (b["zone"], b["home"], b["hidden"]) for b in data["blocks"]}
        for block, zone, home in (("services", "row", "row"), ("bandwidth", "row", "row"), ("disks", "left", "right"),
                                  ("cpu", "left", "left"), ("ram", "left", "left"), ("quota", "right", "right"),
                                  ("network", "wide", "wide"), ("processes", "wide", "wide"),
                                  ("signins", "right", "right"), ("tile-disk", "tiles", "tiles")):
            self.assertEqual(where[block], (zone, home, hidden_on([block], ARRANGED) == [block]), block)
        self.assertEqual((where["cpu"][2], where["ram"][2], where["tile-disk"][2]), (True, False, True))
        known = seen(page)
        for zone in overview_blocks.ZONES:  # the editor's order is the page's (for the blocks layout() can tell)
            self.assertEqual([b["id"] for b in data["blocks"] if b["zone"] == zone and b["id"] in known], found[zone],
                             zone)
        ids = [b["id"] for b in editor(self.overview("friend"))["blocks"]]
        self.assertIn("processes", ids)
        self.assertNotIn("signins", ids)

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
        plain = layout(self.overview("tester"))
        loader = swizzin.app.jinja_env.loader
        with mock.patch.object(overview_blocks, "BLOCKS", overview_blocks.BLOCKS + [EXTRA]), \
                mock.patch.object(swizzin.app.jinja_env, "loader",
                                  jinja2.ChoiceLoader([jinja2.DictLoader({EXTRA["template"]: EXTRA_HTML}), loader])):
            page = self.overview("tester")
            found = layout(page)
            self.assertEqual((found["left"], found["right"]), (plain["left"], plain["right"] + ["extra"]))
            self.assertEqual([b for b in editor(page)["blocks"] if b["id"] == "extra"],
                             [{"id": "extra", "label": "Extra <b>block</b>", "zone": "right", "home": "right",
                               "hidden": False, "default_hidden": False}])
            self.assertNotIn("<b>block</b>", page)  # escaped in the attribute; the editor writes it as text
            page = self.overview("tester", {"order": ["extra", "cpu"], "hidden": ["extra"], "other_column": ["extra"]})
            found = layout(page)
            self.assertEqual(found["left"], ["extra", "cpu"] + [i for i in plain["left"] if i != "cpu"])
            order = [i for zone in overview_blocks.ZONES for i in found[zone]]
            self.assertEqual((found["right"], found["hidden"]),
                             (plain["right"], hidden_on(order, {"hidden": ["extra"]})))
            self.assertEqual(found["hidden"][0], "extra")
            self.assertIn("extra-text", Ids(page).ids)
        swizzin.app.jinja_env.cache.clear()  # the page without it again
        page = self.page("tester")  # once the block is gone, its id is left out
        self.assertNotIn('data-section="extra"', page)
        self.assertNotIn("extra-text", page)
        self.assertEqual(layout(page), plain)


class Defaults(ArrangeTest):
    """The state of each block by default ("hidden" and "open" in core/overview_blocks.py), and what an account, or an
    arrangement written by the previous version, makes of it."""

    def sections(self, page):
        """{id: open} of the sections of Details, as rendered."""
        return {i: bool(o) for i, o in re.findall(r'<details class="card ov-section" data-section="([\w-]+)"( open)?>',
                                                  page)}

    def test_hidden_and_open_by_default(self):
        """Without an arrangement: the blocks hidden by default in an element with the hidden attribute (the cards that
        repeat the tiles), the sections open by default rendered open (those across the whole width), the others
        closed; the editor lists each block with its state by default."""
        page = self.overview("tester", quota=True)
        found = layout(page)
        order = [i for zone in overview_blocks.ZONES for i in found[zone]]
        self.assertEqual(found["hidden"], hidden_on(order))
        self.assertTrue(found["hidden"])
        opened = {b["id"] for b in overview_blocks.BLOCKS if b.get("open")}
        self.assertEqual(self.sections(page), {i: i in opened for i in self.sections(page)})
        self.assertEqual(found["wide"], [i for i in WIDE if i in order])
        data = editor(page)
        self.assertEqual({b["id"]: (b["hidden"], b["default_hidden"]) for b in data["blocks"]},
                         {i: (i in BY_DEFAULT, i in BY_DEFAULT) for i in order})

    def test_a_block_hidden_by_default_shown_again(self):
        """Customize sends it in "shown": the page shows it, and the other blocks hidden by default stay hidden.
        Reset to default hides it again: the page of before, byte for byte."""
        before = self.overview("tester", quota=True)
        first = BY_DEFAULT[0]
        r = self.arrange("tester", {"order": [], "hidden": [], "shown": [first], "other_column": []})
        arrangement = {"order": [], "hidden": [], "shown": [first], "other_column": []}
        self.assertEqual((r.status_code, r.get_json()), (200, {"arrangement": arrangement}))
        self.assertEqual(self.choices(), {"tester": {"layout": "overview", "overview": arrangement}})
        page = self.page("tester", quota=True)
        found = layout(page)
        order = [i for zone in overview_blocks.ZONES for i in found[zone]]
        self.assertNotIn(first, found["hidden"])
        self.assertEqual(found["hidden"], hidden_on(order, {"shown": [first]}))
        self.assertEqual(set(found["hidden"]), set(BY_DEFAULT) - {first})
        self.assertNotIn("left", found["hidden columns"])  # its column shows again
        self.assertEqual([b["hidden"] for b in editor(page)["blocks"] if b["id"] == first], [False])
        self.arrange("tester", {"reset": True})
        self.assertEqual(self.choices(), {"tester": "overview"})
        self.assertEqual(self.page("tester", quota=True), before)

    def test_hidden_by_default_and_hidden_again_is_the_default(self):
        """Sent hidden (as an editor that does not know the default would send it): the default arrangement, kept as
        none."""
        r = self.arrange("tester", {"hidden": BY_DEFAULT})
        self.assertEqual((r.status_code, r.get_json()), (200, {"arrangement": None}))
        self.assertEqual(self.choices(), {})

    def test_shown_and_hidden_at_once_is_400(self):
        for body in ({"hidden": BY_DEFAULT[:1], "shown": BY_DEFAULT[:1]}, {"shown": "cpu"}, {"shown": ["nothing"]}):
            with self.subTest(body=body):
                r = self.arrange("tester", body)
                self.assertEqual(r.status_code, 400)
        self.assertEqual(self.choices(), {})

    def test_the_sections_across_the_whole_width(self):
        """In their own zone, under the two columns: they move within it, hide, but never go to a column."""
        page = self.overview("tester")
        self.assertEqual(layout(page)["wide"], WIDE)
        self.assertEqual([b["zone"] for b in editor(page)["blocks"] if b["id"] in WIDE], ["wide"] * len(WIDE))
        for block in WIDE:
            with self.subTest(block):
                self.assertEqual(self.arrange("tester", {"other_column": [block]}).status_code, 400)
        found = layout(self.overview("tester", {"order": WIDE[::-1]}))
        self.assertEqual(found["wide"], WIDE[::-1])
        found = layout(self.overview("tester", {"hidden": WIDE}))
        self.assertEqual((found["wide"], found["hidden"][-len(WIDE):], found["hidden columns"][-1]),
                         (WIDE, WIDE, "wide"))

    def test_a_file_of_the_previous_version_with_an_arrangement(self):
        """Written by the previous version (#30), before "shown" and the whole width: what it hid stays hidden, the
        blocks hidden by default are hidden as for everyone (it never chose to show them), Disk Info stays in the
        other column, Network Info, which it had put in the other column, now takes the whole width; its order is
        kept. Showing the page writes nothing."""
        shown = seen(self.overview("tester", quota=True))
        old = {"order": ["tile-ram", "tile-cpu", "tile-net", "tile-disk", "services", "bandwidth", "disks", "cpu",
                         "ram", "quota", "network"],
               "hidden": ["ram", "tile-disk"], "other_column": ["disks", "network"]}
        text = json.dumps({"tester": {"layout": "overview", "overview": old}, "friend": "classic"})
        with open(swizzin.layout_choices.path, "w") as f:
            f.write(text)
        page = self.page("tester", quota=True)
        found = layout(page)
        self.assertEqual(found["tiles"], ["tile-ram", "tile-cpu", "tile-net", "tile-disk"])
        self.assertEqual(found["left"][:3], ["disks", "cpu", "ram"])
        self.assertEqual(found["right"], ["quota"] + unlisted("right", old, shown))
        self.assertEqual(found["wide"], ["network"] + [i for i in WIDE if i in shown and i != "network"])
        order = [i for zone in overview_blocks.ZONES for i in found[zone]]
        self.assertEqual(found["hidden"], hidden_on(order, {"hidden": ["tile-disk"]}))
        self.assertIn("ram", found["hidden"])
        with open(swizzin.layout_choices.path) as f:
            self.assertEqual(f.read(), text)

    # Written by the previous version (#31, with "shown" and the whole width of Details), as its Customize sends it:
    # every block of the admin's page, zone by zone, and what differs from that version's defaults (CPU Info and RAM
    # Info hidden; Top processes before Network Info). One hid Disk Info and Panel sign-ins and put Network Info first,
    # as the defaults are now; the other showed CPU Info again and left the rest where it was.
    PREVIOUS_ORDER = ["system-status", "tile-cpu", "tile-ram", "tile-disk", "tile-net", "bandwidth", "services", "cpu",
                      "ram", "disks", "quota", "signins"]
    AS_THE_DEFAULT = {"order": PREVIOUS_ORDER + ["network", "processes"], "hidden": ["signins", "disks"], "shown": [],
                      "other_column": []}
    CPU_SHOWN = {"order": PREVIOUS_ORDER + ["processes", "network"], "hidden": [], "shown": ["cpu"],
                 "other_column": []}

    def write(self, value):
        """The file of choices with `value` as the arrangement of "tester" (Overview), and "friend" on Classic."""
        text = json.dumps({"tester": {"layout": "overview", "overview": value}, "friend": "classic"})
        with open(swizzin.layout_choices.path, "w") as f:
            f.write(text)
        return text

    def test_an_arrangement_of_the_previous_version_as_the_defaults_are_now(self):
        """Read as the default arrangement: the page of an account that never arranged anything, byte for byte, with
        and without a quota, and reading writes nothing. Choosing a layout keeps it in the file as it was; Customize
        sending it again (Done) or Reset gives the account its plain name back."""
        for quota in (False, True):
            with self.subTest(quota=quota):
                default = self.overview("tester", quota=quota)
                text = self.write(self.AS_THE_DEFAULT)
                self.assertEqual(kept("tester"), ("overview", None))
                self.assertEqual(self.page("tester", quota=quota), default)
                with open(swizzin.layout_choices.path) as f:
                    self.assertEqual(f.read(), text)
        swizzin.layout_choices.set("tester", "classic")
        self.assertEqual(self.choices()["tester"], {"layout": "classic", "overview": self.AS_THE_DEFAULT})
        swizzin.layout_choices.set("tester", "overview")
        self.assertEqual(self.choices()["tester"], {"layout": "overview", "overview": self.AS_THE_DEFAULT})
        for body in (self.AS_THE_DEFAULT, {"reset": True}):
            with self.subTest(body=body):
                self.write(self.AS_THE_DEFAULT)
                r = self.arrange("tester", body)
                self.assertEqual((r.status_code, r.get_json()), (200, {"arrangement": None}))
                self.assertEqual(self.choices(), {"tester": "overview", "friend": "classic"})

    def test_an_arrangement_of_the_previous_version_that_showed_a_block(self):
        """It keeps its order (Top processes before Network Info) and what it showed (CPU Info); it said nothing of Disk
        Info and Panel sign-ins, which get their default: hidden, as for everyone. Reading writes nothing."""
        text = self.write(self.CPU_SHOWN)
        found = layout(self.page("tester", quota=True))
        self.assertEqual(found["wide"], ["processes", "network"])
        self.assertEqual(found["right"], ["disks", "quota", "signins"])
        self.assertEqual(found["hidden"], ["ram", "disks", "signins"])
        self.assertEqual(found["hidden columns"], [])
        self.assertEqual(kept("tester"), ("overview", self.CPU_SHOWN))
        with open(swizzin.layout_choices.path) as f:
            self.assertEqual(f.read(), text)

    def test_disk_info_shown_again_stays_shown(self):
        """Disk Info, hidden by default, shown again with Customize ("shown"): in its column, which shows again, open
        or closed as before; kept in the file, it stays shown when the page loads again and after choosing Classic and
        Overview again. Panel sign-ins, which the account said nothing of, stay hidden. Hidden again, and the default
        page comes back."""
        default = self.overview("tester")
        page = self.overview("tester", {"shown": ["disks"]})
        found = layout(page)
        self.assertNotIn("disks", found["hidden"])
        self.assertIn("signins", found["hidden"])
        self.assertNotIn("right", found["hidden columns"])
        self.assertIn('<details class="card ov-section" data-section="disks"><summary', page)
        arrangement = {"order": [], "hidden": [], "shown": ["disks"], "other_column": []}
        self.assertEqual(self.choices()["tester"], {"layout": "overview", "overview": arrangement})
        swizzin.layout_choices.set("tester", "classic")
        swizzin.layout_choices.set("tester", "overview")
        self.assertEqual(self.page("tester"), page)
        self.assertEqual(kept("tester"), ("overview", arrangement))
        r = self.arrange("tester", {"hidden": ["disks"]})
        self.assertEqual((r.status_code, r.get_json()), (200, {"arrangement": None}))
        self.assertEqual(self.page("tester"), default)

    def test_a_file_of_the_previous_version_without_an_arrangement(self):
        """An account with its plain name: the page of an account that never arranged anything."""
        default = self.overview("tester", quota=True)
        with open(swizzin.layout_choices.path, "w") as f:
            f.write('{"tester": "overview", "friend": "classic"}')
        self.assertEqual(self.page("tester", quota=True), default)

if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        os.remove(os.path.join(ROOT, "swizzin.cfg"))
