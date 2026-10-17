"""The verdicts of the uninstall campaign (tests/campaign/analyse.py), on made-up photos of a container.

Run from the repository root: python tests/test_campaign_analyse.py
"""
import copy
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests", "campaign"))
sys.path.insert(0, os.path.join(ROOT, "store-helper"))
import analyse  # noqa: E402
import removable  # noqa: E402

ENTRY = json.loads(json.dumps(removable.APPS["autobrr"].__dict__))
CTX = {"users": ["alice", "test"], "master": "test", "owner": "test"}
F = ["f", 0o644, 1000, 1000, 10, 1, None]
D = ["d", 0o755, 1000, 1000, 0, 0, None]


def diff(**kw):
    d = {"removed": {}, "added": {}, "changed": {}, "packages_removed": {}, "packages_added": {}, "packages_changed": {},
         "owned": {}, "manual": [[], []], "holds": [[], []], "units_before": {}, "units_after": {},
         "unit_files_before": {}, "unit_files_after": {}, "ports_before": [], "ports_after": [],
         "nginx_ok": [True, True], "nginx_test_after": "", "nginx_config_after": "# configuration file /etc/nginx/nginx.conf:\n",
         "locks": [[".autobrr.lock", ".nginx.lock"], [".nginx.lock"]], "users": [[], []]}
    d.update(kw)
    return d


def record():
    """A clean removal of autobrr for two users."""
    removed = {}
    for user in ("alice", "test"):
        removed["/home/%s/.config/autobrr" % user] = D
        removed["/home/%s/.config/autobrr/config.toml" % user] = F
        removed["/etc/nginx/conf.d/%s.autobrr.conf" % user] = F
        removed["/etc/systemd/system/multi-user.target.wants/autobrr@%s.service" % user] = ["l", 0o777, 0, 0, 0, 0, "x"]
    removed.update({"/usr/bin/autobrr": F, "/usr/bin/autobrrctl": F, "/etc/systemd/system/autobrr@.service": F,
                    "/etc/nginx/apps/autobrr.conf": F, "/install/.autobrr.lock": F})
    witness = {"/home/test/media/campagne-temoin.bin": "aa", "/home/alice/media/campagne-temoin.bin": "bb"}
    return {
        "install": {"result": "installed", "web": "/autobrr/ 200"},
        "ctx": copy.deepcopy(CTX),
        "removal": {"result": "removed", "checks": [{"id": "lock", "ok": True}], "errors": [],
                    "preview": {"users": ["alice", "test"]}, "archive": "/var/backups/swizzin-store/autobrr-1.tar.gz",
                    "skipped": [["/home/alice/.config/autobrr/campagne-lien-fichier", "a link"],
                                ["/home/alice/.config/autobrr/campagne-lien-dossier", "a link"],
                                ["/home/alice/.config/autobrr/campagne-fifo", "not a regular file"],
                                ["/home/alice/.config/autobrr/campagne-autre.bin", "belongs to another user"]]},
        "d12": diff(removed=removed),
        "d02": diff(locks=[[".nginx.lock"], [".nginx.lock"]]),
        "witness": {"before": witness, "after": dict(witness)},
        "panel": ["200"] * 5,
        "table_paths": {"copy": {"/home/alice/.config/autobrr": ["d"], "/home/test/.config/autobrr": ["d"]},
                        "gone": {"/usr/bin/autobrr": ["f"]}, "kept": {}},
        "archive_report": {"mode": "0o600", "uid": 0, "dir_mode": "0o700", "dir_uid": 0, "manifest_bad": [],
                           "outside_manifest": [], "restore_txt": "1. Reinstall the app: box install autobrr",
                           "missing": [], "copied_hashes": ["11"]},
        "traps": {"link_file": "/home/alice/.config/autobrr/campagne-lien-fichier",
                  "link_dir": "/home/alice/.config/autobrr/campagne-lien-dossier",
                  "fifo": "/home/alice/.config/autobrr/campagne-fifo", "foreign": "/home/alice/.config/autobrr/campagne-autre.bin",
                  "target": "/home/test/media/campagne-film.mkv", "target_sha": "99"},
        "reinstall": {"result": "installed", "web": "/autobrr/ 200"},
    }


