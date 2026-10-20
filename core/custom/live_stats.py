"""Statistiques en direct du tableau de bord : CPU, RAM et disque, émises chaque seconde sur le websocket du panel.

Ajouté le 2026-10-09 (cartes CPU Info, RAM Info et Disk Info actualisées à la seconde).
Ce fichier est dans core/custom/ : la mise à jour du panel le conserve. Voir le README (Live cards).
Charge moyenne et top des programmes retirés le 2026-10-09 : plus aucun relevé des processus.
Alerte « disque bientôt plein » ajoutée le 2026-10-20 : niveau calculé ici pour chaque disque, bandeau affiché
par static/js/live_stats.js. Seuils DISK_WARN_PERCENT et DISK_CRIT_PERCENT dans swizzin.cfg."""
import os
import time
import core.util as util  # import du module seulement : core.util et swizzin.py s'importent mutuellement au démarrage


RELIRE_DISQUES = 30  # secondes entre deux relectures de la liste des disques
SEUILS_DEFAUT = (90, 95)  # % d'occupation : avertissement (orange) puis critique (rouge), réglables dans swizzin.cfg


def lire_seuils(config, journal=None):
    """(avertissement, critique) en % depuis swizzin.cfg (DISK_WARN_PERCENT, DISK_CRIT_PERCENT).

    0 coupe ce niveau d'alerte. Une valeur invalide (texte, négative, plus de 100...) reprend le défaut,
    avec un avertissement dans le journal : un réglage mal écrit ne doit pas empêcher le panel de démarrer.
    Un avertissement supérieur ou égal au critique ne se verrait jamais : l'orange est coupé, le rouge reste,
    avec un avertissement dans le journal."""
    out = []
    for cle, defaut in zip(('DISK_WARN_PERCENT', 'DISK_CRIT_PERCENT'), SEUILS_DEFAUT):
        valeur = config.get(cle, defaut)
        try:
            if isinstance(valeur, bool):  # True vaudrait 1 %
                raise ValueError(valeur)
            seuil = float(valeur)
            if not 0 <= seuil <= 100:  # écarte aussi nan et inf
                raise ValueError(valeur)
        except (TypeError, ValueError, OverflowError):  # OverflowError : un entier trop grand pour float()
            if journal is not None:
                journal.warning("swizzin.cfg : %s = %r invalide (attendu : 0 à 100), %s utilisé",
                                cle, valeur, defaut)
            seuil = defaut
        out.append(seuil)
    avertissement, critique = out
    if avertissement and critique and avertissement >= critique:
        if journal is not None:
            journal.warning("swizzin.cfg : DISK_WARN_PERCENT (%g) n'est pas sous DISK_CRIT_PERCENT (%g) : "
                            "alerte orange coupée, rouge dès %g %%", avertissement, critique, critique)
        avertissement = 0
    return avertissement, critique


def niveau_alerte(pct, seuils):
    """'critical', 'warning' ou None pour un disque occupé à `pct` %.

    Un seuil à 0 est coupé. Si les deux seuils sont atteints, le critique l'emporte."""
    avertissement, critique = seuils
    if critique and pct >= critique:
        return "critical"
    if avertissement and pct >= avertissement:
        return "warning"
    return None


def _cpu():
    """Compteurs de /proc/stat : {'cpu': [user, nice, system, idle, iowait, irq, softirq, steal], 'cpu0': [...], ...}."""
    out = {}
    with open('/proc/stat') as f:
        for ligne in f:
            if not ligne.startswith('cpu'):
                break
            champs = ligne.split()
            valeurs = [int(x) for x in champs[1:9]]
            out[champs[0]] = valeurs + [0] * (8 - len(valeurs))
    return out


def _repartition(avant, apres):
    """Pourcentages d'utilisation entre deux relevés d'une même ligne de /proc/stat."""
    user, nice, system, idle, iowait, irq, softirq, steal = (b - a for a, b in zip(avant, apres))
    total = (user + nice + system + idle + iowait + irq + softirq + steal) or 1
    return {
        "used": (total - idle - iowait) / total * 100,
        "user": (user + nice) / total * 100,
        "system": (system + irq + softirq) / total * 100,
        "iowait": iowait / total * 100,
        "steal": steal / total * 100,
    }, total


def _peripheriques():
    """{point de montage affiché par le panel: nom du périphérique dans /proc/diskstats}."""
    montages = set(util.get_mounts())
    out = {}
    with open('/proc/mounts') as f:
        for ligne in f:
            dev, mnt = ligne.split()[:2]
            mnt = util.unescape_mount(mnt)
            if mnt in montages and dev.startswith('/dev/'):
                out[mnt] = os.path.basename(os.path.realpath(dev))
    return out


