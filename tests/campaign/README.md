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

# this repository's panel and App Store (run from the repository root)
git archive HEAD | docker exec -i swz-base bash -c 'mkdir -p /root/fork && tar -x -C /root/fork'
docker exec swz-base bash -c 'cd /root/fork && cp -a swizzin.py /opt/swizzin/ && cp -a core static templates /opt/swizzin/ &&
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
