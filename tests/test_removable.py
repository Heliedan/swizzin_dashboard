"""The table of uninstallable apps (store-helper/removable.py) checked against Swizzin's own scripts.

Needs a copy of Swizzin at the audited commit: SWIZZIN_SRC=/path/to/swizzin (a clone of
github.com/swizzin/swizzin), or /etc/swizzin when it is readable. Skipped otherwise.
Run: SWIZZIN_SRC=... /opt/.venv/swizzin/bin/python tests/test_removable.py
"""
import hashlib
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "store-helper"))
import removable  # noqa: E402

SRC = os.environ.get("SWIZZIN_SRC") or "/etc/swizzin"
HAVE_SRC = os.access(os.path.join(SRC, "scripts", "remove"), os.R_OK)

CATEGORIES = {
    "copied": "settings or data, saved in the copy before (a program folder that holds them included)",
    "partial": "settings and databases copied; caches, posters and metadata left out (the app rebuilds them)",
    "program": "program files, put back by a reinstall",
    "venv": "the app's own Python environment",
    "unit": "the app's systemd unit, init or environment file",
    "nginx": "the app's nginx location",
    "lock": "Swizzin's lock file of the app",
    "fail2ban": "the app's own fail2ban filter and jail",
    "apt": "the app's own apt source",
    "legacy": "a path of an older version of the app",
    "if-empty": "deleted only when empty",
    "misses": "a wrong path: deletes nothing (the runner deletes the right unit file itself)",
}

# Every other way a remove script deletes something (packages, users, Swizzin's db, npm...), and why it is
# acceptable. The campaign found the first one missing from the table (Flood's npm package): never again unseen.
OTHER_CATEGORIES = {
    "package": "the app's own packages: listed in packages, the simulation and allowed limit what goes with them",
    "user": "the app's own system user (its home is the app's folder, in gone or copy)",
    "db": "the app's entries in Swizzin's db (/var/lib/swizzin/db), copied when they hold settings",
    "program": "the app's program, installed by its installer: its folders are in gone",
    "apt-source": "the app's own apt source, added by its installer: in gone",
}
OTHER_AUDIT = {
    "airsonic": {"deluser airsonic --system --quiet": "user"},
    "calibreweb": {"swizdb clear calibre/library_path": "db", "swizdb clear calibre/library_user": "db",
                   'userdel calibreweb -f -r >> "$log" 2>&1': "user"},
    "emby": {"dpkg -r emby-server > /dev/null 2>&1": "package"},
    "flood": {'npm -g remove flood >> "$log" 2>&1': "program"},
    "lidarr": {'swizdb clear "lidarr/owner"': "db"},
    "mylar": {"swizdb clear mylar/owner": "db"},
    "plex": {"apt_remove --purge plexmediaserver": "package", "dpkg -r plexmediaserver > /dev/null 2>&1": "package",
             "userdel plex > /dev/null 2>&1": "user"},
    "prowlarr": {'swizdb clear "$app_name/owner"': "db"},
    "qbittorrent": {"apt_remove --purge ^libtorrent-rasterbar*": "package",
                    "dpkg -r ${swizz_dep} > /dev/null 2>&1": "package",
                    "dpkg -r libtorrent-rasterbar > /dev/null 2>&1": "package",
                    "dpkg -r python-libtorrent > /dev/null 2>&1": "package",
                    "dpkg -r python3-libtorrent > /dev/null 2>&1": "package",
                    "dpkg -r qbittorrent-nox > /dev/null 2>&1": "package"},
    "syncthing": {"apt_remove --purge syncthing": "package"},
    "transmission": {"add-apt-repository --remove ppa:transmissionbt/ppa -y >> $log 2>&1": "apt-source",
                     "apt_remove --purge transmission-common transmission-cli transmission-daemon": "package"},
}

