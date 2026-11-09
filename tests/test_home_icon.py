"""The home screen icon: static/manifest.json, the icons of static/img/home and templates/head_icons.html, in the head
of every page of the panel, with or without URLBASE, served without a login.

Run from the repository root: python tests/test_home_icon.py
(uses a throwaway htpasswd file and a throwaway file of layouts: nothing on the machine is touched)."""
import base64
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from unittest import mock
from urllib.parse import urljoin, urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

# The panel reads its users from /etc/htpasswd: point it to a throwaway file before importing it.
_tmp = tempfile.mkdtemp()
from passlib.apache import HtpasswdFile  # noqa: E402
_ht = HtpasswdFile(os.path.join(_tmp, "htpasswd"), new=True)
_ht.set_password("tester", "tester")
_ht.save()
with open(os.path.join(ROOT, "swizzin.cfg"), "w") as f:
    f.write(f"FLASK_HTPASSWD_PATH = '{_tmp}/htpasswd'\nADMIN_USER = 'tester'\nAPP_STORE = True\n")  # its page too

import swizzin  # noqa: E402
from core.middleware import PrefixMiddleware  # noqa: E402

STATIC = os.path.join(ROOT, "static")
AUTH = {"Authorization": "Basic " + base64.b64encode(b"tester:tester").decode()}
PAGES = [{"name": "autobrr", "pretty_name": "autobrr", "url": "https://demo.invalid/autobrr", "systemd": "autobrr@",
          "img": "autobrr"}]
# What every page carries, besides the two links: (name, content).
METAS = [("apple-mobile-web-app-capable", "yes"), ("mobile-web-app-capable", "yes"),
         ("apple-mobile-web-app-title", "swizzin"), ("apple-mobile-web-app-status-bar-style", "black"),
         ("theme-color", "#222222")]


def png(path):
    """(width, height, colour type, names of the chunks) of a PNG file, read without a library."""
    with open(path, "rb") as f:
        data = f.read()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", path
    pos, chunks, header = 8, [], None
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        if kind == b"IHDR":
            header = struct.unpack(">IIBB", data[pos + 8:pos + 18])
        chunks.append(kind.decode("ascii"))
        pos += 12 + length
    width, height, _depth, colour = header
    return width, height, colour, chunks


class Head(HTMLParser):
    """The <link> and <meta> elements of a page, each with whether it comes before the body (the head, written or
    implied: a fragment such as the logout page has no <head>, its first <div> starts the body), and its <input>."""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.links, self.metas, self.inputs, self.body = [], [], [], False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag in ("body", "div"):
            self.body = True
        elif tag in ("link", "meta"):
            (self.links if tag == "link" else self.metas).append(dict(attrs, head=not self.body))
        elif tag == "input":
            self.inputs.append(dict(attrs))


