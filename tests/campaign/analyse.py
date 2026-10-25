"""The verdict on one uninstallation of the campaign (see remove.py): pure functions, tested in tests/test_campaign_analyse.py.

An uninstallation passes when everything it took away is within what store-helper/removable.py says the app's
removal may take (its folders, unit files, nginx files, lock, Swizzin db entries and the files of the packages
it may remove), when the user's files are untouched, and when the uninstaller's own checks agree. Whatever
needs a human eye without being wrong (a file left behind, a system user deleted with its package) is listed
for review instead.
"""
import fnmatch
import re

PASSWORD_FILES = {"/etc/passwd", "/etc/group", "/etc/shadow", "/etc/gshadow", "/etc/subuid", "/etc/subgid"}
TRANSIENT = re.compile(r"(\.pid|\.lock|lockfile|\.sock|-wal|-shm|-journal|\.tmp)$", re.I)
MARK = "campaign-"  # every witness file and trap of the campaign has this in its name


def expand(pattern, ctx):
    """The paths a pattern of the table stands for on the campaign's machine ({user}, {master}, {owner})."""
    paths = [pattern.replace("{user}", u) for u in ctx["users"]] if "{user}" in pattern else [pattern]
    return [p.replace("{master}", ctx["master"]).replace("{owner}", ctx["owner"]) for p in paths]


def scope(app, entry, ctx):
    """Everything the removal of app may take away or change, as path patterns."""
    out = []
    for key in ("gone", "copy", "nginx", "leftovers"):
        for pattern in entry.get(key, ()):
            out += expand(pattern, ctx)
    for unit in entry.get("units", ()):
        name = unit + "*" if unit.endswith("@") else unit
        out += ["/etc/systemd/system/%s.service" % name, "/etc/systemd/system/*.wants/%s.service" % name,
                "/etc/systemd/system/%s.service.d" % name]
        # a package may write its unit file there in its own scripts, without listing it (Plex)
        out += ["%s/systemd/system/%s.service" % (lib, name) for lib in ("/usr/lib", "/lib")]
    return out + ["/install/.%s.lock" % app, "/var/lib/swizzin/db/%s" % app]


def kept(entry, ctx):
    out = []
    for pattern in entry.get("kept", ()):
        out += expand(pattern, ctx)
    return out


def under(path, patterns):
    """path is one of the patterns, or inside one of them (a * may match anything, / included)."""
    for p in patterns:
        if any(c in p for c in "*?["):
            parts = path.split("/")
            if any(fnmatch.fnmatchcase("/".join(parts[:i]), p) for i in range(2, len(parts) + 1)):
                return True
        elif path == p or path.startswith(p.rstrip("/") + "/"):
            return True
    return False


def tops(paths, depth=4):
    """A long list of paths, shown by folder: {'/opt/foo/lib': 120, ...}."""
    out = {}
    for p in paths:
        key = "/".join(p.split("/")[:depth + 1])
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def merged(paths):
    """Packages may list /lib/... or /bin/...: on today's systems these are links to /usr/lib and /usr/bin. And the
    links /etc/rcN.d/[SK]NN<script> are made by the package's own scripts for its /etc/init.d/<script>."""
    out = set(paths)
    out.update("/usr" + p for p in paths if p.startswith(("/lib/", "/bin/", "/sbin/", "/lib64/", "/lib32/", "/libx32/")))
    scripts = [p[len("/etc/init.d/"):] for p in paths if p.startswith("/etc/init.d/") and p.count("/") == 3]
    out.update("/etc/rc%s.d/%s%02d%s" % (level, kind, n, s) for s in scripts for level in "0123456S" for kind in "SK"
               for n in range(100))
    return out


