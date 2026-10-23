# App Store compatibility campaign

Installs every application the App Store offers, each in a fresh throwaway Swizzin, through the
App Store helper exactly as the page does, and records for each one: the result, `box test`, the
time it took, the questions the installer asked and the HTTP status of its page behind nginx.

- `client.py` runs inside the container: it starts the installation on the helper's socket, answers
  each question with its default (Enter), or with a throwaway password when a password is asked,
  and prints one JSON line.
- `run.py` runs on the host: one fresh container per app, one at a time, removed afterwards. An app
  that needs another one first (ruTorrent needs rTorrent...) starts from a copy of the container where
  that one was installed.

Nothing is published: the containers have no port open to the outside.

## Requirements

- Docker 28 or later: the containers run systemd with private, writable cgroups, without `--privileged`.
- About 25 GB of free disk space; each container gets 2 CPUs and 4 GB of memory.

## 1. The base image: a fresh Swizzin with the App Store

```bash
docker build -t swz-test:24.04 - <<'EOF'
FROM ubuntu:24.04
ENV container=docker
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      systemd systemd-sysv dbus sudo curl git whiptail ca-certificates lsb-release locales \
 && apt-get clean && rm -rf /var/lib/apt/lists/*
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
EOF

docker run -d --name swz-base --hostname swz-base --cgroupns=private --security-opt writable-cgroups=true \
  --tmpfs /run --tmpfs /run/lock --memory 4g --cpus 2 --pids-limit 4096 swz-test:24.04
docker exec swz-base bash -c 'curl -fsSL https://s5n.sh -o /root/setup.sh &&
  bash /root/setup.sh --unattend --user test --pass Essai-Swz-2026 --skip-cracklib nginx panel'

# this repository's panel and App Store (run from the repository root). git archive writes its files
# writable by the group (664), and store-helper/install.sh refuses files that another user could change.
# The panel needs its copy of store-helper/: without it, the App Store helper does not run (PANEL_MARKER).
git archive HEAD | docker exec -i swz-base bash -c 'mkdir -p /root/fork && tar -x -C /root/fork && chmod -R go-w /root/fork'
docker exec swz-base bash -c 'cd /root/fork && cp -a swizzin.py /opt/swizzin/ && cp -a core static templates store-helper /opt/swizzin/ &&
  chown -R swizzin:swizzin /opt/swizzin && bash store-helper/install.sh && systemctl restart panel'

docker stop swz-base && docker commit swz-base swz-camp:base && docker rm swz-base
```

## 2. The campaign

```bash
python3 tests/campaign/run.py --image swz-camp:base --out results/            # every app offered
python3 tests/campaign/run.py --image swz-camp:base --out results/ sonarr qui  # only these
```

Add `--docker "sudo -n docker"` if your user cannot run docker directly, and `--login user:password`
if the base image's Swizzin user is not `test:Essai-Swz-2026`. An app already in `results.jsonl` is
skipped (`--again` runs it again), so a stopped campaign carries on where it was. The campaign stops
by itself when less than 20 GB are left (`--min-free-gb`).

In `results/`: `results.jsonl` (one line per app), `results.md` (a table) and, for each app, the
terminal transcript and Swizzin's own log of the installation.

## Reading the results

- `installed` / `failed` / `interrupted`: whether the app's lock file appeared (Swizzin's own
  definition of an installed app).
