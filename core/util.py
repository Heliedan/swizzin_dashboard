import sys
import os
import swizzin
from flask import request, current_app
from flask_socketio import SocketIO, emit
import subprocess as sp
import json
import shutil
import datetime
import calendar
import re
import logging
from pwd import getpwnam
from eventlet.patcher import original

log = logging.getLogger(__name__)

is_shared = swizzin.app.config['SHAREDSERVER']

if is_shared is True:
    from core.profiles_shared import *
else:
    from core.profiles import *
    try:
        from core.custom.profiles import *
    except ModuleNotFoundError as e:
        # Having no custom profiles file is fine, a module missing inside it is not
        if e.name not in ("core.custom", "core.custom.profiles"):
            log.exception("Could not load core/custom/profiles.py, custom profiles are ignored")
    except Exception:
        log.exception("Could not load core/custom/profiles.py, custom profiles are ignored")


def get_btime_utc():
    file = open('/proc/stat', 'r')
    for line in file:
        if line.startswith("btime"):
            btime = int(line.split(" ")[1])
            return btime

boottimestamp = get_btime_utc()
boottimeutc = datetime.datetime.utcfromtimestamp(boottimestamp).strftime('%b %d, %Y %H:%M:%S')

def str_to_class(str):
    return getattr(sys.modules[__name__], str)

def get_default_interface():
    #Based on: https://stackoverflow.com/a/6556951
    """Get the default interface directly from /proc."""
    with open("/proc/net/route") as route:
        for line in route:
            fields = line.strip().split()
            #if fields[1] != '00000000' or not int(fields[3], 16) & 2:
            if fields[1] != '00000000':
                continue
            return fields[0]

def unescape_mount(path):
    """/proc/mounts and /etc/fstab write a space in a path as \\040 (octal escapes): return the real path."""
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), path)

def get_mounts():
    mounts = set()
    with open("/proc/mounts") as mount:
        for line in mount:
            fields = line.strip().split()
            if fields[0].startswith("/dev") or re.match("[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}", fields[0]) or re.match("^(([0-9A-Fa-f]{1,4}:){7})([0-9A-Fa-f]{1,4})$|(([0-9A-Fa-f]{1,4}:){1,6}:)(([0-9A-Fa-f]{1,4}:){0,4})([0-9A-Fa-f]{1,4})$", fields[0]):
                if ("boot" in fields[1]) or ("fuse" in fields) or ("/snap/" in fields[1]) or ("/loop" in fields[0]) or ("/docker" in fields[1]):
                    continue
                else:
                    mounts.add(unescape_mount(fields[1]))
            if fields[2] == "zfs":
                mounts.add(unescape_mount(fields[1]))
    with open("/etc/fstab") as fstab:
        for line in fstab:
            fields = line.strip().split()
            if "bind" in str(fields):
                try:
                    mounts.remove(unescape_mount(fields[1]))
                except:
                    pass
    return sorted(mounts)

def generate_page_list(user):
    admin_user = current_app.config['ADMIN_USER']
    pages = []
    if is_shared is True:
        locks = os.listdir('/home/'+user+'/.install')
    else:
        locks = os.listdir('/install')
    try:
        if "[" in request.host:
            # Special case to handle IPv6 addresses
            host = request.host.split("]:")[0]
        else:
            # Handle IPv4 addresses
            host = request.host.split(":")[0]
    except:
        host = request.host

    scheme = request.scheme
    for lock in locks:
        app = lock.split(".")[1]
        try:
            profile = str_to_class(app+"_meta")(user)
        except:
            try:
                profile = str_to_class(app+"_meta")()
            except:
                continue
        try:
            multiuser = profile.multiuser
        except:
            multiuser = False
        if multiuser == False and user != admin_user:
            continue
        try:
            scheme = profile.scheme
        except:
            scheme = request.scheme
        try:
            url = profile.urloverride
        except:
            try:
                url = scheme+"://"+host+profile.baseurl
            except:
                url = False
        try:
            systemd = profile.systemd
        except:
            systemd = profile.name
        try:
            brand = profile.img
        except:
            brand = profile.name
        pages.append({"name": profile.name, "pretty_name": profile.pretty_name, "url": url, "systemd": systemd, "img": brand})
    return pages