def judge_files(app, entry, ctx, d12, others=()):
    """(fail, review) for what disappeared, changed or appeared during the removal. others: the table entries of
    the neighbours left running, whose own files may change meanwhile (their logs...), never disappear."""
    fail, review = [], []
    allowed, keep = scope(app, entry, ctx), kept(entry, ctx)
    running = [p for name, e in others for p in scope(name, e, ctx) + kept(e, ctx)]
    owned = merged(d12.get("owned", {}))
    bad, near = [], []
    for path, entry_ in d12["removed"].items():
        if under(path, allowed) or path in owned:
            continue
        # FIFOs and sockets are a running program's (the .NET runtime's diagnostic pipes...): they go when it stops
        runtime = entry_[0] in ("p", "s") or TRANSIENT.search(path)
        (near if under(path, keep) or runtime else bad).append(path)
    if bad:
        fail.append("removed outside its scope: %s" % tops(bad))
    if near:
        review.append("removed in a folder it keeps, or a runtime file: %s" % sorted(near)[:20])
    home, other = [], []
    for path in d12["changed"]:
        if under(path, allowed) or under(path, keep) or under(path, running):
            continue
        (home if path.startswith("/home/") else other).append(path)
    if home:
        fail.append("changed in a home folder outside its scope: %s" % sorted(home)[:20])
    users = [p for p in other if p.rstrip("-") in PASSWORD_FILES]
    if users:
        review.append("user accounts changed (%s), deleted users: %s" % (", ".join(sorted(users)), d12["users"][0]))
    other = [p for p in other if p.rstrip("-") not in PASSWORD_FILES]
    if other:
        review.append("changed outside its scope: %s" % sorted(other)[:20])
    added = [p for p in d12["added"] if not under(p, keep) and not under(p, allowed) and not under(p, running)]
    if added:
        review.append("appeared during the removal: %s" % tops(added))
    return fail, review


def judge_packages(entry, d12):
    fail = []
    allowed = entry.get("allowed", ())
    taken = [n for n in d12["packages_removed"] if not any(re.fullmatch(r, n.split(":")[0]) for r in allowed)]
    if taken:
        fail.append("packages removed that it may not remove: %s" % sorted(taken))
    if d12["packages_added"]:
        fail.append("packages installed by the removal: %s" % sorted(d12["packages_added"]))
    if d12["packages_changed"]:
        fail.append("packages upgraded or downgraded by the removal: %s" % sorted(d12["packages_changed"]))
    review = []
    if d12["manual"][0] or d12["manual"][1]:
        review.append("apt-mark manual changed: -%s +%s" % tuple(d12["manual"]))
    if d12["holds"][0] != d12["holds"][1]:
        review.append("held packages changed: %s -> %s" % tuple(d12["holds"]))
    return fail, review


def judge_system(app, entry, d12, d02):
    """Units, nginx, lock and ports after the removal."""
    fail, review = [], []
    names = [u.rstrip("@") for u in entry.get("units", ())]
    mine = lambda unit: any(unit == n + ".service" or unit.startswith(n + "@") for n in names)  # noqa: E731
    left = sorted(u for u in d12["units_after"] if mine(u) and d12["units_after"][u][0] != "not-found")
    left += sorted(u for u in d12["unit_files_after"] if mine(u) and u not in left)
    if left:
        fail.append("its units are still known to systemd: %s" % left)
    new_units = sorted(set(d02["unit_files_after"]) - set(d02["unit_files_before"]) - set(left))
    if new_units:
        review.append("units added since before the installation and still there: %s" % new_units)
    if "." + app + ".lock" in d12["locks"][1]:
        fail.append("its lock file is still there")
    if d12["nginx_ok"][0] and not d12["nginx_ok"][1]:
        fail.append("nginx -t fails after the removal: %s" % d12["nginx_test_after"][-300:])
    config = d12["nginx_config_after"]
    for pattern in entry.get("nginx", ()):
        if "*" not in pattern and "# configuration file %s:" % pattern in config:
            fail.append("nginx still loads %s" % pattern)
    if re.search(r"location\s+[~^=]*\s*/%s\b" % re.escape(app), config, re.I):
        fail.append("nginx still has a location for /%s" % app)
    extra = sorted({tuple(p) for p in d02["ports_after"]} - {tuple(p) for p in d02["ports_before"]})
    if extra:
        review.append("listening now and not before the installation: %s" % extra)
    return fail, review


