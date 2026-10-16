# swizzin dashboard — Heliedan's edition (unofficial)

> **This branch (`heliedan`) is my personal version of the [Swizzin dashboard](https://github.com/swizzin/swizzin_dashboard), the one that runs on my own server.**
> It is **not official** and not endorsed by the Swizzin team. Feel free to look at it or try it, at your own risk, with no promise of support.
> The original README follows further down.

![Dashboard with the live CPU, RAM, network and disk cards](docs/screenshots/dashboard.png)

<img src="docs/screenshots/disk-info.png" alt="Disk Info card: one box per disk, an unreadable or unmounted disk marked UNAVAILABLE" width="360">

*Screenshots made with demo data: the dashboard with the live cards, then the Disk Info card, one box per disk, with a disk that went away shown as UNAVAILABLE.*

### What is different from upstream (`master`, b2eca76)

| Change | Proposed upstream |
|---|---|
| **The websocket refuses clients that are not logged in.** The HTTP routes were protected but the websocket handler was not, so anyone able to reach the panel could receive its live broadcasts. | [#83](https://github.com/swizzin/swizzin_dashboard/pull/83) |
| **Errors in custom profiles are logged** instead of silently making every custom tile disappear. | [#86](https://github.com/swizzin/swizzin_dashboard/pull/86) |
| **Service Info keeps refreshing** after a failed poll (it used to freeze until the page was reloaded). | [#87](https://github.com/swizzin/swizzin_dashboard/pull/87) |
| **Disk statistics survive a bad mount:** a path containing a space (written `\040` by Linux) is decoded, and one unreadable mount no longer makes `/stats/disk` fail with HTTP 500. | not proposed yet |
| **Login protection:** after 5 *different* wrong passwords within 10 minutes, an address is refused for 15 minutes (HTTP 429, and the login page says how long to wait), even with the right password. The same wrong password sent again, by a forgotten tab for example, counts only once, and a successful login does not reset the count. The address is the one the local nginx passes on: when the panel is reached directly, the forwarded header is not trusted. Settings in `swizzin.cfg`: `LOGIN_MAX_FAILURES` (number of passwords), `LOGIN_FAILURE_WINDOW` and `LOGIN_BLOCK_TIME` (seconds); `LOGIN_MAX_FAILURES = 0` turns it off. Restarting the panel lifts every block. | not proposed yet |
| **App Store** (admin only, new entry in the sidebar): the applications Swizzin can install (system components aside), with their description, installed or not, a search box and filters. **Install** runs `box install <app>` on the server in a live terminal in the page, as over SSH, whiptail menus and questions included. See [App Store](#app-store) below. | not proposed yet |
| <a id="live-cards"></a>**Live cards:** CPU Info (replacing System Info), RAM Info and Disk Info are grouped on the left and updated every second over the existing websocket; At a Glance is removed (its badges sit in each card header). Files: `core/custom/live_stats.py`, `templates/cpuinfo.html`, `templates/raminfo_live.html`, `templates/diskinfo_live.html`, `static/js/live_stats.js`, `static/css/live_stats.css`. Code comments in this part are in French. | idea: [#85](https://github.com/swizzin/swizzin_dashboard/issues/85) |

In the live Disk Info card, **each disk has its own light box** with its read/write speeds, and a disk that becomes unreadable or is unmounted while the page is open is shown as **UNAVAILABLE** (crossed circle ⊘ in the header, since the coloured badges stand for how full a disk is) instead of freezing the CPU and RAM cards or showing stale numbers. Disks plugged in or removed are picked up within 30 seconds; hovering a badge shows its percentage.

### App Store

![The App Store page: applications installed, available, greyed out with the reason, or with a note](docs/screenshots/store-catalogue.png)

<img src="docs/screenshots/store-menu.png" alt="Installing qBittorrent from the App Store: the installer's version menu in the page's terminal" width="49%"> <img src="docs/screenshots/store-done.png" alt="An installation finished: installed, checks passed, and a button to open the app" width="49%">

*Screenshots from a throwaway test server: the App Store page, the qBittorrent installer's version menu in the page's terminal, and the end of an installation (autobrr).*

The panel runs as the `swizzin` user, which cannot even read the list of Swizzin applications (`/etc/swizzin` belongs to root). A small root service, **swizzin-store** (`store-helper/`, Python standard library only), lists the applications and runs the installations; the panel relays them to the page. Without the service, the page only explains how to install it:

```bash
# as root, from a copy of this branch that the panel user cannot write
git clone -b heliedan https://github.com/Heliedan/swizzin_dashboard /root/swizzin_dashboard
bash /root/swizzin_dashboard/store-helper/install.sh      # --uninstall removes it
```

- **One installation at a time:** `box install <app>`, then `box test <app>` once the app's lock file is there, and the page shows the result. It keeps going if the tab is closed or the panel restarts (some installers restart nginx or the panel): the page reconnects and replays the output.
- **Whoever started it types in the terminal;** other tabs watch until they take control. A row of keys (arrows, Tab, Enter, Esc, Ctrl+C) helps on a tablet.
- **Not offered:** system components and tools set up on the machine itself (nginx, Let's Encrypt, WireGuard, quota, rclone, ffmpeg, vsftpd, DuckDNS, Webmin, Shellinabox, X2Go, the panel itself), and xmrig. Install those over SSH.
- **Security:** the page and its websocket are for the admin only, logged in with the panel password; the websocket also wants a token that only the panel's own pages can read, and refuses other sites. Before installing or taking control, the admin password is asked again (wrong ones count as failed logins). `STORE_ASK_PASSWORD = False` in `swizzin.cfg` turns that off: keep it on if your panel is reachable from the Internet or used from shared computers, since a panel login never expires by itself. The service accepts only root and the panel user, runs nothing but `box install` and `box test` with a name from Swizzin's own list, and gives the installers a clean environment. Each installation is logged in `/var/log/swizzin-store` (root only); what is typed is not logged, only how much.
- **New web addresses:** an installation usually adds an nginx location for the new app. If you restrict access to your server (allowed addresses, VPN), check it.
- **Not there yet:** updating or removing an app from the page (`box upgrade` and `box remove` over SSH).

#### Compatibility

Tested on 2026-10-16 with [`tests/campaign/`](tests/campaign/README.md): every application installed through the App Store, each in a fresh throwaway Swizzin (Ubuntu 24.04, nginx and panel), then checked with Swizzin's `box test`, its page behind nginx and its service log. The App Store aims at installing in one click: what needs more is left out for now. Results depend on each project at that date; other systems were not tested.

- **One click, and it works (17):** autobrr, Bazarr, Deluge, Filebrowser, Flood, Jellyfin, Lidarr, Medusa, Mylar, Netronome, NZBGet, Organizr, Plex, Prowlarr, qBittorrent, qui, Radarr. Their questions, if any, are fine with the default answer (version menus, Plex's optional claim token).
- **Offered with a note on their card (15):**
  - *Page with no Swizzin password in front:* Airsonic (default login admin / admin), Emby (the first visitor creates the admin account), LibreSpeed (public speed test), Tautulli (until a login is set in its settings).
  - *Own page also open on a port of the server, outside nginx* (a firewall should block it): Emby 8096, Jackett 9117, NZBHydra 5076, SickGear 8081, Transmission 9091, Resilio Sync 8888.
  - *Ports the app is meant to open:* Quassel 4242 (for the Quassel apps), Syncthing 22000 (syncing).
  - *Long installations:* rTorrent about 20 minutes (compiled; on Ubuntu 24.04 it also installs Performance Co-Pilot, which opens ports 44321 to 44323 and 4330), Tautulli about 15, ruTorrent about 6.
  - *One question to answer differently:* Calibre-Web offers to install Calibre: answer n (see below).
  - *Sonarr:* its installer gives up when Sonarr takes more than 30 seconds to start, which happened once in three tries on a busy test machine: installing it again works.
- **Not offered for now (12):** Calibre and Calibre Content Server (Swizzin's Calibre installer fails on Ubuntu 24.04, [swizzin#1212](https://github.com/swizzin/swizzin/issues/1212)), The Lounge (needs Node.js 22, Swizzin installs Node.js 20), Ombi (its own package fails, [Ombi#5448](https://github.com/Ombi-app/Ombi/issues/5448)), SABnzbd and jfa-go (their page is not found at the address Swizzin sets up for them), SickChill (its service stops right after starting), Rapidleech (Swizzin's web setup does not work, and the project is no longer maintained), Nextcloud (three passwords to type, then missing PHP modules), ZNC (many questions in the terminal), Navidrome and Netdata (not checked yet: their service needs system rights that the throwaway test container does not give).

Swizzin's own checks report a failure for some apps that work: the generic check cannot always find the app's port, or looks for a service under another name. The page says so for these apps instead of reporting a failure. A failed check is also run again a minute later, since some apps take a while to start.

The proposals stay open upstream. When the official project publishes changes, I merge them into this branch.

### Trying it

Swizzin installs the panel from the official repository, so switching is manual and meant for people comfortable with git: back up `/opt/swizzin`, fetch this branch there, then restart the panel (`systemctl restart panel`). Your custom tiles (`core/custom/profiles.py`) are not touched. The App Store also needs its service (see [App Store](#app-store)). `box upgrade panel` brings the official version back; the App Store service then stays installed but unused until you remove it (`install.sh --uninstall`).

### Transparency

I am not a developer: these changes were written with the help of an AI assistant (Claude). I run them on my own server and test them there.

### En français

Version personnelle et non officielle du panel Swizzin, celle qui tourne sur mon serveur : trois correctifs proposés au projet officiel (#83, #86, #87), des cartes CPU, RAM et disque actualisées chaque seconde (idée #85), une limite des essais de mot de passe et une banque d'applications (App Store) : installer une application Swizzin depuis le panel, dans un terminal en direct. Libre à chacun de la tester, à ses risques, sans garantie de suivi.

---

![swizzin](http://i.imgur.com/JZlDKP1.png)


# swizzin dashboard v0.1

[website](https://swizzin.ltd) | [docs](https://docs.swizzin.ltd) | [discord](https://discord.gg/bDFqAUF)


### What is this?
This panel offers a quick overview of installed applications, information about their status and information about the server in general. It is written in Python 3 and based on the Flask web server. Its main goals are to be performant, quick and easier to customize than the previous incarnation which was based on the quickbox_dashboard and based on PHP.

### Quick Start:

This package is included in the swizzin repository of applications. To easily install it, you simply need to use box:

```
sudo box install panel
```

If you are adapting this package to work outside of a swizzin environment, the steps necessary for install are outlined in [setup.sh](https://raw.githubusercontent.com/liaralabs/swizzin_dashboard/master/setup.sh)

#### Dependencies

- Python 3.6+
- Flask
- Flask-htpasswd
- Flask-SocketIO
- eventlet

### Support and Help

If you have any questions, please read the [documentation](https://docs.swizzin.ltd/applications/panel) first. If you still have questions or would like to bounce some ideas off other humans, feel free to join us in [discord](https://discord.gg/bDFqAUF).

Do not use GitHub issues for technical support or feature requests. GitHub issues are only to be used to report bugs and other issues with the project.


### Donations

I accept donations on the [project website](https://swizzin.ltd/#donate) and also through [Liberapay](https://liberapay.com/liara/). Please consider a donation if you enjoy the project.

If you don't have spare funds, then you might consider donating the idle cycles on your CPU to my mining pool. Setting it up is easy and will cost you nothing. Simply issue the command:
```
box install xmrig
```
The amount you choose to donate to me is up to you, though the minimum is 1.0. If you need help in setting up your own wallet, check out the [Monero Project](https://getmonero.org).