def apps_status(username):
    apps = []
    admin_user = current_app.config['ADMIN_USER']
    is_shared = current_app.config['SHAREDSERVER']
    if is_shared is True:
        locks = os.listdir('/home/'+username+'/.install')
    else:
        locks = os.listdir('/install')
    ps = sp.Popen(('ps', 'axo', 'user:32,comm,cmd'), stdout=sp.PIPE).communicate()[0]
    procs = ps.splitlines()
    for lock in locks:
        application = lock.split(".")[1]
        try:
            profile = str_to_class(application+"_meta")(username)
        except:
            try:
                profile = str_to_class(application+"_meta")
            except Exception as e:
                current_app.logger.debug("Encountered an error while loading the profile for %s: %s", application, e)
                continue
        try:
            multiuser = profile.multiuser
        except:
            multiuser = False
        if multiuser == False and username != admin_user:
            continue
        try:
            #If application is not run as user
            user = profile.runas
        except:
            user = username
        try:
            #If application in `ps` has another name
            application = profile.process
        except:
            application = profile.name
        try:
            systemd = profile.systemd
        except:
            systemd = profile.name
        if systemd == False:
            continue
        try:
            enabled = is_application_enabled(systemd, user)
        except:
            enabled = False
        try:
            check_theD = profile.check_theD
        except:
            check_theD = False 
        if check_theD is True:
            status = is_process_running(systemd, user, systemd=True)
        else:
            status = is_process_running(application, user, procs=procs)
        apps.append({"name": profile.name, "active": status, "enabled": enabled})
    return apps

def is_process_running(application, username, systemd=False, procs=False):
    result = False
    if systemd is True:
        if application.endswith("@"):
            service = application+username
        else:
            service = application
        returncode = sp.run(('systemctl', 'is-active', service), stdout=sp.DEVNULL).returncode
        if returncode == 0:
            result = True
    else:
        for p in procs:
            p = p.decode('utf-8').split()
            if username.lower() == str(p[0]).lower():
                if application.lower() in str(p).lower():
                    result = True
    return result

def is_application_enabled(application, user):
    if application.endswith("@"):
        result = os.path.exists('/etc/systemd/system/multi-user.target.wants/{application}{user}.service'.format(application=application, user=user))
    else:
        result = os.path.exists('/etc/systemd/system/multi-user.target.wants/{application}.service'.format(application=application))
    return result

def systemctl(function, application):
    if function in ("enable", "disable"):
        result = sp.run(('sudo', 'systemctl', function, '--now', application), stdout=sp.DEVNULL).returncode
    else:
        result = sp.run(('sudo', 'systemctl', function, application), stdout=sp.DEVNULL).returncode
    return result

# Bandwidth of the Network Info card (/stats/vnstat, admin only; see the README). Every system Swizzin installs on
# ships vnstat 2 (Ubuntu 20.04 and Debian 11: 2.6, Ubuntu 22.04: 2.9, Debian 12: 2.10, Ubuntu 24.04: 2.12): one
# `vnstat --json` gives every list at once (JSON version 2, in bytes). vnstat 1 (JSON version 1) is not supported.
VNSTAT_TIMEOUT = 10  # seconds: vnstat reads a small local database, it answers at once unless something is wrong
HISTORY_DAYS = 62  # vnstat 2 keeps 62 days and 25 months by default
HISTORY_MONTHS = 25
# Under eventlet (swizzin.py), a timeout with pipes raises the TimeoutExpired of eventlet's own copy of the subprocess
# module, which is not subprocess.TimeoutExpired: catch both (subprocess.run() stops the program either way).
_TIMEOUT_EXPIRED = (sp.TimeoutExpired, original('subprocess').TimeoutExpired)


class VnstatUnavailable(Exception):
    """No bandwidth data to show. reason is a word that templates/top.html turns into a sentence: "no-interface",
    "not-installed", "no-data", "timeout" or "unreadable". detail, when there is one, is for the panel's log."""

    def __init__(self, reason, detail=None):
        super().__init__(reason if detail is None else "{}: {}".format(reason, detail))
        self.reason = reason
        self.detail = detail


