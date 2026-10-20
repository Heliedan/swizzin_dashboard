/* 2026-10-09 : met à jour chaque seconde les cartes CPU Info, RAM Info et Disk Info (événement « live » du websocket).
   Mêmes identifiants et, pour CPU et RAM, mêmes seuils (75 % / 90 %) que les scripts d'origine du panel
   (templates/macros.html).
   2026-10-20 : bandeau « disque bientôt plein ». Les disques prennent la couleur de leur niveau d'alerte, avec les
   seuils DISK_WARN_PERCENT et DISK_CRIT_PERCENT de swizzin.cfg. Voir le README. */
(function () {
  'use strict';

  function niveau(pct) { return pct > 90 ? 'bg-danger' : (pct > 75 ? 'bg-warning' : 'bg-success'); }

  // classe : couleur imposée (disques), sinon celle des seuils 75 % / 90 % (CPU, RAM).
  function couleur(el, pct, classe) {
    if (!el) { return; }
    el.classList.remove('bg-success', 'bg-warning', 'bg-danger', 'bg-secondary', 'disk-unavailable');
    el.classList.add(classe || niveau(pct));
  }

  function barre(el, pct, classe) {
    if (!el) { return; }
    var p = Math.max(0, Math.min(100, Number(pct) || 0));
    el.style.width = p + '%';
    el.setAttribute('aria-valuenow', Math.trunc(p));
    couleur(el, p, classe);
  }

  // Couleur d'un disque : celle de son niveau d'alerte, calculé par le serveur avec DISK_WARN_PERCENT et
  // DISK_CRIT_PERCENT (swizzin.cfg). Mêmes seuils et mêmes couleurs que le bandeau, pas l'échelle 75 % / 90 %.
  function couleurDisque(alerte) {
    return alerte === 'critical' ? 'bg-danger' : (alerte === 'warning' ? 'bg-warning' : 'bg-success');
  }

  function texte(id, valeur) {
    var el = document.getElementById(id);
    if (el) { el.textContent = valeur; }
  }

  function parCoeur(valeurs) {
    var boite = document.getElementById('cpupercore');
    if (!boite) { return; }
    if (boite.children.length !== valeurs.length) {  // construit une fois les lignes « Core n »
      boite.textContent = '';
      valeurs.forEach(function (v, i) {
        var ligne = document.createElement('div');
        ligne.className = 'd-flex align-items-center small mb-1';
        var nom = document.createElement('span');
        nom.style.width = '4.5em';
        nom.textContent = 'Core ' + (i + 1);
        var cadre = document.createElement('div');
        cadre.className = 'progress flex-grow-1 mx-2';
        cadre.style.height = '8px';
        var b = document.createElement('div');
        b.className = 'progress-bar bg-success';
        b.setAttribute('role', 'progressbar');
        cadre.appendChild(b);
        var val = document.createElement('span');
        val.className = 'text-right';
        val.style.width = '3.5em';
        ligne.appendChild(nom); ligne.appendChild(cadre); ligne.appendChild(val);
        boite.appendChild(ligne);
      });
    }
    valeurs.forEach(function (v, i) {
      var ligne = boite.children[i];
      barre(ligne.children[1].firstChild, v);
      ligne.children[2].textContent = v + '%';
    });
  }

  function cpu(c) {
    texte('cpuused', c.used);
    texte('cpuidle', c.idle);
    texte('cpucores', c.cores);
    texte('cpupercent', c.used);
    texte('cpumodel', c.model + (c.freq ? ' · ' + c.freq : ''));
    barre(document.getElementById('cpuprogress'), c.used);
    couleur(document.getElementById('cpuindicator'), Number(c.used));
    var ci = document.getElementById('cpuindicator'); if (ci) { ci.title = 'CPU : ' + c.used + ' %'; }
    parCoeur(c.percore);
    texte('cpuuser', c.user);
    texte('cpusystem', c.system);
    texte('cpuiowait', c.iowait);
    texte('cpusteal', c.steal);
  }

  function ram(r) {
    texte('ramused', r.ramused);
    texte('ramfree', r.ramfree);
    texte('ramtotal', r.ramtotal);
    texte('rampercent', r.perutil);
    barre(document.getElementById('ramprogress'), r.perutil);
    couleur(document.getElementById('ramindicator'), Number(r.perutil));
    var ri = document.getElementById('ramindicator'); if (ri) { ri.title = 'RAM : ' + r.perutil + ' %'; }
  }

  function etatDisque(m, indisponible) {  // badge « UNAVAILABLE » au-dessus du nom du disque, ligne du % masquée
    var cell = document.getElementById(m + 'diskused');
    var nom = cell && cell.parentElement ? cell.parentElement.firstElementChild : null;
    var badge = nom ? nom.querySelector('.disk-badge') : null;
    if (indisponible && nom && !badge) {  // au-dessus du nom du disque
      badge = document.createElement('div');
      badge.className = 'disk-badge mb-1';
      var etiquette = document.createElement('span');
      etiquette.className = 'badge badge-danger';
      etiquette.textContent = 'UNAVAILABLE';
      badge.appendChild(etiquette);
      nom.insertBefore(badge, nom.firstChild);
    }
    if (!indisponible && badge) { badge.parentNode.removeChild(badge); }
    var pct = document.getElementById(m + 'diskpercent');  // « 87.00% » : on vide la ligne sans la masquer (garde le contour)
    if (pct) {
      if (indisponible) { pct.textContent = ''; }
      if (pct.nextSibling && pct.nextSibling.nodeType === 3) { pct.nextSibling.nodeValue = indisponible ? '' : '%'; }
    }
  }

  function disques(d) {
    // Disques affichés sur la page mais absents des données (démontés depuis le chargement) : indisponibles.
    var carte = document.getElementById('carte-disque');
    if (carte) {
      // 1er relevé en direct reçu : le relevé /stats/disk de chaque minute (templates/macros.html) s'arrête.
      if (!carte.hasAttribute('data-direct')) { carte.setAttribute('data-direct', ''); }
      Array.prototype.forEach.call(carte.querySelectorAll('[id$="diskused"]'), function (el) {
        var m = el.id.slice(0, -'diskused'.length);
        if (!(m in d)) { d[m] = { unavailable: true }; }
      });
    }
    Object.keys(d).forEach(function (m) {
      var x = d[m];
      if (x.unavailable) {  // disque illisible (débranché, partage réseau perdu...) : pas de chiffres périmés
        ['diskused', 'diskfree', 'disktotal'].forEach(function (k) { texte(m + k, ''); });
        ['diskread', 'diskwrite'].forEach(function (k) { texte(m + k, '—'); });
        var b = document.getElementById(m + 'diskprogress');
        if (b) { b.style.width = '0%'; b.setAttribute('aria-valuenow', 0); }
        etatDisque(m, true);
        // Les pastilles disent le remplissage : un disque illisible n'a pas de couleur mais le symbole ⊘ (gris barré).
        var p = document.getElementById(m + 'indicator');
        if (p) { p.classList.remove('bg-success', 'bg-warning', 'bg-danger', 'bg-secondary'); p.classList.add('disk-unavailable'); p.title = m + ' : unavailable'; }
        return;
      }
      etatDisque(m, false);
      texte(m + 'diskused', x.diskused);
      texte(m + 'diskfree', x.diskfree);
      texte(m + 'disktotal', x.disktotal);
      texte(m + 'diskpercent', x.perutil);
      texte(m + 'diskread', x.read);
      texte(m + 'diskwrite', x.write);
      barre(document.getElementById(m + 'diskprogress'), x.perutil, couleurDisque(x.alert));
      var ind = document.getElementById(m + 'indicator');
      couleur(ind, Number(x.perutil), couleurDisque(x.alert));
      if (ind) { ind.title = m + ' : ' + x.perutil + ' %'; }  // au survol : le remplissage en clair
    });
  }

  // 2026-10-20 : bandeau « disque bientôt plein » en haut du tableau de bord, un encart par disque de la carte
  // Disk Info qui a atteint un seuil (avertissement en orange, critique en rouge). Il suit chaque relevé :
  // il apparaît et disparaît en direct. Le chemin de montage vient du système (espaces, caractères spéciaux) :
  // il n'est jamais inséré comme du HTML, seulement par textContent.
  // Lecteurs d'écran : pendant un téléchargement, l'espace libre d'une ligne change presque chaque seconde. Les
  // lignes visibles ne sont donc pas une zone « live ». Seul le résumé caché #alerte-disque-resume (role="status"
  // aria-live="polite", templates/index.html) est lu : il ne nomme que les disques en alerte et leur niveau, il ne
  // change que quand un disque entre en alerte, en sort ou change de niveau. Rien n'est touché sans changement.
  function alertes(d) {
    var bandeau = document.getElementById('alerte-disque');
    var resume = document.getElementById('alerte-disque-resume');
    var carte = document.getElementById('carte-disque');
    if (!bandeau || !carte) { return; }  // pas de carte Disk Info pour cet utilisateur : pas de bandeau non plus
    var lignes = [];
    Object.keys(d).forEach(function (m) {
      var x = d[m];
      if (x.unavailable || (x.alert !== 'warning' && x.alert !== 'critical')) { return; }
      if (!carte.contains(document.getElementById(m + 'diskused'))) { return; }  // disque absent de la carte
      var pct = Math.trunc(Number(x.perutil) || 0);  // tronqué : « 90% » n'apparaît jamais sous le seuil de 90
      lignes.push({ m: m, alerte: x.alert, pct: pct,
                    texte: 'Disk almost full: ' + m + ' is ' + pct + '% used (' + x.diskfree + ' free)' });
    });
    lignes.sort(function (a, b) { return b.pct - a.pct; });  // le disque le plus plein en premier
    lignes.forEach(function (l, i) {
      // text-break : un chemin long sans espace passe à la ligne au lieu de déborder sur un téléphone.
      var classe = 'alert mt-3 mb-0 text-break ' + (l.alerte === 'critical' ? 'alert-danger' : 'alert-warning');
      var encart = bandeau.children[i];
      if (!encart) {
        encart = document.createElement('div');
        encart.className = classe;
        encart.textContent = l.texte;
        bandeau.appendChild(encart);
        return;
      }
      if (encart.className !== classe) { encart.className = classe; }
      if (encart.textContent !== l.texte) { encart.textContent = l.texte; }
    });
    while (bandeau.children.length > lignes.length) {  // disques revenus sous le seuil, démontés ou illisibles
      bandeau.removeChild(bandeau.lastElementChild);
    }
    if (resume) {
      var phrase = resumer(lignes, resume.textContent);
      if (resume.textContent !== phrase) { resume.textContent = phrase; }
    }
  }

  // Résumé pour les lecteurs d'écran : les disques rouges puis orange, chacun dans l'ordre des chemins (l'ordre
  // du remplissage changerait sans qu'aucun niveau ne change). Ni pourcentage ni espace libre. Vide tant
  // qu'aucun disque n'a été en alerte ; quand le dernier en sort, il le dit.
  function resumer(lignes, avant) {
    var niveaux = { critical: [], warning: [] };
    lignes.forEach(function (l) { niveaux[l.alerte].push(l.m); });
    var parties = [];
    if (niveaux.critical.length) { parties.push('critical: ' + niveaux.critical.sort().join(', ')); }
    if (niveaux.warning.length) { parties.push('warning: ' + niveaux.warning.sort().join(', ')); }
    if (parties.length) { return 'Disk almost full, ' + parties.join('; '); }
    return avant ? 'No disk is almost full anymore.' : '';
  }

  document.addEventListener('DOMContentLoaded', function () {
    var bloc = document.querySelector('.live-stack');
    if (!bloc || typeof io === 'undefined') { return; }
    var base = bloc.getAttribute('data-urlbase') || '';
    var socket = io.connect(window.location.protocol + '//' + document.domain + ':' + window.location.port + '/websocket',
                            { path: base + '/socket.io' });
    socket.on('live', function (d) {
      if (d.cpu) { cpu(d.cpu); }
      if (d.ram) { ram(d.ram); }
      if (d.disks) { disques(d.disks); alertes(d.disks); }
    });
  });
})();
