/* 2026-10-09 : met à jour chaque seconde les cartes CPU Info, RAM Info et Disk Info (événement « live » du websocket).
   Mêmes identifiants et mêmes seuils (75 % / 90 %) que les scripts d'origine du panel (templates/macros.html).
   Voir le README. */
(function () {
  'use strict';

  function niveau(pct) { return pct > 90 ? 'bg-danger' : (pct > 75 ? 'bg-warning' : 'bg-success'); }

  function couleur(el, pct) {
    if (!el) { return; }
    el.classList.remove('bg-success', 'bg-warning', 'bg-danger', 'bg-secondary');
    el.classList.add(niveau(pct));
  }

  function barre(el, pct) {
    if (!el) { return; }
    var p = Math.max(0, Math.min(100, Number(pct) || 0));
    el.style.width = p + '%';
    el.setAttribute('aria-valuenow', Math.trunc(p));
    couleur(el, p);
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
  }

  function disques(d) {
    Object.keys(d).forEach(function (m) {
      var x = d[m];
      texte(m + 'diskused', x.diskused);
      texte(m + 'diskfree', x.diskfree);
      texte(m + 'disktotal', x.disktotal);
      texte(m + 'diskpercent', x.perutil);
      texte(m + 'diskread', x.read);
      texte(m + 'diskwrite', x.write);
      barre(document.getElementById(m + 'diskprogress'), x.perutil);
      couleur(document.getElementById(m + 'indicator'), Number(x.perutil));
    });
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
      if (d.disks) { disques(d.disks); }
    });
  });
})();
