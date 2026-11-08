"""The history of the panel's sign-ins (core/signins.py), without the panel: what a sign-in and a failed attempt are,
what is kept and for how long, the file (only once a file is named, mode 0600, broken, too old, 0 days) and how often
it is written. The panel itself (the route, core/htpasswd.py telling the history) is tested in
tests/test_signins_page.py, the section of the page in tests/test_signins_js.py.

Run from the repository root: python tests/test_signins.py
(a fake clock and throwaway folders: nothing on the machine is touched)."""
import json
import logging
import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import signins, statefile  # noqa: E402
from core.signins import GROUP, KEEP, PAIRS, SESSION, History  # noqa: E402

NOW = 1794182400  # 8 November 2026, 12:00 UTC
DAY = 86400
A, B = "192.0.2.10", "192.0.2.20"  # documentation addresses (RFC 5737, RFC 3849)
V6 = "2001:db8::7"


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = os.path.join(self.dir, signins.FILE)
        self.logger = logging.getLogger("test_signins")
        self.clock = Clock()
        self.writes = []  # the objects written, in order
        real = statefile.StateFile.write

        def write(file, data):
            self.writes.append(json.loads(json.dumps(data)))
            return real(file, data)
        patcher = mock.patch.object(statefile.StateFile, "write", write)
        patcher.start()
        self.addCleanup(patcher.stop)

    def history(self, days=30, keep=True):
        """A history on the fake clock; keep: in the file of the test, as the panel that serves does."""
        h = History(days, self.logger)
        h.clock = self.clock
        if keep:
            h.keep_in(self.path)
        return h

    def file(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def at(self, seconds):
        self.clock.now = NOW + seconds

    def signins(self, h):
        return [(r["user"], r["address"], r["at"] - NOW) for r in h.recent(None)["signins"]][::-1]

    def failures(self, h):
        return [(r["user"], r["address"], r["at"] - NOW, r["last"] - NOW, r["count"], r["blocked"])
                for r in h.recent(None)["failures"]][::-1]


class Settings(unittest.TestCase):
    def test_valid_values(self):
        logger = mock.Mock()
        for days in (0, 1, 30, 365):
            with self.subTest(days):
                self.assertEqual(signins.settings({"SIGNIN_HISTORY_DAYS": days}, logger), days)
        self.assertEqual(signins.settings({}, logger), 30)
        logger.warning.assert_not_called()

    def test_an_invalid_value_gives_30_and_a_warning(self):
        for days in (-1, 366, "30", 3.5, True, None, [30]):
            with self.subTest(days), self.assertLogs(level="WARNING") as logs:
                self.assertEqual(signins.settings({"SIGNIN_HISTORY_DAYS": days}), 30)
            self.assertIn("SIGNIN_HISTORY_DAYS = %r is invalid" % (days,), logs.output[0])


class Addresses(unittest.TestCase):
    def test_written_the_usual_way(self):
        for given, kept in (("192.0.2.10", "192.0.2.10"), (" 192.0.2.10 ", "192.0.2.10"),
                            ("2001:DB8:0:0::7", "2001:db8::7"), ("2001:0db8:0000:0000:0000:0000:0000:0007", V6),
                            ("::ffff:192.0.2.10", "192.0.2.10"), ("fe80::1%eth0", "fe80::1"), ("::1", "::1"),
                            ("10.7.0.2", "10.7.0.2")):
            with self.subTest(given):
                self.assertEqual(signins.address_of(given), kept)

    def test_anything_else_is_no_address(self):
        for given in ("", "unknown", "192.0.2.300", "192.0.2.10:443", "[2001:db8::7]", "<img src=x>", None, 3232235777,
                      "192.0.2.10, 192.0.2.11"):
            with self.subTest(given):
                self.assertIsNone(signins.address_of(given))


class SignIns(HistoryTest):
    def test_the_first_request_is_a_sign_in(self):
        h = self.history()
        h.checked("admin", A, True)
        self.assertEqual(self.signins(h), [("admin", A, 0)])

    def test_an_open_page_counts_once(self):
        """Requests every few seconds or minutes, for hours: one sign-in, however long it stays open."""
        h = self.history()
        for t in range(0, 6 * 3600, 25 * 60):  # one every 25 minutes for 6 hours
            self.at(t)
            h.checked("admin", A, True)
        self.assertEqual(self.signins(h), [("admin", A, 0)])

    def test_a_new_sign_in_after_30_minutes_without_a_request(self):
        h = self.history()
        h.checked("admin", A, True)
        self.at(SESSION)
        h.checked("admin", A, True)  # 30 minutes exactly: the same visit
        self.at(2 * SESSION + 1)
        h.checked("admin", A, True)
        self.assertEqual(self.signins(h), [("admin", A, 0), ("admin", A, 2 * SESSION + 1)])

    def test_another_address_or_account_is_another_sign_in(self):
        h = self.history()
        for t, user, address in ((0, "admin", A), (1, "admin", B), (2, "friend", A), (3, "admin", V6), (4, "admin", A)):
            self.at(t)
            h.checked(user, address, True)
        self.assertEqual(self.signins(h), [("admin", A, 0), ("admin", B, 1), ("friend", A, 2), ("admin", V6, 3)])

    def test_ipv6_written_differently_is_the_same_address(self):
        h = self.history()
        for t, address in enumerate(("2001:DB8::7", "2001:db8:0:0:0:0:0:7", V6)):
            self.at(t)
            h.checked("admin", address, True)
        self.at(3)
        h.checked("admin", "::ffff:192.0.2.10", True)
        h.checked("admin", A, True)
        self.assertEqual(self.signins(h), [("admin", V6, 0), ("admin", A, 3)])

    def test_an_invalid_address(self):
        h = self.history()
        h.checked("admin", "<img src=x onerror=alert(1)>", True)
        h.checked("admin", "not an address", True)
        self.assertEqual(self.signins(h), [("admin", None, 0)])

    def test_no_account_no_sign_in(self):
        h = self.history()
        for user in (None, "", 7):
            h.checked(user, A, True)
        self.assertEqual(self.signins(h), [])
        self.assertFalse(os.path.exists(self.path))


class Failures(HistoryTest):
    def test_failures_of_a_name_from_an_address_make_one_line(self):
        """A forgotten tab sending an old password every minute, for an hour: one line, and its count."""
        h = self.history()
        for t in range(0, 3600, 60):
            self.at(t)
            h.checked("admin", A, False)
        self.assertEqual(self.failures(h), [("admin", A, 0, 3540, 60, 0)])

    def test_ten_minutes_without_one_start_a_new_line(self):
        h = self.history()
        h.checked("admin", A, False)
        self.at(GROUP - 1)
        h.checked("admin", A, False)
        self.at(2 * GROUP - 1)
        h.checked("admin", A, False)
        self.assertEqual(self.failures(h), [("admin", A, 0, GROUP - 1, 2, 0), ("admin", A, 2 * GROUP - 1,
                                                                                  2 * GROUP - 1, 1, 0)])

    def test_another_name_or_address_is_another_line(self):
        h = self.history()
        for t, user, address in ((0, "admin", A), (1, None, A), (2, "admin", B), (3, None, A), (4, "admin", A)):
            self.at(t)
            h.checked(user, address, False)
        self.assertEqual(self.failures(h), [("admin", B, 2, 2, 1, 0), (None, A, 1, 3, 2, 0), ("admin", A, 0, 4, 2, 0)])

    def test_an_unknown_name_is_never_kept(self):
        """core/htpasswd.py gives None for a name that is not an account: nothing else can be kept."""
        h = self.history()
        h.checked(None, A, False)
        h.checked("", A, False)
        self.assertEqual(self.failures(h), [(None, A, 0, 0, 2, 0)])
        self.assertEqual(self.file()["failures"][0]["user"], None)

    def test_the_failure_that_blocks_the_address(self):
        """It says for how long; the next failure, once the block is over, starts a new line."""
        h = self.history()
        for t in range(4):
            self.at(t)
            h.checked("admin", A, False)
        self.at(4)
        h.checked("admin", A, False, blocked=900)
        self.at(905)
        h.checked("admin", A, False)
        self.assertEqual(self.failures(h), [("admin", A, 0, 4, 5, 900), ("admin", A, 905, 905, 1, 0)])

    def test_a_block_on_its_own_line(self):
        h = self.history()
        h.checked(None, A, False, blocked=900)
        h.checked("friend", A, False, blocked="900")  # never anything but seconds
        self.assertEqual(self.failures(h), [(None, A, 0, 0, 1, 900), ("friend", A, 0, 0, 1, 0)])

    def test_failures_and_sign_ins_are_apart(self):
        h = self.history()
        h.checked("admin", A, False)
        h.checked("admin", A, True)
        self.assertEqual(self.signins(h), [("admin", A, 0)])
        self.assertEqual(self.failures(h), [("admin", A, 0, 0, 1, 0)])


class Limits(HistoryTest):
    def test_each_list_keeps_its_newest_100(self):
        h = self.history()
        for i in range(KEEP + 50):
            self.at(i)
            h.checked("admin", "192.0.2.%d" % (i % 250) if i < 250 else B, True)
        self.assertEqual(h.recent(None)["signins_total"], KEEP)
        self.assertEqual(h.signins[0]["at"], NOW + 50)  # the oldest kept
        self.assertEqual(len(self.file()["signins"]), KEEP)

    def test_failures_never_push_the_sign_ins_out(self):
        """An attack from many addresses fills the failed attempts only: the sign-ins stay."""
        h = self.history()
        h.checked("admin", A, True)
        for i in range(1000):
            self.at(i)
            h.checked(None, "198.51.100.%d" % (i % 250) if i < 250 else "2001:db8::%x" % i, False)
        found = h.recent(None)
        self.assertEqual((found["signins_total"], found["failures_total"]), (1, KEEP))
        self.at(SESSION + 1000)
        h.checked("admin", A, True)  # a new sign-in: the file is written at once
        data = self.file()
        self.assertEqual((len(data["signins"]), len(data["failures"])), (2, KEEP))
        self.assertLessEqual(len(data["signins"]) + len(data["failures"]), 200)

    def test_the_pairs_in_memory_are_bounded(self):
        h = self.history(keep=False)
        for i in range(PAIRS + 500):
            h.checked("admin", "2001:db8::%x" % i, True)
        self.assertEqual(len(h.sessions), PAIRS)
        self.assertIn(("admin", "2001:db8::%x" % (PAIRS + 499)), h.sessions)

    def test_a_huge_count(self):
        h = self.history()
        h.checked("admin", A, False)
        h.failures[0]["count"] = signins.MAX_COUNT
        h.checked("admin", A, False)
        self.assertEqual(h.failures[0]["count"], signins.MAX_COUNT)


class Retention(HistoryTest):
    def test_lines_go_30_days_after_their_last_request(self):
        h = self.history()
        h.checked("admin", A, True)
        h.checked(None, B, False)
        self.at(30 * DAY - 60)
        h.checked("friend", A, True)
        self.at(30 * DAY)
        self.assertEqual(len(h.recent(None)["signins"]), 2)
        self.at(30 * DAY + 1)
        found = h.recent(None)
        self.assertEqual(([r["user"] for r in found["signins"]], found["failures"]), (["friend"], []))
        h.checked("friend", A, True)  # the same visit: no new line; the file drops the old ones within 10 minutes
        self.assertEqual(len(self.file()["signins"]), 2)
        self.at(30 * DAY + 540)
        h.checked("friend", A, True)
        self.assertEqual(([r["user"] for r in self.file()["signins"]], self.file()["failures"]), (["friend"], []))

    def test_a_shorter_setting(self):
        h = self.history(days=7)
        h.checked("admin", A, True)
        self.at(7 * DAY + 1)
        self.assertEqual(h.recent(None)["signins"], [])
        self.assertEqual(h.recent(None)["days"], 7)

    def test_old_lines_leave_the_file_when_the_panel_starts(self):
        self.history().checked("admin", A, True)
        self.at(31 * DAY)
        h = self.history()
        self.assertEqual(h.recent(None)["signins"], [])
        self.assertEqual(self.file()["signins"], [])


class File(HistoryTest):
    def test_nothing_on_disk_until_a_file_is_named(self):
        """Imported, as the tests do, the panel keeps the history in memory only."""
        h = self.history(keep=False)
        h.checked("admin", A, True)
        h.checked(None, A, False)
        self.assertEqual(os.listdir(self.dir), [])
        self.assertEqual(self.writes, [])
        self.assertFalse(h.recent(None)["on_disk"])

    def test_written_for_the_panel_only(self):
        h = self.history()
        self.assertFalse(os.path.exists(self.path))  # nothing yet: no file
        h.checked("admin", V6, True)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(os.listdir(self.dir), [signins.FILE])  # no temporary file left
        self.assertEqual(self.file(), {"version": 1, "failures": [],
                                       "signins": [{"at": NOW, "last": NOW, "user": "admin", "address": V6}]})
        self.assertTrue(h.recent(None)["on_disk"])

    def test_only_the_time_the_account_the_address_and_the_result(self):
        h = self.history()
        h.checked("admin", A, True)
        h.checked(None, B, False, blocked=900)
        self.at(SESSION + 1)
        h.checked("admin", A, True)
        data = self.file()
        self.assertEqual(set(data), {"version", "signins", "failures"})
        self.assertEqual({k for r in data["signins"] for k in r}, {"at", "last", "user", "address"})
        self.assertEqual({k for r in data["failures"] for k in r},
                         {"at", "last", "user", "address", "count", "blocked"})

    def test_kept_across_a_restart_of_the_panel(self):
        """The lines come back, and a page left open does not count as a new sign-in after the restart."""
        h = self.history()
        h.checked("admin", A, True)
        h.checked(None, B, False)
        self.at(25 * 60)
        h.checked("admin", A, True)
        self.at(40 * 60)
        h.checked("admin", A, True)  # moves "last": written within 10 minutes
        self.at(50 * 60)
        h.checked("admin", A, True)
        self.at(50 * 60 + 20)  # the panel restarts
        h = self.history()
        self.assertEqual(self.signins(h), [("admin", A, 0)])
        self.assertEqual(self.failures(h), [(None, B, 0, 0, 1, 0)])
        self.at(51 * 60)
        h.checked("admin", A, True)  # the open page asks again
        self.at(51 * 60 + 1)
        h.checked("admin", B, True)  # someone else is a new sign-in
        self.assertEqual(self.signins(h), [("admin", A, 0), ("admin", B, 51 * 60 + 1)])

    def test_a_broken_file_starts_empty(self):
        for content in ('{"signins": [', "[]", '"text"', "{" * 100000, "\xff\xfe", ""):
            with self.subTest(content[:20]):
                with open(self.path, "w", encoding="latin-1") as f:
                    f.write(content)
                with self.assertLogs("test_signins", "WARNING") as logs:
                    h = self.history()
                self.assertIn("the sign-ins start empty", logs.output[0])
                self.assertEqual(h.recent(None)["signins"], [])
                h.checked("admin", A, True)  # the next write replaces it
                self.assertEqual(len(self.file()["signins"]), 1)
                os.remove(self.path)

    def test_a_folder_in_its_place(self):
        os.mkdir(self.path)
        with self.assertLogs("test_signins", "WARNING") as logs:
            h = self.history()
            h.checked("admin", A, True)  # cannot be written: kept in memory, never an error
            h.checked("admin", B, True)
        self.assertEqual(len(h.recent(None)["signins"]), 2)
        self.assertEqual(len([line for line in logs.output if "could not be written" in line]), 1)  # once

    def test_lines_that_cannot_be_read_are_left_out(self):
        good = {"at": NOW, "last": NOW + 5, "user": "admin", "address": A}
        bad = [{"at": "x", "user": "admin"}, {"at": NOW, "user": ""}, {"at": NOW, "user": 7}, {"at": -1, "user": "a"},
               {"at": True, "user": "a"}, {"at": NOW, "user": "a" * 256}, ["admin"], None]
        failure = {"at": NOW, "last": NOW, "user": None, "address": "2001:DB8::7", "count": 3, "blocked": 900}
        with open(self.path, "w") as f:
            json.dump({"version": 1, "signins": [good] + bad, "failures": [failure, dict(failure, count=0),
                                                                          dict(failure, user="")]}, f)
        h = self.history()
        self.assertEqual(self.signins(h), [("admin", A, 0)])
        self.assertEqual(self.failures(h), [(None, V6, 0, 0, 3, 900)])
        self.assertEqual(len(self.writes), 1)  # written again at once, without them
        self.assertEqual(self.file()["failures"][0]["address"], V6)

    def test_an_address_is_read_again(self):
        """A line with an address that is not one keeps its line, as an invalid address."""
        with open(self.path, "w") as f:
            json.dump({"version": 1, "signins": [{"at": NOW, "user": "admin", "address": "<b>x</b>"}],
                       "failures": []}, f)
        self.assertEqual(self.signins(self.history()), [("admin", None, 0)])


class Writes(HistoryTest):
    def test_a_sign_in_is_written_at_once(self):
        h = self.history()
        for i in range(5):
            h.checked("admin", "192.0.2.%d" % i, True)
        self.assertEqual(len(self.writes), 5)

    def test_failures_at_most_once_a_minute(self):
        """An attack: 1000 wrong passwords from 1000 addresses in 10 minutes make 10 writes, not 1000."""
        h = self.history()
        for i in range(1000):
            self.at(i * 0.6)
            h.checked(None, "2001:db8::%x" % i, False)
        self.assertEqual(len(self.writes), 10)
        self.assertEqual(len(self.file()["failures"]), KEEP)

    def test_a_forgotten_tab_every_10_minutes(self):
        """The same old password every minute: its count is written every 10 minutes."""
        h = self.history()
        for t in range(0, 3600 + 1, 60):
            self.at(t)
            h.checked("admin", A, False)
        self.assertEqual(len(self.writes), 7)
        self.assertEqual(self.file()["failures"][0]["count"], 61)

    def test_an_open_page_every_10_minutes(self):
        h = self.history()
        for t in range(0, 3600 + 1, 5):
            self.at(t)
            h.checked("admin", A, True)
        self.assertEqual(len(self.writes), 7)
        self.assertEqual(self.file()["signins"][0]["last"], NOW + 3600)

    def test_what_waits_is_written_by_the_next_request(self):
        h = self.history()
        h.checked(None, A, False)  # written at once: nothing written for a minute before it
        self.at(10)
        h.checked(None, B, False)  # waits for the minute
        self.assertEqual(len(self.writes), 1)
        self.at(60)
        h.checked("admin", A, False)
        self.assertEqual(len(self.writes), 2)
        self.assertEqual(len(self.file()["failures"]), 3)

    def test_a_block_is_written_within_the_minute(self):
        h = self.history()
        h.checked("admin", A, False)
        self.at(30)
        h.checked("admin", A, False, blocked=900)
        self.at(60)
        h.checked(None, B, False)
        self.assertEqual(self.file()["failures"][0]["blocked"], 900)

    def test_nothing_new_nothing_written(self):
        h = self.history()
        h.checked("admin", A, True)
        for t in range(1, 50):
            self.at(t)
            h.checked("admin", A, True)
            h.recent(A)
        self.assertEqual(len(self.writes), 1)

    def test_the_clock_going_back(self):
        h = self.history()
        h.checked(None, A, False)
        self.at(-3600)  # an hour back
        h.checked(None, B, False)
        self.at(-3540)
        h.checked("admin", A, False)
        self.assertEqual(len(self.writes), 2)  # a minute later, not an hour
        h.checked("admin", A, True)
        self.assertEqual(len(self.signins(h)), 1)


class MemoryOnly(HistoryTest):
    """SIGNIN_HISTORY_DAYS = 0: nothing on disk; kept until the panel restarts."""

    def test_nothing_is_written(self):
        h = self.history(days=0)
        h.checked("admin", A, True)
        h.checked(None, B, False, blocked=900)
        self.at(400 * DAY)
        h.checked("friend", A, True)
        self.assertEqual(os.listdir(self.dir), [])
        self.assertEqual(self.writes, [])
        found = h.recent(A)
        self.assertEqual((len(found["signins"]), len(found["failures"])), (2, 1))  # no age in memory
        self.assertEqual((found["days"], found["on_disk"]), (0, False))

    def test_a_file_of_an_earlier_setting_is_removed(self):
        self.history().checked("admin", A, True)
        self.assertTrue(os.path.exists(self.path))
        with self.assertLogs("test_signins", "WARNING") as logs:
            h = self.history(days=0)
        self.assertIn("removed", logs.output[0])
        self.assertEqual(os.listdir(self.dir), [])
        self.assertEqual(h.recent(None)["signins"], [])  # not read back


class Recent(HistoryTest):
    def test_the_newest_ten_of_each(self):
        h = self.history()
        for i in range(25):
            self.at(i)
            h.checked("admin", "192.0.2.%d" % i, True)
            h.checked(None, "198.51.100.%d" % i, False)
        found = h.recent("::ffff:192.0.2.24")
        self.assertEqual([r["at"] - NOW for r in found["signins"]], list(range(24, 14, -1)))
        self.assertEqual([r["last"] - NOW for r in found["failures"]], list(range(24, 14, -1)))
        self.assertEqual((found["signins_total"], found["failures_total"]), (25, 25))
        self.assertEqual(found["your_address"], "192.0.2.24")
        self.assertEqual(set(found["signins"][0]), {"at", "user", "address"})
        self.assertEqual(set(found["failures"][0]), {"at", "last", "user", "address", "count", "blocked"})
        self.assertEqual({k: found[k] for k in ("days", "on_disk", "kept", "session", "group")},
                         {"days": 30, "on_disk": True, "kept": 100, "session": 30, "group": 10})

    def test_failures_by_their_last_try(self):
        h = self.history()
        h.checked(None, A, False)
        self.at(5)
        h.checked(None, B, False)
        self.at(9)
        h.checked(None, A, False)
        self.assertEqual([r["address"] for r in h.recent(None)["failures"]], [A, B])


class NeverRaises(HistoryTest):
    def test_a_problem_is_logged_once_and_the_login_goes_on(self):
        h = self.history()
        h.clock = mock.Mock(side_effect=RuntimeError("broken clock"))
        with self.assertLogs("test_signins", "WARNING") as logs:
            for _ in range(5):
                self.assertIsNone(h.checked("admin", A, True))
        self.assertEqual(len(logs.output), 1)
        self.assertIn("a login could not be noted (RuntimeError('broken clock'", logs.output[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