class HomeIcon(unittest.TestCase):
    base = ""  # URLBASE

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        real_isfile = os.path.isfile
        locks = {"/install/.quota.lock": False, "/install/.sbio.lock": False}
        for patcher in (mock.patch.object(swizzin.layout_choices, "path", os.path.join(self.dir, "layouts.json")),
                        mock.patch.object(swizzin.layout_choices, "default", "classic"),
                        mock.patch.object(swizzin, "get_mounts", return_value=["/"]),
                        mock.patch.object(swizzin, "generate_page_list", return_value=PAGES),
                        mock.patch("core.util.generate_page_list", return_value=PAGES),
                        mock.patch.dict(swizzin.app.config, {"STORE_SOCKET": os.path.join(self.dir, "none.sock")}),
                        mock.patch.object(swizzin.os.path, "isfile",
                                          side_effect=lambda p: locks[p] if p in locks else real_isfile(p))):
            patcher.start()
            self.addCleanup(patcher.stop)
        swizzin.limiter.reset()  # requests without a login are limited: each test starts with none counted
        self.client = swizzin.app.test_client()

    def urlbase(self, prefix):
        """The panel as swizzin.py sets it up with URLBASE = prefix in swizzin.cfg (PrefixMiddleware)."""
        middleware = swizzin.app.wsgi_app  # Flask-SocketIO wraps it: the prefix is inside
        while not isinstance(middleware, PrefixMiddleware):
            middleware = middleware.wsgi_app
        for patcher in (mock.patch.object(middleware, "prefix", prefix),
                        mock.patch.dict(swizzin.app.config, {"URLBASE": prefix})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def manifest(self):
        def unique(pairs):
            keys = [k for k, _ in pairs]
            self.assertEqual(len(keys), len(set(keys)), "a key is written twice")
            return dict(pairs)
        with open(os.path.join(STATIC, "manifest.json"), encoding="utf-8") as f:
            return json.load(f, object_pairs_hook=unique)

    def pages(self):
        """Every page of the panel, rendered for the admin: {name: HTML}."""
        pages = {"login": self.client.get(self.base + "/login")}
        for name, path in (("classic", "/"), ("stats", "/stats"), ("logout", "/logout")):
            pages[name] = self.client.get(self.base + path, headers=AUTH)
        with self.assertLogs("swizzin", "WARNING"):  # "App Store helper unavailable": no helper in the tests
            pages["app store"] = self.client.get(self.base + "/apps", headers=AUTH)
        with mock.patch.object(swizzin.layout_choices, "default", "overview"):
            pages["overview"] = self.client.get(self.base + "/", headers=AUTH)
        for name, r in pages.items():
            self.assertEqual(r.status_code, 200, name)
        self.assertIn('class="container-fluid overview"', pages["overview"].get_data(as_text=True))
        self.assertIn('class="card-columns live-layout"', pages["classic"].get_data(as_text=True))
        return {name: r.get_data(as_text=True) for name, r in pages.items()}


class Manifest(HomeIcon):
    def test_the_manifest(self):
        m = self.manifest()
        self.assertEqual((m["name"], m["short_name"], m["display"]), ("swizzin dashboard", "swizzin", "standalone"))
        # Relative to the manifest, in /static: the root of the panel, at / as under URLBASE (see Urlbase).
        self.assertEqual((m["start_url"], m["scope"]), ("../", "../"))
        self.assertEqual((m["theme_color"], m["background_color"]), ("#222222", "#222222"))
        purposes = sorted((i["sizes"], i.get("purpose", "any")) for i in m["icons"])
        self.assertEqual(purposes, [("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")])

    def test_every_icon_exists_with_its_size_and_is_opaque(self):
        """Opaque: RGB without alpha (PNG colour type 2) and no tRNS chunk, as Apple asks for its icons."""
        icons = [(i["src"], i["sizes"], i["type"]) for i in self.manifest()["icons"]]
        icons.append(("img/home/apple-touch-icon.png", "180x180", "image/png"))
        for src, sizes, kind in icons:
            with self.subTest(src):
                self.assertFalse(src.startswith("/"), "relative to the manifest, for URLBASE")
                width, height, colour, chunks = png(os.path.join(STATIC, src))
                self.assertEqual(("%dx%d" % (width, height), kind), (sizes, "image/png"))
                self.assertEqual(colour, 2)
                self.assertNotIn("tRNS", chunks)


class Pages(HomeIcon):
    def check(self, html):
        head = Head(html)
        links = [(a["rel"], a["href"], a["head"]) for a in head.links
                 if a.get("rel") in ("manifest", "apple-touch-icon")]
        self.assertEqual(links, [("manifest", self.base + "/static/manifest.json", True),
                                 ("apple-touch-icon", self.base + "/static/img/home/apple-touch-icon.png", True)])
        for meta, content in METAS:
            found = [(m.get("content"), m["head"]) for m in head.metas if m.get("name") == meta]
            self.assertEqual(found, [(content, True)], meta)

    def test_every_page_has_the_tags_once_in_its_head(self):
        for name, html in self.pages().items():
            with self.subTest(name):
                self.check(html)

    def test_the_login_page(self):
        """The keychain of iOS can fill the login page when the app has forgotten the login; it fits a phone's width."""
        head = Head(self.client.get(self.base + "/login").get_data(as_text=True))
        fields = {i["id"]: i.get("autocomplete") for i in head.inputs if i.get("type") in ("text", "password")}
        self.assertEqual(fields, {"basic-username": "username", "basic-password": "current-password"})
        self.assertIn({"name": "viewport", "content": "width=device-width, initial-scale=1, shrink-to-fit=no",
                       "head": True}, head.metas)


class Urlbase(Pages):
    """The same pages, with URLBASE = /panel: every address follows it."""
    base = "/panel"

    def setUp(self):
        super().setUp()
        self.urlbase(self.base)

    def test_outside_urlbase_nothing(self):
        self.assertEqual(self.client.get("/static/manifest.json").status_code, 404)


class Served(HomeIcon):
    """What a phone fetches before any login: the login page, then the manifest and the icons its head names."""
    origin = "https://box.example"

    def links(self):
        """{rel: absolute address} of the links of the login page, fetched without a login."""
        page = self.client.get(self.base + "/login")
        self.assertEqual(page.status_code, 200)
        return {a["rel"]: urljoin(self.origin + self.base + "/login", a["href"])
                for a in Head(page.get_data(as_text=True)).links}

    def fetch_all(self, links):
        """The manifest and every icon, without a login: (address of the manifest, the manifest)."""
        r = self.client.get(urlsplit(links["manifest"]).path)
        self.assertEqual((r.status_code, r.mimetype), (200, "application/json"))
        m = json.loads(r.get_data(as_text=True))
        r.close()
        for url in [urljoin(links["manifest"], i["src"]) for i in m["icons"]] + [links["apple-touch-icon"]]:
            with self.subTest(url):
                path = urlsplit(url).path
                self.assertTrue(path.startswith(self.base + "/static/img/home/"), path)
                icon = self.client.get(path)
                self.assertEqual((icon.status_code, icon.mimetype), (200, "image/png"))
                with open(os.path.join(STATIC, path[len(self.base + "/static/"):]), "rb") as f:
                    self.assertEqual(icon.get_data(), f.read())
                icon.close()
        return links["manifest"], m

    def test_without_a_login(self):
        self.fetch_all(self.links())

    def test_start_url_and_scope_are_the_dashboard(self):
        manifest_url, m = self.fetch_all(self.links())
        start, scope = urljoin(manifest_url, m["start_url"]), urljoin(manifest_url, m["scope"])
        self.assertEqual(start, self.origin + self.base + "/")
        self.assertEqual(scope, start)
        r = self.client.get(urlsplit(start).path)  # not logged in: the login page, which is in the scope
        self.assertEqual(r.status_code, 302)
        login = urlsplit(urljoin(start, r.headers["Location"])).path
        self.assertEqual(login, self.base + "/login")
        self.assertTrue(login.startswith(urlsplit(scope).path))
        self.assertEqual(self.client.get(urlsplit(start).path, headers=AUTH).status_code, 200)

    def test_not_rate_limited(self):
        """The rate limit (RATELIMIT_DEFAULT, 5 per minute) refuses the 6th anonymous request of a page, never the
        static files: Flask-Limiter leaves its "static" endpoints alone."""
        self.assertTrue(swizzin.limiter.enabled)
        links = self.links()  # the 1st request of the login page
        codes = [self.client.get(self.base + "/login").status_code for _ in range(5)]
        self.assertEqual(codes, [200] * 4 + [429])
        for _ in range(4):  # 20 more requests (the manifest and 4 icons, 4 times), each answered
            self.fetch_all(links)


class ServedUrlbase(Served):
    base = "/panel"

    def setUp(self):
        super().setUp()
        self.urlbase(self.base)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        os.remove(os.path.join(ROOT, "swizzin.cfg"))