# Every rm of every remove script, as written in it, and why it is acceptable. A changed script fails
# the pins first; this list makes sure each deletion was looked at when an entry was added.
AUDIT = {
    "airsonic": {
        "rm -rf /opt/airsonic": "copied",
        "rm /etc/nginx/apps/airsonic.conf": "nginx",
        "rm /install/.airsonic.lock": "lock",
    },
    "autobrr": {
        "rm -rf /home/${user}/.config/autobrr": "copied",
        "rm -f /etc/systemd/system/autobrr@.service": "unit",
        "rm -f /usr/bin/autobrr": "program",
        "rm -f /usr/bin/autobrrctl": "program",
        "rm -f /etc/nginx/apps/autobrr.conf": "nginx",
        "rm -f /etc/nginx/conf.d/*.autobrr.conf": "nginx",
        "rm /install/.autobrr.lock": "lock",
    },
    "bazarr": {
        "rm -rf /etc/systemd/system/bazarr.service": "unit",
        "rm -rf /opt/bazarr": "copied",
        "rm -rf /opt/.venv/bazarr": "venv",
        "rm -rf /opt/.venv": "if-empty",
        "rm -rf /etc/nginx/apps/bazarr.conf": "nginx",
        "rm -rf /install/.bazarr.lock": "lock",
    },
    "calibreweb": {
        "rm -rf /opt/.venv/calibreweb": "venv",
        "rm -rf /opt/calibreweb": "copied",
        "rm /etc/systemd/system/calibreweb.service": "unit",
        "rm /etc/nginx/apps/calibreweb.conf": "nginx",
        "rm /install/.calibreweb.lock": "lock",
    },
    "emby": {
        "rm /etc/apt/sources.list.d/emby-server.list": "apt",
        "rm -rf /etc/nginx/apps/emby.conf": "nginx",
        "rm -rf /install/.emby.lock": "lock",
    },
    "filebrowser": {
        'rm -f "/etc/systemd/system/filebrowser.service"': "unit",
        'rm -f "/home/${username}/bin/filebrowser"': "program",
        'rm -rf "/home/${username}/.config/Filebrowser"': "copied",
        'rm -f "/etc/nginx/apps/filebrowser.conf"': "nginx",
        'rm -f "/install/.filebrowser.lock"': "lock",
    },
    "flood": {
        "rm -rf /home/${user}/.config/flood": "copied",
        "rm -rf /home/${user}/.flood": "copied",
        "rm -f /etc/nginx/apps/flood.conf": "nginx",
        "rm -f /etc/nginx/conf.d/*.flood.conf": "nginx",
        "rm -f /etc/systemd/system/flood@.service": "unit",
        "rm -f /install/.flood.lock": "lock",
    },
    "jackett": {
        "rm -r /home/$username/Jackett": "program",
        "rm /install/.jackett.lock": "lock",
        "rm -r /home/${username}/.config/Jackett": "copied",
        "rm /etc/systemd/system/jackett@.service": "unit",
        "rm -f /etc/nginx/apps/jackett.conf": "nginx",
        "rm /etc/init.d/jackett": "legacy",
    },
    "librespeed": {
        "sudo rm -r /srv/librespeed": "program",
        "sudo rm /etc/nginx/apps/librespeed.conf": "nginx",
        "sudo rm /install/.librespeed.lock": "lock",
    },
    "lidarr": {
        "rm -rf /opt/Lidarr": "program",
        "rm -rf /install/.lidarr.lock": "lock",
        "rm -rf /etc/systemd/system/lidarr.service": "unit",
        "rm -rf /etc/nginx/apps/lidarr.conf": "nginx",
    },
    "medusa": {
        "sudo rm /etc/nginx/apps/medusa.conf > /dev/null 2>&1": "nginx",
        "sudo rm /etc/systemd/medusa.service > /dev/null 2>&1": "misses",
        "rm -rf /opt/medusa": "copied",
        "rm -rf /opt/.venv/medusa": "venv",
        "rm -rf /opt/.venv": "if-empty",
        "sudo rm /install/.medusa.lock": "lock",
    },
    "mylar": {
        "rm_if_exists /etc/systemd/system/mylar.service": "unit",
        "rm_if_exists /opt/mylar": "program",
        "rm_if_exists /opt/.venv/mylar": "venv",
        "rm_if_exists /install/.mylar.lock": "lock",
        'rm_if_exists "/home/$(swizdb get mylar/owner)/.config/mylar"': "copied",
        "rm_if_exists /etc/nginx/apps/mylar.conf": "nginx",
    },
    "netronome": {
        "rm -rf /home/${user}/.config/netronome": "copied",
        "rm -f /etc/systemd/system/netronome@.service": "unit",
        "rm -f /usr/bin/netronome": "program",
        "rm -f /etc/nginx/apps/netronome.conf": "nginx",
        "rm -f /etc/nginx/conf.d/*.netronome.conf": "nginx",
        "rm /install/.netronome.lock": "lock",
    },
    "nzbhydra": {
        "rm_if_exists /opt/nzbhydra": "legacy",
        "rm_if_exists /home/${user}/.config/nzbhydra": "legacy",
        "rm_if_exists /opt/.venv/nzbhydra": "legacy",
        "rm -rf /opt/.venv": "if-empty",
        "rm_if_exists /opt/nzbhydra2": "program",
        "rm_if_exists /home/${user}/.config/nzbhydra2": "copied",
        "rm /etc/systemd/system/nzbhydra.service": "unit",
        "rm -f /etc/nginx/apps/nzbhydra.conf": "nginx",
        "rm /install/.nzbhydra.lock": "lock",
    },
    "organizr": {
        "rm -rf /srv/organizr": "copied",
        "rm -rf /srv/organizr_db": "copied",
        "rm -rf /etc/nginx/apps/organizr.conf": "nginx",
        "rm -rf /install/.organizr.lock": "lock",
        "rm /etc/fail2ban/filter.d/organizr-auth.conf": "fail2ban",
        "rm /etc/fail2ban/jail.d/organizr-auth.conf": "fail2ban",
    },
    "plex": {
        "rm -f /etc/systemd/system/plexmediaserver.service": "unit",
        "rm -rf /var/lib/plexmediaserver": "partial",
        "rm -rf /usr/lib/plexmediaserver": "program",
        "rm /etc/init/plexmediaserver.conf > /dev/null 2>&1": "legacy",
        "rm /etc/default/plexmediaserver > /dev/null 2>&1": "unit",
        "rm /install/.plex.lock": "lock",
    },
    "prowlarr": {
        'rm /etc/systemd/system/"$app_servicefile"': "unit",
        'rm -rf "$app_dir"': "program",
        'rm -rf "$app_configdir"': "copied",
        'rm "/etc/nginx/apps/$app_name.conf"': "nginx",
        'rm "/install/.$app_lockname.lock"': "lock",
    },
    "qbittorrent": {
        "rm -rf /home/${user}/.config/qbittorrent": "misses",
        "rm -f /etc/systemd/system/qbittorrent@.service": "unit",
        "rm -f /etc/nginx/apps/qbittorrent.conf": "nginx",
        "rm -f /etc/nginx/conf.d/*.qbittorrent.conf": "nginx",
        "rm /install/.qbittorrent.lock": "lock",
    },
    "qui": {
        "rm -rf /home/${user}/.config/qui": "copied",
        "rm -f /etc/systemd/system/qui@.service": "unit",
        "rm -f /usr/bin/qui": "program",
        "rm -f /etc/nginx/apps/qui.conf": "nginx",
        "rm -f /etc/nginx/conf.d/*.qui.conf": "nginx",
        "rm /install/.qui.lock": "lock",
    },
    "radarr": {
        "rm /etc/systemd/system/radarr.service": "unit",
        "rm -rf /opt/Radarr": "program",
        "rm /etc/nginx/apps/radarr.conf": "nginx",
        "rm /install/.radarr.lock": "lock",
    },
    "sickgear": {
        "sudo rm /etc/nginx/apps/sickgear.conf > /dev/null 2>&1": "nginx",
        "sudo rm /etc/systemd/sickgear.service > /dev/null 2>&1": "misses",
        "sudo rm /install/.sickgear.lock": "lock",
        "rm -rf /opt/sickgear": "copied",
        "rm -rf /opt/.venv/sickgear": "venv",
        "rm -rf /opt/.venv": "if-empty",
        "rm -f /install/.sickgear.lock": "lock",
    },
    "syncthing": {
        "rm /etc/systemd/system/syncthing@.service": "unit",
        "rm -f /etc/nginx/apps/syncthing.conf": "nginx",
        "rm /install/.syncthing.lock": "lock",
    },
    "tautulli": {
        "rm -rf /opt/tautulli": "copied",
        "rm -rf /opt/.venv/tautulli": "venv",
        "rm /install/.tautulli.lock": "lock",
        "rm -f /etc/nginx/apps/tautulli.conf": "nginx",
        "rm /etc/systemd/system/tautulli.service": "unit",
    },
    "transmission": {
        "rm -f /home/${u}/.config/transmission-daemon/settings.json": "copied",
        "rm /etc/systemd/system/transmission@.service": "unit",
        'rm /etc/nginx/apps/transmission.conf >> "$log" 2>&1': "nginx",
        'rm /etc/nginx/conf.d/*.transmission.conf >> "$log" 2>&1': "nginx",
        "rm /install/.transmission.lock": "lock",
    },
}