def judge_leftovers(app, entry, ctx, d02, created=()):
    """What the installation added that is still there after the removal (beyond what it keeps on purpose and the
    campaign's own witness folders)."""
    keep = kept(entry, ctx)
    packaged = d02.get("owned_added", {})  # files of the packages it left: counted with the packages
    paths = [p for p in d02["added"] if MARK not in p and not under(p, keep) and p not in created and p not in packaged]
    review = []
    if paths:
        review.append("left by the installation: %s" % tops(paths))
    if d02["packages_added"]:
        review.append("packages left by the installation: %s" % sorted(d02["packages_added"]))
    if d02["users"][1]:
        review.append("users left by the installation: %s" % d02["users"][1])
    return review


def judge_archive(entry, ctx, before_paths, report, removal, traps):
    fail, review = [], []
    present = [p for p, kinds in before_paths.get("copy", {}).items() if kinds]
    archive = (removal or {}).get("archive")
    if not present:
        if archive:
            review.append("a copy was made although none of its settings existed: %s" % archive)
        return fail, review
    if not archive or not report:
        fail.append("no copy of its settings although these existed: %s" % present)
        return fail, review
    if report.get("error"):
        fail.append("the copy could not be read: %s" % report["error"])
        return fail, review
    if report["mode"] != "0o600" or report["uid"] != 0 or report["dir_mode"] != "0o700" or report["dir_uid"] != 0:
        fail.append("the copy or its folder can be read by others: %s" % {k: report[k] for k in ("mode", "uid", "dir_mode", "dir_uid")})
    if report["manifest_bad"]:
        fail.append("files of the copy do not match its manifest: %s" % report["manifest_bad"][:10])
    if report["outside_manifest"]:
        fail.append("files in the copy that its manifest does not list: %s" % report["outside_manifest"][:10])
    if "box install" not in report.get("restore_txt", ""):
        fail.append("RESTORE.txt is missing or says nothing useful")
    # SQLite folds its -wal file into the database when the app stops cleanly, and deletes it with the -shm file
    missing = [p for p in report["missing"] if not (re.search(r"-(wal|shm|journal)$", p)
                                                    and p.rsplit("-", 1)[0] not in report["missing"])]
    real = [p for p in missing if not TRANSIENT.search(p)]
    if real:
        fail.append("settings missing from the copy: %s" % real[:20])
    if len(real) < len(missing):
        review.append("runtime files missing from the copy (may have gone when it stopped): %s"
                      % [p for p in missing if TRANSIENT.search(p)][:10])
    skipped = {p: why for p, why in (removal.get("skipped") or [])}
    for key in ("link_file", "link_dir", "fifo", "foreign"):
        path = (traps or {}).get(key)
        if path and path not in skipped:
            fail.append("the trap %s was not listed as left out of the copy" % path)
    if traps and traps.get("target_sha") and traps["target_sha"] in report.get("copied_hashes", []):
        fail.append("the copy followed a link: the content of %s is in it" % traps["target"])
    return fail, review


