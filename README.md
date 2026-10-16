# swizzin dashboard — Heliedan's edition (unofficial)

> **This branch (`heliedan`) is my personal version of the [Swizzin dashboard](https://github.com/swizzin/swizzin_dashboard), the one that runs on my own server.**
> It is **not official** and not endorsed by the Swizzin team. Feel free to look at it or try it, at your own risk, with no promise of support.
> The original README follows further down.

### What is different from upstream (`master`, b2eca76)

| Change | Proposed upstream |
|---|---|
| **The websocket refuses clients that are not logged in.** The HTTP routes were protected but the websocket handler was not, so anyone able to reach the panel could receive its live broadcasts. | [#83](https://github.com/swizzin/swizzin_dashboard/pull/83) |
| **Errors in custom profiles are logged** instead of silently making every custom tile disappear. | [#86](https://github.com/swizzin/swizzin_dashboard/pull/86) |
| **Service Info keeps refreshing** after a failed poll (it used to freeze until the page was reloaded). | [#87](https://github.com/swizzin/swizzin_dashboard/pull/87) |
| <a id="live-cards"></a>**Live cards:** CPU Info (replacing System Info), RAM Info and Disk Info are grouped on the left and updated every second over the existing websocket; At a Glance is removed (its badges sit in each card header). Files: `core/custom/live_stats.py`, `templates/cpuinfo.html`, `templates/raminfo_live.html`, `templates/diskinfo_live.html`, `static/js/live_stats.js`, `static/css/live_stats.css`. Code comments in this part are in French. | idea: [#85](https://github.com/swizzin/swizzin_dashboard/issues/85) |

The proposals stay open upstream. When the official project publishes changes, I merge them into this branch.

### Trying it

Swizzin installs the panel from the official repository, so switching is manual and meant for people comfortable with git: back up `/opt/swizzin`, fetch this branch there, then restart the panel (`systemctl restart panel`). Your custom tiles (`core/custom/profiles.py`) are not touched. `box upgrade panel` brings the official version back.

### Transparency

I am not a developer: these changes were written with the help of an AI assistant (Claude). I run them on my own server and test them there.

### En français

Version personnelle et non officielle du panel Swizzin, celle qui tourne sur mon serveur : trois correctifs proposés au projet officiel (#83, #86, #87) et des cartes CPU, RAM et disque actualisées chaque seconde (idée #85). Libre à chacun de la tester, à ses risques, sans garantie de suivi.

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
