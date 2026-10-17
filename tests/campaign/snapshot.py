#!/usr/bin/python3
"""What a throwaway Swizzin looks like at one moment, and what changed between two moments (see remove.py).

Run as root inside the container, with the system Python (standard library only):
  python3 -I snapshot.py take <file.json.gz>          files, packages, units, ports, nginx, locks, users
  python3 -I snapshot.py diff <before> <after>        print what changed, as one JSON object
  python3 -I snapshot.py hash <path> ...              print {path: sha256 or null}
  python3 -I snapshot.py exists <path> ...            print {path: [type of each match]} (* inside its folder)
  python3 -I snapshot.py archive <copy> <before> <spec.json>   check a copy of settings (see archive_report)

Files are listed with their type, mode, owner, size and modification time, never their content (the witness
files are hashed separately). Folders where the system writes on its own are left out: logs, caches, apt and
dpkg's own state (packages are compared through dpkg instead), systemd's timestamps, temporary files.
"""
import fnmatch
import gzip
import hashlib
import json
import os
import stat
import subprocess
import sys
import tarfile

SKIP = {"/proc", "/sys", "/dev", "/run", "/tmp", "/var/tmp", "/var/log", "/var/cache", "/var/lib/apt",
        "/var/lib/dpkg", "/var/lib/systemd", "/var/lib/nginx", "/var/lib/php/sessions", "/var/lib/fail2ban",
        "/var/lib/logrotate", "/var/lib/sudo", "/var/lib/private", "/var/lib/ucf", "/var/backups",
        "/usr/share/doc", "/usr/share/man", "/usr/share/locale", "/root/logs", "/root/.cache", "/root/campaign",
        "/var/lib/swizzin-store", "/swapfile"}
NOISE = {"/etc/ld.so.cache", "/etc/resolv.conf", "/etc/hostname", "/etc/hosts", "/etc/mtab"}
TYPES = {stat.S_IFREG: "f", stat.S_IFDIR: "d", stat.S_IFLNK: "l", stat.S_IFIFO: "p", stat.S_IFSOCK: "s",
         stat.S_IFCHR: "c", stat.S_IFBLK: "b"}


def run(*argv):
    r = subprocess.run(argv, capture_output=True, text=True, errors="replace")
    return r.returncode, r.stdout


def files():
    """{path: [type, mode, uid, gid, size, mtime_ns, link target]} for everything outside SKIP."""
    out = {}
    for top, dirs, names, topfd in os.fwalk("/", follow_symlinks=False):
        dirs[:] = [d for d in dirs if os.path.join(top, d) not in SKIP]
        for name in dirs + names:
            path = os.path.join(top, name)
            if path in NOISE or path in SKIP:
                continue
            try:
                st = os.lstat(name, dir_fd=topfd)
            except OSError:
                continue
            kind = TYPES.get(stat.S_IFMT(st.st_mode), "?")
            target = os.readlink(name, dir_fd=topfd) if kind == "l" else None
            out[path] = [kind, stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid,
                         st.st_size if kind == "f" else 0, st.st_mtime_ns if kind == "f" else 0, target]
    return out


def packages():
    code, text = run("dpkg-query", "-W", "-f", "${binary:Package}\t${Version}\t${db:Status-Abbrev}\n")
    out = {}
    for line in text.splitlines():
        name, version, status = (line.split("\t") + ["", ""])[:3]
        out[name] = [version, status.strip()]
    return out


def package_files():
    """{package: [path, ...]} from dpkg's lists: what a package removal may take with it."""
    out = {}
    info = "/var/lib/dpkg/info"
    for name in os.listdir(info):
        if name.endswith(".list"):
            with open(os.path.join(info, name), errors="replace") as f:
                out[name[:-5]] = [line.rstrip("\n") for line in f if line.strip() and line.strip() != "/."]
    return out