def _disques():
    """{périphérique: (secteurs lus, secteurs écrits)} ; un secteur de /proc/diskstats vaut toujours 512 octets."""
    out = {}
    with open('/proc/diskstats') as f:
        for ligne in f:
            c = ligne.split()
            if len(c) > 9:
                out[c[2]] = (int(c[5]), int(c[9]))
    return out


def _ram():
    """Mêmes champs et mêmes calculs que la route /stats/ram du panel."""
    m = {}
    with open('/proc/meminfo') as f:
        for ligne in f:
            cle, valeur = ligne.split(':', 1)
            m[cle] = int(valeur.split()[0])
    total, dispo = m['MemTotal'], m['MemAvailable']
    return {"ramtotal": util.GetHumanReadableKiB(total), "ramfree": util.GetHumanReadableKiB(dispo),
            "ramused": util.GetHumanReadableKiB(total - dispo),
            "perutil": '{0:.2f}'.format((total - dispo) / total * 100)}


def _modele():
    """Nom et fréquence du processeur (lus une seule fois)."""
    nom, mhz = "", 0.0
    with open('/proc/cpuinfo') as f:
        for ligne in f:
            if ligne.startswith('model name') and not nom:
                nom = ligne.split(':', 1)[1].strip()
            elif ligne.startswith('cpu MHz') and not mhz:
                mhz = float(ligne.split(':', 1)[1])
    return nom, ('{0:.2f} GHz'.format(mhz / 1000) if mhz else "")


def mesure(avant, apres, duree, modele, freq, devs, seuils=SEUILS_DEFAUT):
    """Contenu de l'événement « live » à partir de deux relevés (cpu, disques) espacés de `duree` secondes.

    Chaque disque lisible porte son niveau d'alerte (« alert ») selon `seuils`,
    pour le bandeau « disque bientôt plein »."""
    (cpu_a, disq_a), (cpu_b, disq_b) = avant, apres
    total, _ = _repartition(cpu_a['cpu'], cpu_b['cpu'])
    coeurs = sorted((k for k in cpu_b if k != 'cpu'), key=lambda k: int(k[3:]))
    ncpu = len(coeurs) or os.cpu_count() or 1
    f = '{0:.1f}'.format
    disques = {}
    for mnt, dev in devs.items():
        try:
            if not os.path.ismount(mnt):  # disque démonté : le dossier vide donnerait les chiffres du disque parent
                raise OSError("plus monté")
            taille, utilise, libre, pct = util.disk_usage(mnt)
        except OSError:  # un montage illisible ne doit pas figer les cartes CPU et RAM
            disques[mnt] = {"unavailable": True}  # la carte affiche « — » au lieu de chiffres périmés
            continue
        lu, ecrit = (b - a for a, b in zip(disq_a.get(dev, (0, 0)), disq_b.get(dev, (0, 0))))
        disques[mnt] = {"disktotal": taille, "diskused": utilise, "diskfree": libre, "perutil": pct,
                        "read": util.GetHumanReadableBi(lu * 512 / duree) + "/s",
                        "write": util.GetHumanReadableBi(ecrit * 512 / duree) + "/s",
                        "alert": niveau_alerte(float(pct), seuils)}
    return {
        "cpu": {"used": f(total["used"]), "idle": f(100 - total["used"]), "cores": ncpu, "model": modele, "freq": freq,
                "user": f(total["user"]), "system": f(total["system"]), "iowait": f(total["iowait"]),
                "steal": f(total["steal"]),
                "percore": [f(_repartition(cpu_a[k], cpu_b[k])[0]["used"]) for k in coeurs if k in cpu_a]},
        "ram": _ram(),
        "disks": disques,
    }


def live_stats(app, socketio):
    """Tâche de fond lancée à la première connexion au websocket : un relevé par seconde, émis à tous les navigateurs."""
    with app.app_context():
        modele, freq = _modele()
        seuils = lire_seuils(app.config, app.logger)  # swizzin.cfg n'est lu qu'au démarrage du panel
        devs, relire = _peripheriques(), time.monotonic() + RELIRE_DISQUES
        avant, t_avant = None, None
        while True:
            try:
                if time.monotonic() >= relire:  # un disque branché ou retiré pendant que le panel tourne
                    devs, relire = _peripheriques(), time.monotonic() + RELIRE_DISQUES
                apres, t_apres = (_cpu(), _disques()), time.monotonic()
                if avant is not None:
                    socketio.emit('live', mesure(avant, apres, max(t_apres - t_avant, 0.001), modele, freq, devs,
                                                 seuils), namespace='/websocket')
                avant, t_avant = apres, t_apres
            except Exception:
                app.logger.exception("live_stats : relevé impossible")
                avant = None
            socketio.sleep(1)