RM = re.compile(r'(^|[;&|(\s])(sudo\s+)?(rm|rm_if_exists)\s')
OTHER = re.compile(r'(^|[;&|(\s])(sudo\s+)?(npm\s.*\s(remove|uninstall|rm)\s|pip3?\s+uninstall|apt_remove|'
                   r'apt-get\s.*(remove|purge)|apt\s+(remove|purge)|dpkg\s+(-r|-P|--purge|--remove)|userdel|deluser|'
                   r'delgroup|groupdel|swizdb\s+clear|find\s.*-delete|unlink\s|rmdir\s|mv\s|'
                   r'add-apt-repository\s.*--remove|truncate\s)')
SOURCED = re.compile(r'^\s*(?:\.|source)\s+/etc/swizzin/(\S+)')
# Never deleted by the App Store, nor any folder above them.
PROTECTED = ("/", "/home", "/home/{user}", "/home/{master}", "/home/{owner}", "/home/{user}/.config",
             "/home/{master}/.config", "/home/{owner}/.config", "/home/{user}/torrents", "/home/{user}/Downloads",
             "/opt", "/opt/.venv", "/opt/swizzin", "/opt/.venv/swizzin", "/srv", "/root", "/etc", "/etc/nginx",
             "/etc/nginx/apps", "/etc/nginx/conf.d", "/etc/systemd/system", "/usr/bin", "/var/lib", "/install",
             "/var/lib/swizzin", "/var/lib/swizzin/db")


