"""The apps the App Store may uninstall, and how: root side, read by swizzin_store.py and remove_runner.py.

Each entry was checked by hand against Swizzin's scripts at commit AUDITED (see tests/test_removable.py,
which also lists every `rm` of every script and why it is acceptable). The removal never goes through
`box remove`, which ends with a system-wide `apt-get autoremove` even when the script failed: the runner
loads Swizzin's environment and runs the app's own remove script, after saving the app's settings.

A file of Swizzin that a removal runs or loads must have one of the known sha256 below. When an update of
Swizzin changes one of them, the removal of the apps concerned is turned off until a new version of the
App Store has checked the new file. An app is offered only once it passed the container campaign (tested).

Paths may contain {user} (every user with a home: Swizzin's users, htpasswd users and the users of the
app's unit instances), {master} (Swizzin's master user) and {owner} (the User= of owner_unit, else the
master). A name with * matches inside its folder only.
"""
import json
from dataclasses import dataclass

AUDITED = "1bb1321"

# Loaded through sources/globals.sh by every removal, as box does.
COMMON = ("sources/globals.sh", "sources/functions/color_echo", "sources/functions/os",
          "sources/functions/apt", "sources/functions/ask", "sources/functions/users", "sources/functions/swizdb")

PINS = {
    "sources/globals.sh": ("4af7f07fb1072612f6c9c2b987baaaa10b722853c25b7101c7de5e7a1c9d4b0b",),
    "sources/functions/color_echo": ("551ec748769a9b37952b254b733ee286d16f138c0bfd9a4cbbd95d8c254c2fd2",),
    "sources/functions/os": ("446924c78a93adf52383d9dbb021b6eb502fe617f8a13ad96e257e6a7073a212",),
    "sources/functions/apt": ("c33374c112915d68ca79b08890ba5048cc854a7cc790315aaa37572c4b05ad69",),
    "sources/functions/ask": ("4222a1e98273554ac920421f3f9ebc8e37ec2193ee54aa1b3fc27806d1b618e9",),
    "sources/functions/users": ("d0d49b980cedd5c8f9e941cb24a5bdcb057dd90586f9f1b068aa42623c920c51",),
    "sources/functions/swizdb": ("95ee5d243e009402d73358b10f81da659d7dd247a23f7315dcda3bd40c1103e1",),
    "sources/functions/utils": ("8cb3420b0d491aa20a0545f91e55779e98c43551f3f9ad0a4dd8cf8cc88993f6",),
    "sources/functions/libtorrent": ("41762e9a819df863ca7a58edeb16b33d080eb3a200b3a13f1126411e9ae2eb66",),
    "scripts/remove/airsonic.sh": ("5768cd7de436918401f3a9c2b134e6ecc66742d82bb487b71e68130b1e2ffc5c",),
    "scripts/remove/autobrr.sh": ("c3d313d7f36c2efa5486ff660bbdba24551d68556247c087c718d91d2b6a0a7d",),
    "scripts/remove/bazarr.sh": ("587aa23575143e2c5bf9a3aa0c0c3da3a9a2d8f77c208ba1b5eca717dce94122",),
    "scripts/remove/calibreweb.sh": ("0e9554ca56fe1bda75010b3f4c4ccfe0c6f02b6ff88db1c5c5ddcf3db70b72fd",),
    "scripts/remove/emby.sh": ("0e37876c7e9de465100ef59e6f8d1f475dcb0845ca1ec92f2d4ce529943d449b",),
    "scripts/remove/filebrowser.sh": ("35f34f870dd236960af5f8abc4aa8ae413b552713a982d291b28734300cf6658",),
    "scripts/remove/flood.sh": ("86db2caff11fa7344d7d10c1dfc1dc0bd3cc4fefe3e9f1b5933b55aa86b8a099",),
    "scripts/remove/jackett.sh": ("edc5db191c31afe88979570dca024feab406d797b3c02f88e266d644ba06bc31",),
    "scripts/remove/librespeed.sh": ("56ad1a34d534fcf6a00d0e65fb28bd20a218d8809acaf614d4600457bad97dc7",),
    "scripts/remove/lidarr.sh": ("dafd6b576ad42ac8c7a1661335f31318bf3e8045783c4d04d6b08edcd1655bea",),
    "scripts/remove/medusa.sh": ("3f6aab1b136c784127a457d2c72dd45079a375de8f5eb232da8830ff60e816ce",),
    "scripts/remove/mylar.sh": ("68f22c95598333b4c67d6687f53f726fe1418d0e522286c3d783454621f56f76",),
    "scripts/remove/netronome.sh": ("fddf58cedde6835a64142a332d49fec58c49220400b886882a8656afe3db1e4a",),
    "scripts/remove/nzbhydra.sh": ("ec030dc4387fa3b0cba6317166757fbd97ea987793827289d0a35be603ee8aa5",),
    "scripts/remove/organizr.sh": ("5356a9424d7e9aa89ab6924cf24499a83a464fa8195be4ce4078e81fe1dee3d6",),
    "scripts/remove/plex.sh": ("ffffd4e43723ea169a0de0c680a1b589ba3513552a006f37a19fc344a8edddda",),
    "scripts/remove/prowlarr.sh": ("af2256da5efe8bfed055b921028a89a925000d7035909eb68a208c9940f0f1e7",),
    "scripts/remove/qbittorrent.sh": ("5826dabea4f1855f7d04939cdbdff090b2c06ed969e050869d9a3510f76f80fe",),
    "scripts/remove/qui.sh": ("42b60c5673d47ecb0516bef33402bbe556ff0ba752c409bc7230847124470fc0",),
    "scripts/remove/radarr.sh": ("aba80f8246d2bd92062fae5807538b7926e063141b877902ad68601e56d5eb73",),
    "scripts/remove/sickgear.sh": ("3a941b023431765c8419f96250e3c42290687235d8f735121ddbfaab991638fb",),
    "scripts/remove/syncthing.sh": ("cd587be4235bc167b1e14f530210e1ae943aaecdda460de80dca53920f426b7c",),
    "scripts/remove/tautulli.sh": ("6e4fe9ed4586cf61169871ca4c5662663668add6f8ea2590bd4f9c41922aa23f",),
    "scripts/remove/transmission.sh": ("8aa238a3e8021de1301ae190a7807aa5dd52a6aff75e18b7bdd27b10be7a4e05",),
}


