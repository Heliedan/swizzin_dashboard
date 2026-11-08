"""Layouts of the dashboard: Classic (the cards) or Overview, chosen by each account in its menu (see the README).

The choices are kept by the panel in a small JSON file, {"user": "layout"}, next to swizzin.cfg: the panel's own
folder belongs to its account, and neither git nor the panel's updates touch a file they do not track. A later version
may keep more for an account, as an object with its layout under "layout" ({"user": {"layout": "overview", ...}}):
this version reads that layout, so that going back to it keeps the account's choice.
Reading never fails: a missing, unreadable or broken file, or a name this version does not know, gives the default
(DASHBOARD_LAYOUT in swizzin.cfg), and the dashboard shows. The file is read and written by core/statefile.py: through
a temporary file renamed over the old one, readable by the panel's account only, one choice at a time."""
import logging

from core import statefile

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


class Choices(statefile.StateFile):
    """The layout chosen by each account, in the JSON file `path`; `default` for the others."""

    def __init__(self, path, default=DEFAULT):
        super().__init__(path)
        self.default = default

    def get(self, user):
        """The layout of `user`, or the default. Never raises: whatever the file holds, the dashboard must show.
        The choice is a layout's name, or an object with that name under "layout" (written by a later version)."""
        name = self.load(log, "the default layout is used").get(user)
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
            choices = self.load(log, "it is written again")
            choices[user] = name
            self.write(choices)