class Scope(unittest.TestCase):
    def test_patterns_of_the_table_for_every_user(self):
        allowed = analyse.scope("autobrr", ENTRY, CTX)
        self.assertIn("/home/alice/.config/autobrr", allowed)
        self.assertIn("/home/test/.config/autobrr", allowed)
        self.assertTrue(analyse.under("/home/test/.config/autobrr/logs/x.log", allowed))
        self.assertTrue(analyse.under("/etc/nginx/conf.d/alice.autobrr.conf", allowed))
        self.assertTrue(analyse.under("/etc/systemd/system/multi-user.target.wants/autobrr@test.service", allowed))
        self.assertTrue(analyse.under("/etc/systemd/system/autobrr@.service", allowed))
        self.assertFalse(analyse.under("/home/test/.config", allowed))
        self.assertFalse(analyse.under("/home/test/.config/autobrr2/x", allowed))
        self.assertFalse(analyse.under("/etc/nginx/conf.d/alice.qui.conf", allowed))

    def test_owner_and_master(self):
        lidarr = json.loads(json.dumps(removable.APPS["lidarr"].__dict__))
        ctx = dict(CTX, owner="alice")
        self.assertIn("/home/alice/.config/Lidarr/config.xml", analyse.scope("lidarr", lidarr, ctx))
        self.assertEqual(analyse.kept(lidarr, ctx), ["/home/alice/.config/Lidarr"])