def verdict(app, entry, r, reinstall=True, others=()):
    """{'verdict': 'pass' or 'fail', 'fail': [...], 'review': [...]} for one record of remove.py's cycle."""
    fail, review = [], []
    if r.get("install", {}).get("result") != "installed":
        return {"verdict": "not-tested", "fail": ["the installation failed: %s" % r.get("install", {}).get("result")],
                "review": []}
    removal = r.get("removal") or {}
    if removal.get("preview_error") or removal.get("start_error"):
        fail.append("refused: %s %s" % (removal.get("preview_error") or removal.get("start_error"),
                                        removal.get("preview_message") or ""))
        return {"verdict": "fail", "fail": fail, "review": review}
    if removal.get("result") != "removed":
        fail.append("result %s: %s" % (removal.get("result"), removal.get("message")))
    failed = [c for c in removal.get("checks") or [] if not c.get("ok")]
    if failed:
        fail.append("checks failed: %s" % ["%s (%s)" % (c.get("id"), c.get("detail")) for c in failed])
    if removal.get("errors"):
        fail.append("while following the removal: %s" % removal["errors"])
    ctx = r["ctx"]
    preview = removal.get("preview") or {}
    if sorted(preview.get("users") or []) != sorted(ctx["users"]):
        review.append("the confirmation lists the users %s, the machine has %s" % (preview.get("users"), ctx["users"]))
    for f, rv in (judge_files(app, entry, ctx, r["d12"], others), judge_packages(entry, r["d12"]),
                  judge_system(app, entry, r["d12"], r["d02"]),
                  judge_archive(entry, ctx, r.get("table_paths", {}), r.get("archive_report"), removal, r.get("traps"))):
        fail += f
        review += rv
    review += judge_leftovers(app, entry, ctx, r["d02"], r.get("created", ()))
    changed = sorted(p for p, h in r["witness"]["before"].items() if r["witness"]["after"].get(p) != h)
    if changed:
        fail.append("the user's files changed or disappeared: %s" % changed)
    if not r["witness"]["before"]:
        fail.append("no witness file was checked")
    down = [c for c in r.get("panel", []) if c != "200"]
    if down or not r.get("panel"):
        fail.append("the panel did not always answer during the removal: %s" % (down[:10] or "never asked"))
    for gone in r.get("table_paths", {}).get("gone", {}).items():
        if not gone[1]:
            review.append("a path the table says it deletes did not exist before: %s" % gone[0])
    again = r.get("reinstall") or {}
    if reinstall and again.get("result") != "installed":
        fail.append("reinstalling it failed: %s" % again.get("result"))
    elif reinstall and r.get("install", {}).get("web") != again.get("web"):
        review.append("its page answers %s after the reinstall, %s after the first installation"
                      % (again.get("web"), r.get("install", {}).get("web")))
    restore = r.get("restore")
    if restore is not None:
        if not restore.get("before") or restore.get("after") != restore.get("before"):
            fail.append("restoring the copy did not bring its settings back: %s" % restore)
        if not restore.get("active"):
            fail.append("it does not run after restoring the copy: %s" % restore)
    return {"verdict": "fail" if fail else "pass", "fail": fail, "review": review}


def judge_watch(app, r, affected):
    """The neighbours that must come through: still installed, their services as they were (those that use the
    removed app may stop working, as the confirmation says: noted for review)."""
    fail, review = [], []
    for other, before in (r.get("watch_before") or {}).items():
        after = (r.get("watch_after") or {}).get(other, {})
        if before.get("lock") and not after.get("lock"):
            fail.append("%s is no longer installed" % other)
        for unit, state in before.get("units", {}).items():
            now = after.get("units", {}).get(unit)
            if now is None:
                fail.append("the service %s of %s is gone" % (unit, other))
            elif now != state or after.get("web") != before.get("web"):
                note = "%s changed: %s %s -> %s %s" % (other, unit, state, now, "web %s -> %s" % (before.get("web"), after.get("web")))
                (review if other in affected else fail).append(note)
        if not before.get("units") and after.get("web") != before.get("web"):
            (review if other in affected else fail).append("the page of %s answers %s, before %s"
                                                          % (other, after.get("web"), before.get("web")))
    return fail, review