def units():
    code, text = run("systemctl", "list-units", "--all", "--type=service", "--plain", "--no-legend")
    loaded = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 4:
            loaded[fields[0]] = fields[1:4]  # load, active, sub
    code, text = run("systemctl", "list-unit-files", "--type=service", "--plain", "--no-legend")
    unit_files = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 2:
            unit_files[fields[0]] = fields[1]
    return loaded, unit_files


def ports():
    code, text = run("ss", "-ltnpH")
    out = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 4:
            program = line.split('(("', 1)[1].split('"', 1)[0] if '(("' in line else ""
            out.append([fields[3].rsplit(":", 1)[-1], program])
    return sorted(out)


def take(path):
    code, nginx_t = run("bash", "-c", "nginx -t 2>&1")
    _, nginx_full = run("bash", "-c", "nginx -T 2>/dev/null")
    loaded, unit_files = units()
    with open("/etc/passwd") as f:
        users = sorted(line.split(":", 1)[0] for line in f if ":" in line)
    snap = {
        "files": files(), "packages": packages(), "package_files": package_files(),
        "manual": sorted(run("apt-mark", "showmanual")[1].split()), "holds": sorted(run("apt-mark", "showhold")[1].split()),
        "units": loaded, "unit_files": unit_files, "ports": ports(),
        "nginx_ok": code == 0, "nginx_test": nginx_t[-2000:], "nginx_config": nginx_full,
        "locks": sorted(n for n in os.listdir("/install") if n.endswith(".lock")) if os.path.isdir("/install") else [],
        "users": users,
    }
    with gzip.open(path, "wt") as f:
        json.dump(snap, f)
    return {"files": len(snap["files"]), "packages": len(snap["packages"])}


def load(path):
    with gzip.open(path, "rt") as f:
        return json.load(f)


def same_file(a, b):
    """Two listings of one path: the same unless its type, mode, owner, size or time changed (a folder's size
    and time follow its entries, they do not count)."""
    if a[0] != b[0] or a[1:4] != b[1:4] or a[6] != b[6]:
        return False
    return a[0] != "f" or a[4:6] == b[4:6]


def diff(before, after):
    fa, fb = before["files"], after["files"]
    gone_packages = {n: v for n, v in before["packages"].items()
                     if v[1].startswith("ii") and not after["packages"].get(n, ["", ""])[1].startswith("ii")}
    new_packages = {n: v for n, v in after["packages"].items()
                    if v[1].startswith("ii") and not before["packages"].get(n, ["", ""])[1].startswith("ii")}
    owned = {}  # path -> package, for the packages that went away
    for name in gone_packages:
        for p in before["package_files"].get(name, []):
            owned[p] = name
    return {
        "removed": {p: v for p, v in fa.items() if p not in fb},
        "added": {p: v for p, v in fb.items() if p not in fa},
        "changed": {p: [v, fb[p]] for p, v in fa.items() if p in fb and not same_file(v, fb[p])},
        "packages_removed": gone_packages,
        "packages_added": new_packages,
        "packages_changed": {n: [v, after["packages"][n]] for n, v in before["packages"].items()
                             if n in after["packages"] and v[0] != after["packages"][n][0]},
        "owned": owned,
        "manual": [sorted(set(before["manual"]) - set(after["manual"])), sorted(set(after["manual"]) - set(before["manual"]))],
        "holds": [before["holds"], after["holds"]],
        "units_before": before["units"], "units_after": after["units"],
        "unit_files_before": before["unit_files"], "unit_files_after": after["unit_files"],
        "ports_before": before["ports"], "ports_after": after["ports"],
        "nginx_ok": [before["nginx_ok"], after["nginx_ok"]], "nginx_test_after": after["nginx_test"],
        "nginx_config_after": after["nginx_config"],
        "locks": [before["locks"], after["locks"]],
        "users": [sorted(set(before["users"]) - set(after["users"])), sorted(set(after["users"]) - set(before["users"]))],
    }


def hashes(paths):
    out = {}
    for p in paths:
        try:
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    h.update(block)
            out[p] = h.hexdigest()
        except OSError:
            out[p] = None
    return out


