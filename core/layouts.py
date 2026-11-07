"""Layouts of the dashboard: Classic (the cards) or Overview, chosen by each account in its menu (see the README).

The choices are kept by the panel in a small JSON file, {"user": "layout"}, next to swizzin.cfg: the panel's own
folder belongs to its account, and neither git nor the panel's updates touch a file they do not track. A later version
may keep more for an account, as an object with its layout under "layout" ({"user": {"layout": "overview", ...}}):
this version reads that layout, so that going back to it keeps the account's choice.
Reading never fails: a missing, unreadable or broken file, or a name this version does not know, gives the default
(DASHBOARD_LAYOUT in swizzin.cfg), and the dashboard shows. Writing goes through a temporary file renamed over the
old one, so that nobody ever reads half a file; it is readable by the panel's account only. The panel runs in one
process (python swizzin.py): a lock keeps two choices made at the same moment from losing one of them."""
import json
import logging
import os
import tempfile
import threading

LAYOUTS = {"classic": "Classic", "overview": "Overview"}  # name: label in the account menu, in this order
DEFAULT = "classic"
FILE = "layouts.json"  # in the panel's folder, next to swizzin.cfg

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


class Choices:
    """The layout chosen by each account, in the JSON file `path`; `default` for the others."""

    def __init__(self, path, default=DEFAULT):
        self.path = path
        self.default = default
        self.lock = threading.Lock()  # green under eventlet (swizzin.py patches threading): one write at a time

    def _read(self):
        """{user: layout} as written; {} when there is no file yet. Raises when it cannot be read, or is not an
        object."""
        try:
            with open(self.path, encoding="utf-8") as f:
                choices = json.load(f)
        except FileNotFoundError:
            return {}
        if not isinstance(choices, dict):
            raise ValueError("not a JSON object")
        return choices

    def get(self, user):
        """The layout of `user`, or the default. Never raises: whatever the file holds, the dashboard must show.
        The choice is a layout's name, or an object with that name under "layout" (written by a later version)."""
        try:
            name = self._read().get(user)
        except Exception as e:  # unreadable, broken JSON, a folder in its place, nested too deep...
            log.warning("%s cannot be read (%s): the default layout is used", self.path, e)
            return self.default
        if isinstance(name, dict):
            name = name.get("layout")
        return name if known(name) else self.default

    def set(self, user, name):
        """Keep `name` as the layout of `user`. ValueError for a name that is not a layout; OSError when the file
        cannot be written, the previous one then stays as it was. A broken file is replaced: only the choices that
        could be read are kept."""
        if not known(name):
            raise ValueError("not a layout: %r" % (name,))
        with self.lock:
            try:
                choices = self._read()
            except Exception as e:
                log.warning("%s cannot be read (%s): it is written again", self.path, e)
                choices = {}
            choices[user] = name
            fd, tmp = tempfile.mkstemp(prefix=".layouts-", suffix=".tmp",
                                       dir=os.path.dirname(os.path.abspath(self.path)))  # mode 0600
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(choices, f, indent=1, sort_keys=True)
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