def vnstat_traffic(interface):
    """The "traffic" object vnstat recorded for this interface, from a single `vnstat -i <interface> --json`."""
    if not interface:
        raise VnstatUnavailable("no-interface")
    try:
        done = sp.run(('vnstat', '-i', interface, '--json'), stdout=sp.PIPE, stderr=sp.PIPE, timeout=VNSTAT_TIMEOUT)
    except FileNotFoundError:
        raise VnstatUnavailable("not-installed")
    except _TIMEOUT_EXPIRED:
        raise VnstatUnavailable("timeout", "no answer within {} seconds".format(VNSTAT_TIMEOUT))
    except OSError as e:
        raise VnstatUnavailable("unreadable", e)
    if done.returncode != 0:
        # The interface is not in vnstat's database (yet), or there is no database: vnstat says which on stderr.
        said = done.stderr.decode('utf-8', 'replace').strip()
        raise VnstatUnavailable("no-data", said or "exit code {}".format(done.returncode))
    try:
        data = json.loads(done.stdout.decode('utf-8'))
        if data["jsonversion"] != "2":
            raise ValueError("JSON version {!r}, not 2".format(data["jsonversion"]))
        return data["interfaces"][0]["traffic"]
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise VnstatUnavailable("unreadable", repr(e))


def bandwidth(traffic, top_count, cutoff=None):
    """What templates/top.html shows, from vnstat_traffic(): the same totals and top days as before this edition
    read vnstat once, and the history of the last days and months, newest first.
    top_count: how many top days (NETWORK_INFO_TOPSTATS); cutoff: a datetime.date, to rank only the days since then
    (as `vnstat -b` did), or None for vnstat's own top list.
    Sizes are written as before: "1.23 GB" (1 GB = 10^9 bytes)."""
    try:
        if not (traffic["hour"] or traffic["day"] or traffic["month"]):
            raise VnstatUnavailable("no-data")  # vnstat follows the interface, but has saved nothing yet
        hours = traffic["hour"]
        if cutoff is None:
            tops = traffic["top"]
        else:
            tops = sorted((d for d in traffic["day"] if _vnstat_date(d) >= cutoff),
                          key=lambda d: d["rx"] + d["tx"], reverse=True)
        return {
            # The last entry of each list, as before: vnstat lists the oldest first.
            "hour": _amounts(hours[-1]) if hours else None,
            "lasthour": _amounts(hours[-2]) if len(hours) > 1 else None,
            "day": _amounts(traffic["day"][-1]) if traffic["day"] else None,
            "month": _amounts(traffic["month"][-1]) if traffic["month"] else None,
            "alltime": _amounts(traffic["total"]),
            "days": _history(traffic["day"][-HISTORY_DAYS:], _day_label),
            "months": _history(traffic["month"][-HISTORY_MONTHS:], _month_label),
            "top": [dict(_amounts(d), date=_day_label(d)) for d in tops[:top_count]],
        }
    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as e:
        raise VnstatUnavailable("unreadable", repr(e))


def top_cutoff(setting):
    """NETWORK_INFO_CUTOFF as a datetime.date, or None when it is not set ("YYYY-MM-DD", its default, or empty).
    Raises ValueError when it is not a date written YYYY-MM-DD."""
    if not setting or setting == "YYYY-MM-DD":
        return None
    return datetime.datetime.strptime(setting, "%Y-%m-%d").date()


def _amounts(entry):
    return {"rx": GetHumanReadableB(entry['rx']), "tx": GetHumanReadableB(entry['tx']),
            "total": GetHumanReadableB(entry['rx'] + entry['tx'])}


def _vnstat_date(entry):
    date = entry['date']
    return datetime.date(date['year'], date['month'], date['day'])


def _day_label(entry):
    date = entry['date']
    return "{month} {day}, {year}".format(month=calendar.month_abbr[date['month']], day=date['day'], year=date['year'])


def _month_label(entry):
    date = entry['date']
    return "{month} {year}".format(month=calendar.month_abbr[date['month']], year=date['year'])


def _history(entries, label):
    """The entries newest first, each with its bar: received then sent, in percent of the largest total of the list."""
    largest = max((e['rx'] + e['tx'] for e in entries), default=0)
    rows = []
    for e in reversed(entries):
        row = _amounts(e)
        row["date"] = label(e)
        row["rx_bar"] = "{:.2f}".format(e['rx'] * 100 / largest if largest else 0)
        row["tx_bar"] = "{:.2f}".format(e['tx'] * 100 / largest if largest else 0)
        rows.append(row)
    return rows