def exists(paths):
    """{pattern: [type of each match]}: what of the table's paths is there now (a * matches inside its folder)."""
    out = {}
    for p in paths:
        folder, name = os.path.split(p)
        if "*" in name:
            try:
                matches = sorted(os.path.join(folder, n) for n in os.listdir(folder) if fnmatch.fnmatchcase(n, name))
            except OSError:
                matches = []
        else:
            matches = [p] if os.path.lexists(p) else []
        out[p] = [TYPES.get(stat.S_IFMT(os.lstat(m).st_mode), "?") for m in matches]
    return out


def archive_report(archive, before, spec):
    """Is the copy of the settings what it should be? Read back in full, compared with the photo taken before the
    removal: every regular file under the paths to copy is in it, except the names left out on purpose, the files
    of other users and what the uninstaller listed as not copied (links, FIFOs...)."""
    out = {"archive": archive}
    st, folder = os.lstat(archive), os.lstat(os.path.dirname(archive))
    out.update(mode=oct(stat.S_IMODE(st.st_mode)), uid=st.st_uid,
               dir_mode=oct(stat.S_IMODE(folder.st_mode)), dir_uid=folder.st_uid)
    bad = []
    with tarfile.open(archive, "r:gz") as tar:
        members = {m.name: m for m in tar.getmembers()}
        manifest = json.load(tar.extractfile("swizzin-store/manifest.json"))
        out["restore_txt"] = tar.extractfile("swizzin-store/RESTORE.txt").read().decode()
        for m in manifest:
            if m["type"] != "file":
                continue
            member = members.get(m["path"].lstrip("/"))
            h = hashlib.sha256()
            if member is None or not member.isfile():
                bad.append(m["path"])
                continue
            with tar.extractfile(member) as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    h.update(block)
            if h.hexdigest() != m["sha256"]:
                bad.append(m["path"])
        out["outside_manifest"] = sorted(n for n, m in members.items() if m.isfile() and not n.startswith("swizzin-store/")
                                         and "/" + n not in {x["path"] for x in manifest})
    out["manifest_bad"] = bad
    copied = {m["path"] for m in manifest if m["type"] == "file"}
    out["files"] = len(copied)
    out["copied_hashes"] = sorted({m["sha256"] for m in manifest if m["type"] == "file"})
    listed = before["files"]
    skipped = [p for p, _ in spec["skipped"]]
    expected = set()
    for pattern in spec["copy"]:
        folder, name = os.path.split(pattern)
        roots = sorted(p for p in listed if os.path.dirname(p) == folder and fnmatch.fnmatchcase(os.path.basename(p), name)) \
            if "*" in name else ([pattern] if pattern in listed else [])
        for root in roots:
            kind, owner = listed[root][0], listed[root][2]
            if kind == "f":
                expected.add(root)
            elif kind == "d":
                for p, v in listed.items():
                    if not p.startswith(root + "/") or v[0] != "f":
                        continue
                    names = p[len(root) + 1:].split("/")
                    if any(fnmatch.fnmatchcase(n, s) for n in names for s in spec["skip"]) or v[2] not in (0, owner):
                        continue
                    expected.add(p)
    out["missing"] = sorted(p for p in expected - copied if not any(p == s or p.startswith(s + "/") for s in skipped))
    return out


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "take":
        json.dump(take(sys.argv[2]), sys.stdout)
    elif len(sys.argv) == 4 and sys.argv[1] == "diff":
        json.dump(diff(load(sys.argv[2]), load(sys.argv[3])), sys.stdout)
    elif len(sys.argv) >= 2 and sys.argv[1] == "hash":
        json.dump(hashes(sys.argv[2:]), sys.stdout)
    elif len(sys.argv) >= 2 and sys.argv[1] == "exists":
        json.dump(exists(sys.argv[2:]), sys.stdout)
    elif len(sys.argv) == 5 and sys.argv[1] == "archive":
        with open(sys.argv[4]) as f:
            json.dump(archive_report(sys.argv[2], load(sys.argv[3]), json.load(f)), sys.stdout)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
