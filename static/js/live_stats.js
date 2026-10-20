/* 2026-10-09 : met à jour chaque seconde les cartes CPU Info, RAM Info et Disk Info (événement « live » du websocket).
   Mêmes identifiants et mêmes seuils (75 % / 90 %) que les scripts d'origine du panel (templates/macros.html).
   2026-10-20 : bandeau « disque bientôt plein » ; un disque en alerte prend la couleur de l'alerte.
   Voir le README. */
(function () {
  'use strict';

  function niveau(pct) { return pct > 90 ? 'bg-danger' : (pct > 75 ? 'bg-warning' : 'bg-success'); }

  // classe : couleur imposée (alerte disque), sinon celle des seuils 75 % / 90 %.
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

  // Niveau d'alerte « disque bientôt plein » calculé par le serveur (seuils de swizzin.cfg) → couleur Bootstrap.
  function classeAlerte(alerte) {
    return alerte === 'critical' ? 'bg-danger' : (alerte === 'warning' ? 'bg-warning' : null);
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
      // Disque en alerte : la barre et la pastille prennent la couleur du bandeau (orange ou rouge).
      barre(document.getElementById(m + 'diskprogress'), x.perutil, classeAlerte(x.alert));
      var ind = document.getElementById(m + 'indicator');
      couleur(ind, Number(x.perutil), classeAlerte(x.alert));
      if (ind) { ind.title = m + ' : ' + x.perutil + ' %'; }  // au survol : le remplissage en clair
    });
  }

  // 2026-10-20 : bandeau « disque bientôt plein » en haut du tableau de bord, un encart par disque de la carte
  // Disk Info qui a atteint un seuil (avertissement en orange, critique en rouge). Il suit chaque relevé :
  // il apparaît et disparaît en direct. Le chemin de montage vient du système (espaces, caractères spéciaux) :
  // il n'est jamais inséré comme du HTML, seulement par textContent.
  var dernierBandeau = null;

  function alertes(d) {
    var bandeau = document.getElementById('alerte-disque');
    var carte = document.getElementById('carte-disque');
    if (!bandeau || !carte) { return; }  // pas de carte Disk Info pour cet utilisateur : pas de bandeau non plus
    var lignes = [];
    Object.keys(d).forEach(function (m) {
      var x = d[m];
      if (x.unavailable || !classeAlerte(x.alert)) { return; }
      if (!carte.contains(document.getElementById(m + 'diskused'))) { return; }  // disque absent de la carte
      var pct = Math.trunc(Number(x.perutil) || 0);  // tronqué : « 90% » n'apparaît jamais sous le seuil de 90
      lignes.push({ alerte: x.alert, pct: pct,
                    texte: 'Disk almost full: ' + m + ' is ' + pct + '% used (' + x.diskfree + ' free)' });
    });
    lignes.sort(function (a, b) { return b.pct - a.pct; });  // le disque le plus plein en premier
    var cle = JSON.stringify(lignes);
    if (cle === dernierBandeau) { return; }  // rien de changé depuis le relevé précédent
    dernierBandeau = cle;
    bandeau.textContent = '';
    lignes.forEach(function (l) {
      var encart = document.createElement('div');
      // text-break : un chemin long sans espace passe à la ligne au lieu de déborder sur un téléphone.
      encart.className = 'alert mb-2 text-break ' + (l.alerte === 'critical' ? 'alert-danger' : 'alert-warning');
      encart.textContent = l.texte;
      bandeau.appendChild(encart);
    });
    bandeau.hidden = lignes.length === 0;
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