@dataclass(frozen=True)
class App:
    sources: tuple = ()         # Swizzin files the remove script loads itself (pinned too)
    units: tuple = ()           # systemd units; "name@" stands for every instance systemd knows of
    owner_unit: str = ""        # {owner} = the User= of this unit, read before the removal
    copy: tuple = ()            # saved before the removal (missing ones are skipped)
    skip: tuple = ()            # names left out of the copy (logs, caches)
    gone: tuple = ()            # must be gone afterwards
    kept: tuple = ()            # must still be there afterwards, when they were there before
    nginx: tuple = ()           # the app's nginx files: gone afterwards
    leftovers: tuple = ()       # files the script forgets: deleted after it
    packages: tuple = ()        # apt packages the script removes (names or globs), simulated first
    allowed: tuple = ()         # regexes: the only packages the removal may take with it
    refuse_if: tuple = ()       # installed apps that make the removal unsafe
    answers: tuple = ()         # (exact question, answer) typed in for the user
    partial: bool = False       # the copy leaves out large data the app rebuilds
    tested: bool = False        # passed the container campaign: only then offered


def _home(user, *names):
    return "/home/{%s}/" % user + "/".join(names)


APPS = {
    # The program and its data live together in /opt/airsonic; the script forgets the unit and its env file.
    "airsonic": App(
        units=("airsonic",),
        copy=("/opt/airsonic/airsonic.properties", "/opt/airsonic/db"),
        gone=("/opt/airsonic",),
        nginx=("/etc/nginx/apps/airsonic.conf",),
        leftovers=("/etc/systemd/system/airsonic.service", "/etc/sysconfig/airsonic", "/etc/defaults/airsonic")),
    "autobrr": App(
        sources=("sources/functions/utils",),
        units=("autobrr@",),
        copy=(_home("user", ".config/autobrr"), "/etc/nginx/conf.d/{user}.autobrr.conf"),
        skip=("logs",),
        gone=(_home("user", ".config/autobrr"), "/usr/bin/autobrr", "/usr/bin/autobrrctl",
              "/etc/systemd/system/autobrr@.service"),
        nginx=("/etc/nginx/apps/autobrr.conf", "/etc/nginx/conf.d/*.autobrr.conf")),
    "bazarr": App(
        units=("bazarr",),
        copy=("/opt/bazarr/data",),
        skip=("cache", "log"),
        gone=("/opt/bazarr", "/opt/.venv/bazarr", "/etc/systemd/system/bazarr.service"),
        nginx=("/etc/nginx/apps/bazarr.conf",)),
    # The script deletes its data before stopping the service: the runner stops it first.
    "calibreweb": App(
        units=("calibreweb",),
        copy=("/opt/calibreweb/app.db*", "/opt/calibreweb/gdrive.db*", "/opt/calibreweb/client_secrets.json",
              "/opt/calibreweb/gdrive_credentials", "/var/lib/swizzin/db/calibre"),
        gone=("/opt/calibreweb", "/opt/.venv/calibreweb", "/etc/systemd/system/calibreweb.service"),
        nginx=("/etc/nginx/apps/calibreweb.conf",)),
    # dpkg -r keeps Emby's data; the script never reloads nginx (the runner does).
    "emby": App(
        units=("emby-server",),
        kept=("/var/lib/emby",),
        nginx=("/etc/nginx/apps/emby.conf",),
        packages=("emby-server",),
        allowed=(r"emby-server",)),
    "filebrowser": App(
        units=("filebrowser",),
        copy=(_home("master", ".config/Filebrowser"),),
        gone=(_home("master", ".config/Filebrowser"), _home("master", "bin/filebrowser"),
              "/etc/systemd/system/filebrowser.service"),
        nginx=("/etc/nginx/apps/filebrowser.conf",)),
    # Its program is npm's global package, removed by the script with `npm -g remove`.
    "flood": App(
        units=("flood@",),
        copy=(_home("user", ".config/flood"), _home("user", ".flood"), "/etc/nginx/conf.d/{user}.flood.conf"),
        skip=("temp",),
        gone=(_home("user", ".config/flood"), _home("user", ".flood"), "/etc/systemd/system/flood@.service",
              "/usr/lib/node_modules/flood", "/usr/bin/flood"),
        nginx=("/etc/nginx/apps/flood.conf", "/etc/nginx/conf.d/*.flood.conf")),
    # The script deletes the program and its settings before stopping it (Restart=always): the runner stops it first.
    "jackett": App(
        units=("jackett@",),
        copy=(_home("master", ".config/Jackett"),),
        skip=("log.txt*", "updater.txt*"),
        gone=(_home("master", "Jackett"), _home("master", ".config/Jackett"), "/etc/systemd/system/jackett@.service"),
        nginx=("/etc/nginx/apps/jackett.conf",)),
    "librespeed": App(
        gone=("/srv/librespeed",),
        nginx=("/etc/nginx/apps/librespeed.conf",)),
    # Its settings stay, but a reinstall rewrites config.xml (new API key): it is copied anyway.
    "lidarr": App(
        units=("lidarr",), owner_unit="lidarr",
        copy=(_home("owner", ".config/Lidarr/config.xml"), "/var/lib/swizzin/db/lidarr"),
        gone=("/opt/Lidarr", "/etc/systemd/system/lidarr.service"),
        kept=(_home("owner", ".config/Lidarr"),),
        nginx=("/etc/nginx/apps/lidarr.conf",)),
    # The program folder is also the data folder; the script deletes the unit at a wrong path.
    "medusa": App(
        units=("medusa",),
        copy=("/opt/medusa/config.ini", "/opt/medusa/main.db*", "/opt/medusa/cache.db*", "/opt/medusa/failed.db*"),
        gone=("/opt/medusa", "/opt/.venv/medusa"),
        nginx=("/etc/nginx/apps/medusa.conf",),
        leftovers=("/etc/systemd/system/medusa.service",)),
    "mylar": App(
        sources=("sources/functions/utils",),
        units=("mylar",), owner_unit="mylar",
        copy=(_home("owner", ".config/mylar"), "/var/lib/swizzin/db/mylar"),
        skip=("cache", "logs"),
        gone=(_home("owner", ".config/mylar"), "/opt/mylar", "/opt/.venv/mylar", "/etc/systemd/system/mylar.service"),
        nginx=("/etc/nginx/apps/mylar.conf",)),
    "netronome": App(
        sources=("sources/functions/utils",),
        units=("netronome@",),
        copy=(_home("user", ".config/netronome"), "/etc/nginx/conf.d/{user}.netronome.conf"),
        gone=(_home("user", ".config/netronome"), "/usr/bin/netronome", "/etc/systemd/system/netronome@.service"),
        nginx=("/etc/nginx/apps/netronome.conf", "/etc/nginx/conf.d/*.netronome.conf")),
    "nzbhydra": App(
        sources=("sources/functions/utils",),
        units=("nzbhydra",),
        copy=(_home("master", ".config/nzbhydra2"),),
        skip=("logs", "backup"),
        gone=("/opt/nzbhydra2", _home("master", ".config/nzbhydra2"), "/etc/systemd/system/nzbhydra.service"),
        nginx=("/etc/nginx/apps/nzbhydra.conf",)),
    # No service (php-fpm); /root/.organizr is kept by the script but rewritten by a reinstall.
    "organizr": App(
        copy=("/srv/organizr_db", "/srv/organizr/api/config", "/root/.organizr"),
        gone=("/srv/organizr", "/srv/organizr_db", "/etc/fail2ban/filter.d/organizr-auth.conf",
              "/etc/fail2ban/jail.d/organizr-auth.conf"),
        nginx=("/etc/nginx/apps/organizr.conf",)),
    # Only the settings and the databases are copied: metadata, posters and caches can weigh many GB.
    "plex": App(
        units=("plexmediaserver",),
        copy=("/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Preferences.xml",
              "/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Plug-in Support/Databases"),
        gone=("/var/lib/plexmediaserver", "/usr/lib/plexmediaserver"),
        packages=("plexmediaserver",),
        allowed=(r"plexmediaserver",),
        partial=True),
    # Its question "purge the configuration?" gets Swizzin's default answer: the copy keeps the settings.
    "prowlarr": App(
        units=("prowlarr",), owner_unit="prowlarr",
        copy=(_home("owner", ".config/Prowlarr"), "/var/lib/swizzin/db/prowlarr"),
        skip=("logs", "logs.db*", "Backups"),
        gone=("/opt/Prowlarr", _home("owner", ".config/Prowlarr"), "/etc/systemd/system/prowlarr.service"),
        nginx=("/etc/nginx/apps/prowlarr.conf",),
        answers=(("Would you like to purge the configuration?", "y"),)),
    # The script misses the settings folder (wrong case): qBittorrent.conf is copied because a reinstall
    # rewrites it. Its libtorrent purge would take Deluge with it when Deluge is installed. It also forgets
    # qbtindex.conf, the web index of the downloads (/qbittorrent.downloads): deleted after it.
    "qbittorrent": App(
        sources=("sources/functions/utils", "sources/functions/libtorrent"),
        units=("qbittorrent@",),
        copy=(_home("user", ".config/qBittorrent/qBittorrent.conf"), "/etc/nginx/conf.d/{user}.qbittorrent.conf"),
        gone=("/etc/systemd/system/qbittorrent@.service",),
        kept=(_home("user", ".config/qBittorrent"), _home("user", ".local/share/qBittorrent"),
              _home("user", "torrents/qbittorrent")),
        nginx=("/etc/nginx/apps/qbittorrent.conf", "/etc/nginx/conf.d/*.qbittorrent.conf",
               "/etc/nginx/apps/qbtindex.conf"),
        leftovers=("/etc/nginx/apps/qbtindex.conf",),
        packages=("qbittorrent-nox", "libtorrent-rasterbar*", "python-libtorrent", "python3-libtorrent",
                  "qtbase5-swizzin", "qttools5-swizzin", "qt6-swizzin", "cmake-swizzin"),
        allowed=(r"qbittorrent-nox", r"libtorrent-rasterbar.*", r"python3?-libtorrent",
                 r"(qtbase5|qttools5|qt6|cmake)-swizzin"),
        refuse_if=("deluge",)),
    "qui": App(
        sources=("sources/functions/utils",),
        units=("qui@",),
        copy=(_home("user", ".config/qui"), "/etc/nginx/conf.d/{user}.qui.conf"),
        gone=(_home("user", ".config/qui"), "/usr/bin/qui", "/etc/systemd/system/qui@.service"),
        nginx=("/etc/nginx/apps/qui.conf", "/etc/nginx/conf.d/*.qui.conf")),
    # Its settings stay, but a reinstall rewrites config.xml: it is copied anyway.
    "radarr": App(
        units=("radarr",), owner_unit="radarr",
        copy=(_home("owner", ".config/Radarr/config.xml"), "/var/lib/swizzin/db/radarr"),
        gone=("/opt/Radarr", "/etc/systemd/system/radarr.service"),
        kept=(_home("owner", ".config/Radarr"),),
        nginx=("/etc/nginx/apps/radarr.conf",)),
    # The program folder is also the data folder; the script deletes the unit at a wrong path.
    "sickgear": App(
        units=("sickgear",),
        copy=("/opt/sickgear/config.ini", "/opt/sickgear/sickbeard.db*", "/opt/sickgear/cache.db*",
              "/opt/sickgear/failed.db*"),
        gone=("/opt/sickgear", "/opt/.venv/sickgear"),
        nginx=("/etc/nginx/apps/sickgear.conf",),
        leftovers=("/etc/systemd/system/sickgear.service",)),
    # Nothing of the user's is deleted: settings, keys and synced folders stay in the master's home.
    "syncthing": App(
        units=("syncthing@",),
        gone=("/etc/systemd/system/syncthing@.service",),
        kept=(_home("master", ".config/syncthing"), _home("master", ".local/state/syncthing"),
              _home("master", "Sync")),
        nginx=("/etc/nginx/apps/syncthing.conf",),
        packages=("syncthing",),
        allowed=(r"syncthing",)),
    "tautulli": App(
        units=("tautulli",),
        copy=("/opt/tautulli/config.ini", "/opt/tautulli/tautulli.db*"),
        gone=("/opt/tautulli", "/opt/.venv/tautulli", "/etc/systemd/system/tautulli.service"),
        nginx=("/etc/nginx/apps/tautulli.conf",)),
    # Only settings.json is deleted (torrents, resume data and downloads stay); a reinstall rewrites it. The script
    # removes the PPA its installer added (Ubuntu), and forgets tmsindex.conf, the web index of the downloads
    # (/transmission.downloads): deleted after it.
    "transmission": App(
        units=("transmission@",),
        copy=(_home("user", ".config/transmission-daemon/settings.json"), "/etc/nginx/conf.d/{user}.transmission.conf"),
        gone=(_home("user", ".config/transmission-daemon/settings.json"), "/etc/systemd/system/transmission@.service",
              "/etc/apt/sources.list.d/transmissionbt-ubuntu-ppa-*"),
        kept=(_home("user", ".config/transmission-daemon"), _home("user", "transmission")),
        nginx=("/etc/nginx/apps/transmission.conf", "/etc/nginx/conf.d/*.transmission.conf",
               "/etc/nginx/apps/tmsindex.conf"),
        leftovers=("/etc/nginx/apps/tmsindex.conf",),
        packages=("transmission-common", "transmission-cli", "transmission-daemon"),
        allowed=(r"transmission-(common|cli|daemon)",)),
}

