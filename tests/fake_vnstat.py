"""A fake vnstat for the tests of the bandwidth card, and made-up data in vnstat 2's JSON (version 2).

FakeVnstat writes a small program named vnstat in a folder of its own, to put first in PATH: it prints what say()
gave it, notes each call (calls()) and its process id (pid()). The real vnstat of the machine is never run."""
import datetime
import json
import os
import sys
import tempfile

GB = 10 ** 9


def entry(id_, date, rx, tx, **more):
    """One entry of a vnstat 2 list (JSON version 2): id is its row in vnstat's database, date (year, month[, day])."""
    keys = ("year", "month", "day")
    return dict({"id": id_, "date": dict(zip(keys, date)), "rx": rx, "tx": tx}, **more)


def traffic():
    """What vnstat 2.12 prints for an interface, oldest entries first, with made-up numbers. 70 days (vnstat keeps
    62 by default, more after a change of its settings), 30 months, 3 hours, and a top list of its own."""
    last = datetime.date(2026, 11, 7)
    days = []
    for i in range(70):
        d = last - datetime.timedelta(days=69 - i)
        # 1 to 70 GB received, in a mixed order (37 and 70 have no common divisor), 0.1 to 5 GB sent
        days.append(entry(i + 1, (d.year, d.month, d.day), ((i * 37) % 70 + 1) * GB, ((i * 13) % 50 + 1) * GB // 10))
    months = []
    for i in range(30):
        year, month = divmod(2024 * 12 + 5 + i, 12)  # June 2024 to November 2026
        months.append(entry(i + 1, (year, month + 1), (i + 1) * 100 * GB, (i % 7 + 1) * 10 * GB))
    hours = [entry(101, (2026, 11, 7), 900 * 10 ** 6, 300 * 10 ** 6, time={"hour": 21, "minute": 0}),
             entry(102, (2026, 11, 7), 1500 * 10 ** 6, 250 * 10 ** 6, time={"hour": 22, "minute": 0}),
             entry(103, (2026, 11, 7), 420 * 10 ** 6, 70 * 10 ** 6, time={"hour": 23, "minute": 0})]
    # vnstat's own top list, the largest first: days older than the list of days, larger than any of them
    top = [entry(3, (2026, 3, 14), 900 * GB, 100 * GB), entry(2, (2026, 1, 2), 512 * GB, 8 * GB),
           entry(1, (2025, 12, 25), 300 * GB, 33 * GB), entry(4, (2026, 6, 2), 70 * GB, 4 * GB),
           entry(5, (2026, 7, 19), 69 * GB, 3 * GB), entry(6, (2026, 8, 3), 68 * GB, 2 * GB)]
    return {"total": {"rx": 123456789012345, "tx": 98765432109876},
            "fiveminute": [], "hour": hours, "day": days, "month": months, "year": [], "top": top}


def vnstat_json(traffic_, version="2"):
    return json.dumps({"vnstatversion": "2.12", "jsonversion": version,
                       "interfaces": [{"name": "eth0", "alias": "", "traffic": traffic_}]})


EMPTY = {"total": {"rx": 0, "tx": 0}, "fiveminute": [], "hour": [], "day": [], "month": [], "year": [], "top": []}
NOT_IN_DATABASE = 'Error: No interface matching "eth0" found in database.\n'  # vnstat 2.9 to 2.12, exit code 1


class FakeVnstat:
    """A program named vnstat, in a folder put first in PATH. say() sets what it prints and its exit code."""

    def __init__(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "vnstat")
        with open(self.path, "w") as f:
            f.write(f"""#!{sys.executable}
import json, os, sys, time
d = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(d, "calls"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
with open(os.path.join(d, "pid"), "w") as f:
    f.write(str(os.getpid()))
with open(os.path.join(d, "answer.json")) as f:
    answer = json.load(f)
time.sleep(answer["sleep"])
sys.stdout.write(answer["stdout"])
sys.stderr.write(answer["stderr"])
sys.exit(answer["code"])
""")
        os.chmod(self.path, 0o755)
        self.say(vnstat_json(traffic()))

    def say(self, stdout="", stderr="", code=0, sleep=0):
        with open(os.path.join(self.dir, "answer.json"), "w") as f:
            json.dump({"stdout": stdout, "stderr": stderr, "code": code, "sleep": sleep}, f)

    def calls(self):
        try:
            with open(os.path.join(self.dir, "calls")) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def pid(self):
        with open(os.path.join(self.dir, "pid")) as f:
            return int(f.read())