def judge_failure(app, entry, r, expect, text):
    """A failure caused on purpose: refused or put back with nothing removed, or a result that tells the truth."""
    fail, review = [], []
    removal = r.get("removal") or {}
    last = r.get("last") or {}
    result = last.get("result") if "helper-closed" in removal.get("errors", []) else removal.get("result")
    message = " ".join(str(x) for x in (removal.get("message"), removal.get("preview_error"), removal.get("preview_message"),
                                        removal.get("start_error"), last.get("message")) if x)
    d12 = r.get("d12") or {}
    before, after = r.get("state_before"), r.get("state_after")
    panel = r.get("panel") or []
    if not panel or any(c != "200" for c in panel):
        fail.append("the panel did not always answer: %s" % ([c for c in panel if c != "200"][:10] or "never asked"))
    if expect in ("refused", "put-back"):
        if result == "removed":
            fail.append("it was uninstalled anyway")
        if text and text not in message:
            fail.append("the reason given does not say '%s': %s" % (text, message or "nothing"))
        if before != after:
            fail.append("not as it was before: %s -> %s" % (before, after))
        taken = [p for p in d12.get("removed", {}) if under(p, scope(app, entry, r["ctx"])) and not TRANSIENT.search(p)]
        if taken:
            fail.append("removed although it was %s: %s" % ("refused" if expect == "refused" else "put back", tops(taken)))
        if d12.get("packages_removed"):
            fail.append("packages removed: %s" % sorted(d12["packages_removed"]))
        copies = [a for a in r.get("archives_after", []) if MARK not in a]
        if copies:
            fail.append("a copy was left behind: %s" % copies)
        review.append("result %s: %s" % (result, message))
    elif expect == "consistent":
        checks = removal.get("checks") or last.get("checks") or []
        if result == "removed":
            if after.get("lock") or not all(c.get("ok") for c in checks):
                fail.append("reported removed, but: lock %s, checks %s" % (after.get("lock"), [c for c in checks if not c.get("ok")]))
        elif result == "failed":
            if before != after:
                fail.append("reported failed (nothing removed), but not as before: %s -> %s" % (before, after))
        elif result == "incomplete":
            review.append("reported incomplete (Try again offered)")
        else:
            fail.append("no result: %s" % message)
        review.append("result %s: %s" % (result, message))
    elif expect == "nginx-broken":
        if result != "removed":
            fail.append("result %s: %s" % (result, message))
        check = [c for c in removal.get("checks") or [] if c.get("id") == "nginx"]
        if not check or not check[0].get("ok") or "broken before" not in check[0].get("detail", ""):
            fail.append("the nginx check does not say it was broken before: %s" % check)
        count = (r.get("after_fix") or "").strip().splitlines()[-1:]  # after nginx -t's own messages
        if count not in ([], ["0"]):
            fail.append("once nginx was fixed, its location was still served (%s)" % count)
    return {"verdict": "fail" if fail else "pass", "fail": fail, "review": review}


def judge_panel(out):
    """Through the real panel: no token, a wrong password and a wrong name are refused before anything starts."""
    fail = []
    if not str(out.get("without_token", "")).startswith("refused"):
        fail.append("the panel accepted a connection without the page's token: %s" % out.get("without_token"))
    if out.get("wrong_password") != "password":
        fail.append("a wrong password was not refused: %s" % out.get("wrong_password"))
    if out.get("wrong_name") != "confirm-mismatch":
        fail.append("a wrong name was not refused: %s" % out.get("wrong_name"))
    if not (out.get("started") or {}).get("ok"):
        fail.append("the panel did not start the removal: %s" % out.get("started"))
    return fail


def verdict_for(spec, entry, r):
    """The verdict on one run of remove.py, whatever its kind."""
    app = spec["app"]
    expect = spec.get("expect")
    v = verdict(app, entry, r, spec.get("reinstall", False), spec.get("others", ())) if expect is None else \
        judge_failure(app, entry, r, expect, spec.get("text", ""))
    if spec.get("via_panel"):
        v["fail"] += judge_panel((r.get("removal") or {}).get("panel") or {})
    f, rv = judge_watch(app, r, spec.get("affected", ()))
    v["fail"] += f
    v["review"] += rv
    restore = r.get("restore")
    if restore and restore.get("before_restore") and restore.get("before_restore") == restore.get("before"):
        v["review"].append("the reinstall drew the same value: the restore proves little")
    if v["fail"] and v["verdict"] == "pass":
        v["verdict"] = "fail"
    return v