def read(rel):
    with open(os.path.join(SRC, rel), "rb") as f:
        return f.read()


def code_lines(rel):
    return [line.strip() for line in read(rel).decode().splitlines() if line.strip() and not line.strip().startswith("#")]


class Table(unittest.TestCase):
    """Checks that need no Swizzin."""

    def test_excluded_apps_are_not_offered(self):
        self.assertFalse(set(removable.APPS) & set(removable.EXCLUDED))

    def test_every_loaded_file_has_a_pin(self):
        for app in removable.APPS:
            for rel in removable.pinned_files(app):
                self.assertIn(rel, removable.PINS, app)
                self.assertTrue(all(re.fullmatch(r"[0-9a-f]{64}", h) for h in removable.PINS[rel]), rel)

    def test_no_protected_path_is_deleted_or_copied_whole(self):
        for app, entry in removable.APPS.items():
            for path in entry.gone + entry.nginx + entry.leftovers + entry.copy:
                self.assertNotIn(path.rstrip("/"), PROTECTED, app)
                self.assertTrue(path.startswith("/") and ".." not in path.split("/"), path)
                self.assertNotIn("*", os.path.dirname(path), path)  # a * matches inside its folder only

    def test_answers_and_allowed_packages_go_together(self):
        for app, entry in removable.APPS.items():
            self.assertEqual(bool(entry.packages), bool(entry.allowed), app)
            for regex in entry.allowed:
                re.compile(regex)


@unittest.skipUnless(HAVE_SRC, "no copy of Swizzin (set SWIZZIN_SRC)")
class AgainstSwizzin(unittest.TestCase):
    """The table matches Swizzin at the audited commit."""

    def test_pins_match_swizzin(self):
        for rel, hashes in removable.PINS.items():
            self.assertIn(hashlib.sha256(read(rel)).hexdigest(), hashes, rel)

    def test_globals_loads_exactly_the_common_files(self):
        loaded = [m.group(1) for line in code_lines("sources/globals.sh") for m in [SOURCED.match(line)] if m]
        self.assertEqual(["sources/globals.sh"] + loaded, list(removable.COMMON))

    def test_each_script_loads_exactly_its_listed_sources(self):
        for app, entry in removable.APPS.items():
            loaded = [m.group(1) for line in code_lines(removable.script(app)) for m in [SOURCED.match(line)] if m]
            self.assertEqual(tuple(loaded), entry.sources, app)

    def test_every_rm_was_reviewed(self):
        self.assertEqual(set(AUDIT), set(removable.APPS))
        for app in removable.APPS:
            found = [line for line in code_lines(removable.script(app)) if RM.search(line)]
            self.assertEqual(sorted(set(found)), sorted(AUDIT[app]), app)
            for line, category in AUDIT[app].items():
                self.assertIn(category, CATEGORIES, (app, line))
            if "partial" in AUDIT[app].values():
                self.assertTrue(removable.APPS[app].partial, app)

    def test_every_other_deletion_was_reviewed(self):
        for app in removable.APPS:
            found = sorted(set(line for line in code_lines(removable.script(app)) if OTHER.search(line)))
            self.assertEqual(found, sorted(OTHER_AUDIT.get(app, {})), app)
            for line, category in OTHER_AUDIT.get(app, {}).items():
                self.assertIn(category, OTHER_CATEGORIES, (app, line))
                if category == "package":
                    self.assertTrue(removable.APPS[app].packages, (app, line))

    def test_removable_apps_can_be_installed(self):
        for app in removable.APPS:
            self.assertTrue(os.path.isfile(os.path.join(SRC, "scripts", "install", app + ".sh")), app)

    def test_answers_are_questions_of_the_script(self):
        for app, entry in removable.APPS.items():
            text = read(removable.script(app)).decode()
            for question, answer in entry.answers:
                self.assertIn(question, text, app)
                self.assertIn(answer, ("y", "n"), app)

    def test_units_are_the_apps_own(self):
        for app, entry in removable.APPS.items():
            text = read(removable.script(app)).decode() + read("scripts/install/%s.sh" % app).decode()
            for unit in entry.units:
                self.assertIn(unit.rstrip("@"), text, app)


if __name__ == "__main__":
    unittest.main()