# Not offered, with the reason shown on their card (the panel words it for the user).
EXCLUDED = {
    "rtorrent": "it also removes ruTorrent and Flood, and stops every torrent",
    "rutorrent": "it deletes the saved .torrent files, RSS filters and settings of every user",
    "deluge": "its removal handles only the first user and purges a library other apps use",
    "btsync": "it deletes the synced files",
    "nzbget": "it deletes its downloads",
    "jellyfin": "its removal can stop halfway and leave Jellyfin installed with empty data",
    "sonarr": "its removal never stops Sonarr and cleans packages system-wide",
    "quassel": "its removal updates apt halfway and can stop there",
}


def script(app):
    return "scripts/remove/%s.sh" % app


def pinned_files(app):
    """Every file of Swizzin the removal of app runs or loads."""
    return COMMON + APPS[app].sources + (script(app),)


def load(path=None):
    """(common files, pins, apps): this module's, or a JSON table of the same shape (the tests')."""
    if not path:
        return COMMON, PINS, APPS
    with open(path) as f:
        data = json.load(f)

    def frozen(value):
        return tuple(frozen(v) for v in value) if isinstance(value, list) else value
    apps = {name: App(**{k: frozen(v) for k, v in fields.items()}) for name, fields in data["apps"].items()}
    return tuple(data["common"]), {k: tuple(v) for k, v in data["pins"].items()}, apps
