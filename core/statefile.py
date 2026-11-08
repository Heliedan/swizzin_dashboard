"""The small JSON files the panel keeps for itself next to swizzin.cfg, such as layouts.json (core/layouts.py).

The panel's own folder belongs to its account: the panel writes there without anything created as root. Neither git
nor the panel's updates touch a file they do not track, and .gitignore lists these files; going back to the official
panel (heliedan.sh revert) puts its folder back without them.
Reading never fails: a missing file is an empty object, and a file that cannot be read gives one too, with a warning
in the log. Writing goes through a temporary file renamed over the old one, so that nobody ever reads half a file; it
is readable by the panel's account only. The panel runs in one process (python swizzin.py): a lock, held around a
read, change and write, keeps two changes made at the same moment from losing one of them."""
import json
import os
import tempfile
import threading


class StateFile:
    """A JSON object kept in the file `path`."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()  # green under eventlet (swizzin.py patches threading): one write at a time

    def load(self, logger, then):
        """The object of the file; {} when there is no file yet. Never raises: a file that cannot be read (broken
        JSON, not an object, a folder in its place, no rights, nested too deep...) gives {} too, with a warning in
        `logger` that ends with `then`, what the caller does about it."""
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
            return data
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.warning("%s cannot be read (%s): %s", self.path, e, then)
            return {}

    def write(self, data):
        """Replace the file with the object `data`, mode 0600, through a temporary file next to it (.<name>-*.tmp).
        OSError when it cannot be written: the previous file then stays as it was, and no temporary file is left.
        Hold `lock` from the load to the write when the new object is made from the old one."""
        name = os.path.splitext(os.path.basename(self.path))[0]
        fd, tmp = tempfile.mkstemp(prefix="." + name + "-", suffix=".tmp",
                                   dir=os.path.dirname(os.path.abspath(self.path)))  # mode 0600
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1, sort_keys=True)
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