class Verdict(unittest.TestCase):
    def check(self, r, verdict="pass", fail=None, review=None):
        v = analyse.verdict_for({"app": "autobrr", "reinstall": True}, ENTRY, r)
        self.assertEqual(v["verdict"], verdict, v)
        if fail:
            self.assertTrue(any(fail in f for f in v["fail"]), v)
        if review:
            self.assertTrue(any(review in f for f in v["review"]), v)
        return v

    def test_a_clean_removal_passes(self):
        v = self.check(record())
        self.assertEqual(v["fail"], [])

    def test_a_user_file_removed_outside_the_scope_fails(self):
        r = record()
        r["d12"]["removed"]["/home/test/torrents/film.mkv"] = F
        self.check(r, "fail", "removed outside its scope")

    def test_a_package_file_of_an_allowed_package_is_in_scope(self):
        r = record()
        r["d12"]["removed"]["/usr/lib/x/y.so"] = F
        r["d12"]["owned"]["/usr/lib/x/y.so"] = "foo"
        self.check(r)  # the file is fine; the package itself is judged on its own:
        r["d12"]["packages_removed"]["foo"] = ["1", "ii"]
        self.check(r, "fail", "packages removed that it may not remove")

    def test_packages_installed_or_upgraded_by_a_removal_fail(self):
        r = record()
        r["d12"]["packages_added"]["bar"] = ["1", "ii"]
        self.check(r, "fail", "packages installed by the removal")
        r = record()
        r["d12"]["packages_changed"]["bar"] = [["1", "ii"], ["2", "ii"]]
        self.check(r, "fail", "upgraded or downgraded")

    def test_changes_in_a_home_fail_and_elsewhere_are_reviewed(self):
        r = record()
        r["d12"]["changed"]["/home/test/.bashrc"] = [F, F]
        self.check(r, "fail", "changed in a home folder")
        r = record()
        r["d12"]["changed"]["/etc/passwd"] = [F, F]
        r["d12"]["users"] = [["autobrr"], []]
        self.check(r, "pass", review="user accounts changed")

    def test_runtime_files_and_files_in_kept_folders_are_reviewed(self):
        r = record()
        r["d12"]["removed"]["/var/lib/foo/app.pid"] = F
        self.check(r, "pass", review="runtime file")

    def test_witnesses(self):
        r = record()
        r["witness"]["after"]["/home/test/media/campagne-temoin.bin"] = None
        self.check(r, "fail", "the user's files changed")
        r = record()
        r["witness"] = {"before": {}, "after": {}}
        self.check(r, "fail", "no witness file")

    def test_the_panel_must_answer_all_along(self):
        r = record()
        r["panel"] = ["200", "502", "200"]
        self.check(r, "fail", "the panel did not always answer")
        r["panel"] = []
        self.check(r, "fail", "never asked")

    def test_the_uninstallers_result_and_checks(self):
        r = record()
        r["removal"]["result"] = "incomplete"
        r["removal"]["checks"] = [{"id": "files", "ok": False, "detail": "/usr/bin/autobrr"}]
        v = self.check(r, "fail", "result incomplete")
        self.assertTrue(any("files (/usr/bin/autobrr)" in f for f in v["fail"]))

    def test_a_refusal_fails_a_cycle(self):
        r = record()
        r["removal"] = {"preview_error": "refused", "preview_message": "x is installed"}
        self.check(r, "fail", "refused")

    def test_the_copy(self):
        r = record()
        r["archive_report"]["missing"] = ["/home/test/.config/autobrr/autobrr.db"]
        self.check(r, "fail", "settings missing from the copy")
        r["archive_report"]["missing"] = ["/home/test/.config/autobrr/autobrr.db-wal"]
        self.assertEqual(self.check(r, "pass")["review"], [])  # folded into the database when it stopped
        r["archive_report"]["missing"] = ["/home/test/.config/autobrr/autobrr.db", "/home/test/.config/autobrr/autobrr.db-wal"]
        self.check(r, "fail", "autobrr.db']", review="autobrr.db-wal")  # the database itself is missing
        r["archive_report"]["missing"] = ["/home/test/.config/autobrr/app.pid"]
        self.check(r, "pass", review="runtime files missing")
        r = record()
        r["archive_report"]["mode"] = "0o644"
        self.check(r, "fail", "can be read by others")
        r = record()
        r["archive_report"]["manifest_bad"] = ["/x"]
        self.check(r, "fail", "do not match its manifest")
        r = record()
        r["archive_report"]["restore_txt"] = ""
        self.check(r, "fail", "RESTORE.txt")
        r = record()
        r["removal"]["archive"] = None
        r["archive_report"] = None
        self.check(r, "fail", "no copy of its settings")

    def test_the_traps(self):
        r = record()
        r["removal"]["skipped"] = r["removal"]["skipped"][1:]
        self.check(r, "fail", "campagne-lien-fichier was not listed")
        r = record()
        r["archive_report"]["copied_hashes"] = ["99"]
        self.check(r, "fail", "followed a link")

    def test_what_stays_behind_is_reviewed(self):
        r = record()
        r["d02"]["added"] = {"/opt/x/y": F, "/home/test/media/campagne-temoin.bin": F}
        r["d02"]["packages_added"] = {"libfoo": ["1", "ii"]}
        r["d02"]["added"]["/home/test/torrents"] = D
        r["created"] = ["/home/test/torrents"]  # made by the campaign for its witness files
        v = self.check(r, "pass", review="left by the installation: {'/opt/x/y': 1}")
        self.assertTrue(any("packages left" in x for x in v["review"]))

    def test_units_nginx_and_lock_left(self):
        r = record()
        r["d12"]["unit_files_after"] = {"autobrr@.service": "indirect"}
        self.check(r, "fail", "still known to systemd")
        r = record()
        r["d12"]["nginx_config_after"] += "location /autobrr {\n"
        self.check(r, "fail", "location for /autobrr")
        r = record()
        r["d12"]["nginx_config_after"] += "# configuration file /etc/nginx/apps/autobrr.conf:\n"
        self.check(r, "fail", "nginx still loads")
        r = record()
        r["d12"]["locks"][1].append(".autobrr.lock")
        self.check(r, "fail", "lock file is still there")
        r = record()
        r["d12"]["nginx_ok"] = [True, False]
        self.check(r, "fail", "nginx -t fails")

    def test_reinstall_and_restore(self):
        r = record()
        r["reinstall"] = {"result": "failed"}
        self.check(r, "fail", "reinstalling it failed")
        del r["reinstall"]  # a run that does not reinstall (through the panel)
        self.assertEqual(analyse.verdict_for({"app": "autobrr", "reinstall": False}, ENTRY, r)["verdict"], "pass")
        r = record()
        r["restore"] = {"before": "abc", "after": "abc", "active": True, "before_restore": "new"}
        self.check(r)
        r["restore"]["after"] = "new"
        self.check(r, "fail", "did not bring its settings back")
        r["restore"] = {"before": "abc", "after": "abc", "active": False, "before_restore": "new"}
        self.check(r, "fail", "does not run")
        r["restore"] = {"before": "abc", "after": "abc", "active": True, "before_restore": "abc"}
        self.check(r, "pass", review="proves little")


