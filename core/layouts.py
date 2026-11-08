"""Layouts of the dashboard: Classic (the cards) or Overview, chosen by each account in its menu, and the arrangement
of Overview made by each account with Customize (see the README).

The choices are kept by the panel in a small JSON file next to swizzin.cfg: the panel's own folder belongs to its
account, and neither git nor the panel's updates touch a file they do not track. An account's value is the name of
its layout, {"user": "overview"}, unless it arranged Overview: it is then an object, with the layout under "layout"
(when the account chose one) and the arrangement under "overview":
  {"user": {"layout": "overview", "overview": {"order": [...], "hidden": [...], "other_column": [...]}}}
order lists the blocks of core/overview_blocks.py in the order the account gave them (a block it does not list comes
after those of its zone that are listed, in the order of core/overview_blocks.py); hidden, the blocks it hid;
other_column, the sections of Details it put in the other column. An account that never arranged anything keeps its
name: a file written by the previous version is only ever changed for an account that arranges, and choosing Classic
keeps the arrangement for later. Reset to default gives the account its name back.
Reading never fails: a missing, unreadable or broken file, or a name this version does not know, gives the default
(DASHBOARD_LAYOUT in swizzin.cfg), and the dashboard shows. What it cannot read in an arrangement is left out: a key or
an id it does not know (a block a later version added, or one an earlier version had), an id given twice, more than
LIMIT ids. An arrangement marked "v" other than 1 (a later version that changed its meaning) is not read at all. The
file is read and written by core/statefile.py: through a temporary file renamed over the old one, readable by the
panel's account only, one change at a time; a change that changes nothing writes nothing."""
import logging

from core import overview_blocks, statefile

LAYOUTS = {"classic": "Classic", "overview": "Overview"}  # name: label in the account menu, in this order
DEFAULT = "classic"
FILE = "layouts.json"  # in the panel's folder, next to swizzin.cfg
KEYS = ("order", "hidden", "other_column")  # the lists of an arrangement
LIMIT = 64  # ids in each list, at most
OTHER_COLUMN = {"left": "right", "right": "left"}  # the two columns of Details

log = logging.getLogger(__name__)


def known(name):
    return isinstance(name, str) and name in LAYOUTS


def configured(config, logger=log):
    """The layout of the accounts that chose none: DASHBOARD_LAYOUT in swizzin.cfg, read when the panel starts.
    A name that is not a layout falls back to Classic, with a warning in the log: the panel starts anyway."""
    name = config.get("DASHBOARD_LAYOUT", DEFAULT)
    if known(name):
        return name
    logger.warning("swizzin.cfg: DASHBOARD_LAYOUT = %r is not a layout (%s): %s used instead",
                   name, ", ".join(LAYOUTS), DEFAULT)
    return DEFAULT


def _blocks():
    """The blocks of Overview by id, read at each call: a block added to the list is arranged like the others."""
    return {block["id"]: block for block in overview_blocks.BLOCKS}


def _plain(arrangement):
    """The arrangement, or None when it is the default one: nothing hidden, nothing in the other column, and the blocks
    it lists in the order of core/overview_blocks.py (those it does not list then come at their own place too)."""
    order = [block["id"] for block in overview_blocks.BLOCKS if block["id"] in arrangement["order"]]
    if arrangement["hidden"] or arrangement["other_column"] or arrangement["order"] != order:
        return arrangement
    return None


def read(value):
    """The arrangement of Overview found in `value` (an account's "overview" in the file), as this version reads it:
    {"order": [...], "hidden": [...], "other_column": [...]}, or None for the default one. Never raises."""
    if not isinstance(value, dict):
        return None
    version = value.get("v", 1)
    if version != 1 or isinstance(version, bool):
        return None
    blocks = _blocks()
    arrangement = {}
    for key in KEYS:
        items = value.get(key)
        ids = []
        for i in (items[:LIMIT] if isinstance(items, list) else []):
            if isinstance(i, str) and i in blocks and i not in ids and (
                    key != "other_column" or blocks[i]["zone"] in OTHER_COLUMN):
                ids.append(i)
        arrangement[key] = ids
    return _plain(arrangement)