def disk_usage(location):
    total, used, free = shutil.disk_usage(location)
    if swizzin.app.config['DISK_UNITS'] == "si":
        totalh = GetHumanReadableB(total)
        usedh = GetHumanReadableB(used)
        freeh = GetHumanReadableB(free)
    else:
        totalh = GetHumanReadableBi(total)
        usedh = GetHumanReadableBi(used)
        freeh = GetHumanReadableBi(free)
    # Like df's Use%: used / (used + available). "free" is what a normal user can still write (df's Avail):
    # the blocks reserved for root (5 % on ext4) are not free for the user, so a full disk shows 100 %.
    usage = '{0:.2f}'.format((used / (used + free) * 100) if used + free else 0)
    return totalh, usedh, freeh, usage

def quota_usage(username):
    quota = sp.Popen(('sudo', 'quota', '-wpu', username), stdout=sp.PIPE)
    quota = quota.communicate()[0].decode("utf-8").split('\n')[2].split()
    fs = quota[0]
    used = int(re.sub("[^0-9]", "", quota[1]))
    total = int(quota[2])
    free = total - used
    totalh = GetHumanReadableKiB(total)
    usedh = GetHumanReadableKiB(used)
    freeh = GetHumanReadableKiB(free)
    usage = '{0:.2f}'.format((used / total * 100))
    return totalh, usedh, freeh, usage

def network_quota_usage(username):
    quota = sp.Popen(('sudo', '/etc/swizzin.xl/add-on/panelquotas.sh', 'json', username), stdout=sp.PIPE).communicate()[0].decode("utf-8")
    quota = json.loads(quota)
    try:
        total = int(quota['total'])
        totalh = GetHumanReadableBi(total)
    except:
        totalh = quota['total']
    try:
        free = int(quota['remaining'])
        freeh = GetHumanReadableBi(free)
    except:
        freeh = quota['remaining']
    used = int(quota['used'])
    usedh = GetHumanReadableBi(used)
    try:
        usage = '{0:.2f}'.format((used / total * 100))
    except:
        usage = "N/A"
    return totalh, usedh, freeh, usage

def GetHumanReadableKiB(size,precision=2):
    #https://stackoverflow.com/a/32009595
    suffixes=['KiB','MiB','GiB','TiB','PiB']
    suffixIndex = 0
    while size > 1024 and suffixIndex < 5:
        suffixIndex += 1 #increment the index of the suffix
        size = size/1024.0 #apply the division
    return "%.*f %s"%(precision,size,suffixes[suffixIndex])

#KB function is unused -- check math before using
def GetHumanReadableKB(size,precision=2):
    #https://stackoverflow.com/a/32009595
    suffixes=['KB','MB','GB','TB','PB']
    suffixIndex = 0
    while size > 1000 and suffixIndex < 4:
        suffixIndex += 1 #increment the index of the suffix
        size = size/1000.0 #apply the division
    return "%.*f %s"%(precision,size,suffixes[suffixIndex])

def GetHumanReadableBi(size,precision=2):
    #https://stackoverflow.com/a/32009595
    suffixes=['B','KiB','MiB','GiB','TiB','PiB']
    suffixIndex = 0
    while size > 1024 and suffixIndex < 5:
        suffixIndex += 1 #increment the index of the suffix
        size = size/1024.0 #apply the division
    return "%.*f %s"%(precision,size,suffixes[suffixIndex])

def GetHumanReadableB(size,precision=2):
    #https://stackoverflow.com/a/32009595
    suffixes=['B','KB','MB','GB','TB','PB']
    suffixIndex = 0
    while size > 1000 and suffixIndex < 5:
        suffixIndex += 1 #increment the index of the suffix
        size = size/1000.0 #apply the division
    return "%.*f %s"%(precision,size,suffixes[suffixIndex])

def get_nic_bytes(t, interface):
    with open('/sys/class/net/' + interface + '/statistics/' + t + '_bytes', 'r') as f:
        data = f.read();
    return int(data)

def get_uid(user):
    result = getpwnam(user).pw_uid
    return result


#https://stackoverflow.com/questions/41431882/live-stream-stdout-and-stdin-with-websocket
## panel threading install idea
#async def time(websocket, path):
#    script_name = 'script.py'
#    script = await websocket.recv()
#    with open(script_name, 'w') as script_file:
#        script_file.write(script)
#    with subprocess.Popen(['python3', '-u', script_name],
#                          stdout=subprocess.PIPE,
#                          bufsize=1,
#                          universal_newlines=True) as process:
#        for line in process.stdout:
#            line = line.rstrip()
#            print(f"line = {line}")
#            await websocket.send(line)