class Failures(unittest.TestCase):
    def failure(self, expect, text="", **kw):
        r = {"ctx": CTX, "removal": {"result": "failed", "message": "apt or dpkg is working (x is locked)"},
             "state_before": {"lock": True, "units": {"autobrr@test.service": ["active", "enabled"]}},
             "state_after": {"lock": True, "units": {"autobrr@test.service": ["active", "enabled"]}},
             "d12": diff(locks=[[".autobrr.lock"], [".autobrr.lock"]]), "archives_after": [], "panel": ["200"]}
        r.update(kw)
        return analyse.verdict_for({"app": "autobrr", "expect": expect, "text": text}, ENTRY, r)

    def test_refused_and_nothing_touched(self):
        self.assertEqual(self.failure("refused", "is locked")["verdict"], "pass")

    def test_refused_with_another_reason(self):
        v = self.failure("refused", "not enough free space")
        self.assertEqual(v["verdict"], "fail")
        self.assertIn("does not say", v["fail"][0])

    def test_not_put_back(self):
        v = self.failure("put-back", "", state_after={"lock": True, "units": {"autobrr@test.service": ["inactive", "disabled"]}})
        self.assertEqual(v["verdict"], "fail")

    def test_something_removed_or_a_copy_left(self):
        d = diff(removed={"/home/test/.config/autobrr/config.toml": F})
        self.assertEqual(self.failure("refused", "is locked", d12=d)["verdict"], "fail")
        self.assertEqual(self.failure("put-back", "", archives_after=["autobrr-1.tar.gz"])["verdict"], "fail")
        self.assertEqual(self.failure("put-back", "", archives_after=["campagne-plein"])["verdict"], "pass")

    def test_after_a_restart_the_last_job_tells(self):
        v = self.failure("put-back", "put back", removal={"errors": ["helper-closed"]},
                         last={"result": "failed", "message": "nothing was removed: autobrr was put back"})
        self.assertEqual(v["verdict"], "pass", v)
        v = self.failure("put-back", "put back", removal={"errors": ["helper-closed"]}, last={"result": "removed"})
        self.assertEqual(v["verdict"], "fail", v)

    def test_consistent(self):
        after = {"lock": False, "units": {}}
        ok = self.failure("consistent", removal={"result": "removed", "checks": [{"id": "lock", "ok": True}]}, state_after=after)
        self.assertEqual(ok["verdict"], "pass", ok)
        bad = self.failure("consistent", removal={"result": "removed", "checks": [{"id": "lock", "ok": True}]})
        self.assertEqual(bad["verdict"], "fail")  # its lock is still there
        bad = self.failure("consistent", removal={"result": "failed"}, state_after=after)
        self.assertEqual(bad["verdict"], "fail")  # "nothing removed", but it is gone

    def test_nginx_broken_before(self):
        checks = [{"id": "nginx", "ok": True, "detail": "broken before the removal already"}]
        ok = self.failure("nginx-broken", removal={"result": "removed", "checks": checks}, after_fix="0")
        self.assertEqual(ok["verdict"], "pass", ok)
        bad = self.failure("nginx-broken", removal={"result": "removed", "checks": checks}, after_fix="2")
        self.assertEqual(bad["verdict"], "fail")


class Neighbours(unittest.TestCase):
    def test_a_neighbour_must_stay_installed_and_running(self):
        r = record()
        before = {"lock": True, "units": {"qbittorrent@test.service": ["active", "enabled"]}, "web": "/qbittorrent/ 200"}
        r["watch_before"] = {"qbittorrent": before}
        r["watch_after"] = {"qbittorrent": dict(before)}
        self.assertEqual(analyse.verdict_for({"app": "autobrr"}, ENTRY, r)["verdict"], "pass")
        r["watch_after"] = {"qbittorrent": dict(before, units={"qbittorrent@test.service": ["failed", "enabled"]})}
        self.assertEqual(analyse.verdict_for({"app": "autobrr"}, ENTRY, r)["verdict"], "fail")
        v = analyse.verdict_for({"app": "autobrr", "affected": ["qbittorrent"]}, ENTRY, r)
        self.assertEqual(v["verdict"], "pass")  # said in the confirmation: noted for review
        self.assertTrue(v["review"])
        r["watch_after"] = {"qbittorrent": dict(before, lock=False)}
        self.assertEqual(analyse.verdict_for({"app": "autobrr", "affected": ["qbittorrent"]}, ENTRY, r)["verdict"], "fail")


class Panel(unittest.TestCase):
    def test_refusals_through_the_real_panel(self):
        good = {"without_token": "refused: One or more namespaces failed to connect", "wrong_password": "password",
                "wrong_name": "confirm-mismatch", "started": {"ok": True}}
        r = record()
        r["removal"]["panel"] = good
        self.assertEqual(analyse.verdict_for({"app": "autobrr", "via_panel": True}, ENTRY, r)["verdict"], "pass")
        for key, value in (("without_token", "connected"), ("wrong_password", None), ("wrong_name", "bad-ticket"),
                           ("started", {"ok": False, "error": "busy"})):
            r["removal"]["panel"] = dict(good, **{key: value})
            v = analyse.verdict_for({"app": "autobrr", "via_panel": True}, ENTRY, r)
            self.assertEqual(v["verdict"], "fail", key)


if __name__ == "__main__":
    unittest.main()