- Checks: `box test <app>`. "generic" means Swizzin has no test of its own for this app and used its
  generic one, which guesses the app's port from Swizzin's nginx template: for apps whose port is
  chosen at install time it cannot find it and fails although the app works. The Web column
  (status of the app's page behind nginx, with the panel login) tells these apart.
- Questions: what the installer asked, as seen in the terminal, and the answer given.

# Uninstall campaign

An app is offered for uninstalling only after it passed this campaign (`tested=True` in
`store-helper/removable.py`). `remove.py` runs each case in a fresh throwaway container, removes it
afterwards, and judges it with `analyse.py` (tested by `tests/test_campaign_analyse.py`).

- **Cycles**: every app of the table is installed, then given witness files, then uninstalled through
  the helper's socket exactly as the page does it, then installed again.
  - The witness files are downloads and media of both users, and files in the folders the app keeps.
  - Traps are put in its settings folder: links to a user's file and folder, a FIFO and a file of
    another user. None of them may enter the copy.
  - For autobrr, Transmission, qBittorrent, Lidarr, Prowlarr and Plex, the copy is then restored by
    following its `RESTORE.txt`. A value drawn at random at install time (a secret, an API key, a
    password hash, Plex's machine id) must come back.
- **Neighbours**: one app is removed while another one is installed. The other one must still be
  installed, with its services and its page as before. Only an app that the confirmation names as
  affected may change, and that is noted for review. Cases: qBittorrent with qui, with autobrr and with
  Deluge (refused), Plex with Tautulli, Prowlarr with Radarr.
- **Failures**: each is caused on purpose, on autobrr (Transmission for the restart during Swizzin's
  script).
  - These must be refused with nothing touched: dpkg's lock held, apt-daily running, a process named
    apt-get, a remove script or a sourced file of Swizzin changed, too little room for the copy.
  - These must put everything back: the disk filling up during the copy, Stop during the copy, the
    service restarted or killed during the copy.
  - A restart while Swizzin's script runs must end with a result that tells the truth.
  - When nginx's configuration was already broken, the removal goes on without reloading nginx, and says so.
- **Panel**: one removal goes through the real panel (Socket.IO, the page's token, the admin password),
  after checking that no token, a wrong password and a wrong name are refused.

## The base image

`swz-camp:base` (above) with a second Swizzin user, so that every removal concerns two users:

```bash
python3 tests/campaign/remove.py --base --docker "sudo -n docker"     # makes swz-rm:base
```

Each container then gets this working tree's panel and App Store, and its helper reads a table where every
app is marked tested (the campaign is what decides it): `--table`, a test option of the helper.

## Running it

```bash
python3 tests/campaign/remove.py --docker "sudo -n docker" --out results-remove/                 # everything
python3 tests/campaign/remove.py --docker "sudo -n docker" --out results-remove/ cycles          # one group
python3 tests/campaign/remove.py --docker "sudo -n docker" --out results-remove/ --only plex panne-stop-copie
```

Images with prerequisites installed (rTorrent for Flood, qBittorrent for qui, Plex for Tautulli, autobrr
and Transmission for the failures) are made once and kept: `swz-rm:with-<app>`. A case already in
`results.jsonl` is skipped (`--again` runs it again). The campaign stops below 20 GB of free space, and
waits when the anonymous GitHub API quota of the address is low (many installers use it).

## Reading the results

`results.md` has one line per case: `pass`, `fail` (with the reasons), `not-tested` (the app did not
install) or `error` (the campaign's own trouble). For each case, `<key>/` holds the transcripts of the
installation, the removal and the reinstall, the helper's logs, Swizzin's log, and the differences between
the photos of the container (`d12`: just before and just after the removal; `d02`: before the installation
and after the removal).

A case fails when:
- the uninstaller's result is not `removed` with every check passed;
- anything outside what the table allows for the app disappeared: its folders, unit files, nginx files,
  lock, Swizzin db entries, and the files of the packages it may remove;
- a file changed in a home folder outside that scope, or a witness file changed;
- a package was removed that the table does not allow, or a package was installed or upgraded;
- its units, nginx files or lock are still there, or `nginx -t` fails;
- the panel stopped answering during the removal (it is asked every 2 seconds);
- the copy is missing a setting, can be read by others, does not match its manifest, has no useful
  `RESTORE.txt`, or followed a trap;
- the reinstall or the restore failed.

"To review" lists what is not wrong in itself but needs a human eye: what the installation left behind
(files, packages, system users), a user deleted with its package, a path of the table that never existed.