def checked(data):
    """The arrangement sent by Customize (POST /layout/arrange), checked: an object with one or more of "order",
    "hidden" and "other_column", each a list of ids of blocks of core/overview_blocks.py, each once, LIMIT at most; the
    ids of other_column, sections of Details. Returns it with its three lists, or None when it is the default one.
    ValueError, with what is wrong, for anything else."""
    if not isinstance(data, dict) or not data or not set(data) <= set(KEYS):
        raise ValueError("an object with order, hidden or other_column")
    blocks = _blocks()
    arrangement = {}
    for key in KEYS:
        ids = data.get(key, [])
        if not isinstance(ids, list) or len(ids) > LIMIT:
            raise ValueError("%s: a list of %d ids at most" % (key, LIMIT))
        if not all(isinstance(i, str) and i in blocks for i in ids):
            raise ValueError("%s: unknown block" % key)
        if len(set(ids)) != len(ids):
            raise ValueError("%s: a block given twice" % key)
        if key == "other_column" and not all(blocks[i]["zone"] in OTHER_COLUMN for i in ids):
            raise ValueError("other_column: not a section of Details")
        arrangement[key] = list(ids)
    return _plain(arrangement)


def zones(arrangement):
    """Every block of core/overview_blocks.py by zone, in the order of `arrangement` (None: the list's own order):
    {zone: [(block, hidden), ...]} for each zone of ZONES. A section in other_column is in the other column."""
    arrangement = arrangement or {key: [] for key in KEYS}
    rank = {i: n for n, i in enumerate(arrangement["order"])}
    listed = sorted(enumerate(overview_blocks.BLOCKS),
                    key=lambda item: (0, rank[item[1]["id"]]) if item[1]["id"] in rank else (1, item[0]))
    out = {zone: [] for zone in overview_blocks.ZONES}
    for _, block in listed:
        zone = block["zone"]
        if block["id"] in arrangement["other_column"]:
            zone = OTHER_COLUMN.get(zone, zone)
        out[zone].append((block, block["id"] in arrangement["hidden"]))
    return out


class Choices(statefile.StateFile):
    """The layout chosen by each account, and its arrangement of Overview, in the JSON file `path`; `default` for the
    others."""

    def __init__(self, path, default=DEFAULT):
        super().__init__(path)
        self.default = default

    def get(self, user):
        """The layout of `user`, or the default. Never raises: whatever the file holds, the dashboard must show.
        The choice is a layout's name, or an object with that name under "layout" (when the account arranged
        Overview)."""
        name = self.load(log, "the default layout is used").get(user)
        if isinstance(name, dict):
            name = name.get("layout")
        return name if known(name) else self.default

    def arrangement(self, user):
        """The arrangement of Overview of `user` (see read()), or None for the default one. Never raises."""
        value = self.load(log, "Overview shows its blocks in their default order").get(user)
        return read(value.get("overview")) if isinstance(value, dict) else None

    def set(self, user, name):
        """Keep `name` as the layout of `user`, and the arrangement of Overview it may have. ValueError for a name that
        is not a layout; OSError when the file cannot be written, the previous one then stays as it was. A broken file
        is replaced: only the choices that could be read are kept."""
        if not known(name):
            raise ValueError("not a layout: %r" % (name,))
        self._change(user, "layout", name)

    def arrange(self, user, arrangement):
        """Keep `arrangement` (from checked()) as the arrangement of Overview of `user`; None, the default one, removes
        it. OSError as for set()."""
        self._change(user, "overview", arrangement)

    def _change(self, user, key, value):
        """Set (or remove, for None) `key` of the account's object, and keep the account as a name alone when that is
        all it has left. What a later version may have added for the account is kept. Nothing changed, nothing
        written: the file is left as it was."""
        with self.lock:
            choices = self.load(log, "it is written again")
            before = dict(choices)
            old = choices.get(user)
            entry = dict(old) if isinstance(old, dict) else {"layout": old} if isinstance(old, str) else {}
            if value is None:
                entry.pop(key, None)
            else:
                entry[key] = value
            if list(entry) == ["layout"] and isinstance(entry["layout"], str):
                entry = entry["layout"]
            if entry:
                choices[user] = entry
            else:
                choices.pop(user, None)
            if choices != before:
                self.write(choices)
