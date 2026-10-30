/* Three pipelines, animated. Plain IIFE, no dependencies.
   Draws into #pipes (viewBox 0 0 1000 1262) and runs one particle model per panel.
   Model: every panel keeps a fixed pool of dots that are advanced along x in fixed
   1/60 s steps (frames interpolate between steps, so the result is the same on every
   display and the screenshots are reproducible). Upstream of a throat the dots pack
   into a creeping crowd; a release at the throat mouth lets one dot through every
   T seconds, so the stretch after it is a steady single file. Panel 3 has no wall
   throat: kept dots converge inside the dashed envelope while holding an even
   spacing, the rest coast outward to a halt as hollow rings, linger, fade and
   re-enter at the left. With prefers-reduced-motion the figure is drawn once as the
   static blue-noise fill of the sketch. */
(function () {
  'use strict';
  var svg = document.getElementById('pipes');
  if (!svg) return;

  var NS = 'http://www.w3.org/2000/svg';
  var X0 = 60, X1 = 940, H = 100, R = 10, CY = 190;
  var WALL = R + 4;            // a dot centre keeps this far from the wall
  var SEP = 2 * R + 4;         // hard minimum centre distance
  var STEP = 1 / 60;           // fixed model step
  var ENV_END = 560;           // the dashed envelope stops at the Harden boundary
  var ACC = 70;                // u/s^2: how quickly a dot regains speed when the way opens
  var G3 = 46;                 // spacing the kept dots of panel 3 hold between each other
  var XIN = X0 - 14;           // dots enter here, still clipped by the pipe mouth

  /* ---- colours: read from :root, Night values as fallbacks ---- */
  function cssVar(name, fb) {
    try { var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim(); return v || fb; }
    catch (e) { return fb; }
  }
  var INK = cssVar('--ink', '#e2ddd4'), INK2 = cssVar('--ink-2', '#a39d92'),
      LINE2 = cssVar('--line-2', '#3f3931'), BRASS = cssVar('--brass', '#c69d64');
  var SANS = '"Hanken Grotesk", system-ui, -apple-system, sans-serif';
  var MONO = '"JetBrains Mono", ui-monospace, Menlo, monospace';

  /* ---- helpers ---- */
  function el(name, attrs, parent) {
    var e = document.createElementNS(NS, name);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    (parent || svg).appendChild(e);
    return e;
  }
  function ss(t) { t = t < 0 ? 0 : t > 1 ? 1 : t; return t * t * (3 - 2 * t); }
  function clamp(v, a, b) { return v < a ? a : v > b ? b : v; }
  function rng(seed) {
    return function () {
      seed |= 0; seed = seed + 0x6D2B79F5 | 0;
      var t = Math.imul(seed ^ seed >>> 15, 1 | seed);
      t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };
  }
  /* pipe half-height with a throat between a and b */
  function taper(a, b, neck) {
    var ramp = (b - a) * 0.5, mid = (a + b) / 2;
    return function (x) {
      if (x <= a || x >= b) return H;
      if (x < mid) return H - (H - neck) * ss((x - a) / ramp);
      return H - (H - neck) * ss((b - x) / ramp);
    };
  }
  function flat() { return H; }
  /* panel 3: the envelope of what is kept, a boundary of choice rather than a wall */
  function env(x) { return Math.max(14, H * (1 - 0.86 * ss((x - 84) / 400))); }
  function range(a, b, step) { var out = []; for (var x = a; x <= b; x += step) out.push(x); return out; }

  /* Blue-noise fill (Bridson Poisson disc), as the sketch: even density, no rows, no clusters. */
  function poisson(half, x0, x1, seed) {
    var r = 27, m = 4, rand = rng(seed), k = 30;
    var inside = function (x, y) { return x >= x0 && x <= x1 && Math.abs(y - CY) <= half(x) - R - m; };
    var cell = r / Math.SQRT2, gx0 = x0 - r, gy0 = CY - H - r,
        gw = Math.ceil((x1 - x0 + 2 * r) / cell), gh = Math.ceil((2 * H + 2 * r) / cell);
    var grid = new Array(gw * gh), pts = [], active = [], i;
    for (i = 0; i < grid.length; i++) grid[i] = -1;
    var ok = function (x, y) {
      var cx = Math.floor((x - gx0) / cell), cy = Math.floor((y - gy0) / cell);
      for (var a = Math.max(0, cx - 2); a <= Math.min(gw - 1, cx + 2); a++)
        for (var b = Math.max(0, cy - 2); b <= Math.min(gh - 1, cy + 2); b++) {
          var q = grid[a + b * gw];
          if (q >= 0 && Math.hypot(pts[q][0] - x, pts[q][1] - y) < r) return false;
        }
      return true;
    };
    var add = function (x, y) {
      pts.push([x, y]); active.push(pts.length - 1);
      grid[Math.floor((x - gx0) / cell) + Math.floor((y - gy0) / cell) * gw] = pts.length - 1;
    };
    add(x0 + 2 + rand() * 4, CY + (rand() - 0.5) * 20);
    while (active.length) {
      var ai = Math.floor(rand() * active.length), p = pts[active[ai]], found = false;
      for (var t = 0; t < k; t++) {
        var ang = rand() * Math.PI * 2, d = r * (1 + rand());
        var x = p[0] + Math.cos(ang) * d, y = p[1] + Math.sin(ang) * d;
        if (inside(x, y) && ok(x, y)) { add(x, y); found = true; break; }
      }
      if (!found) active.splice(ai, 1);
    }
    return pts;
  }

  /* ---- panel definitions ---- */
  var panels = [
    { kind: 1, y: 0, title: 'BEFORE', half: taper(280, 500, 16),
      stages: [['Ideation / Design', 'Design', 60, 280], ['Implementation', 'Build', 280, 500, true],
               ['Review', 'Review', 500, 720], ['Ship', 'Ship', 720, 940]],
      sim: { gate: 388, T: 2.0, vFree: 50, vLine: 55, crowd: [82, 392], seed: 11,
             line: [470, 580, 680, 790, 890], extra: 2, gap: [0.2, 0.9], rad: 30 } },
    { kind: 2, y: 410, title: 'NOW, AS USUALLY DRAWN', half: taper(500, 720, 34),
      stages: [['Ideation / Design', 'Design', 60, 280], ['Implementation', 'Build', 280, 500],
               ['Review', 'Review', 500, 720, true], ['Ship', 'Ship', 720, 940]],
      sim: { gate: 598, T: 0.84, vFree: 50, vLine: 55, crowd: [302, 580], seed: 23,
             scatter: [[100, -45], [160, 55], [205, -72], [245, 10], [125, 68], [260, -30], [185, -12]],
             line: range(600, 922, 46), extra: 3, gap: [0.25, 0.9], rad: 27 } },
    { kind: 3, y: 820, title: 'NOW, WITH COLLIMATE', half: flat,
      stages: [['Collimate', 'Collimate', 60, 560, true], ['Harden', 'Harden', 560, 700],
               ['Review', 'Review', 700, 840], ['Ship', 'Ship', 840, 940]],
      sim: { vFree: 50, vLine: 55, keptEvery: 0.88, crowd: [82, 538], seed: 37,
             line: range(578, 900, 46), extra: 18, gap: [0.16, 0.34], rad: 27 } }
  ];

  /* ---- static drawing: titles, dividers, walls, labels ---- */
  var figStyle = el('style', {});
  figStyle.textContent = '#pipes text{font-family:' + SANS + '}#pipes .pipes-title{font-family:' + MONO + '}';
  function fs(expr) { return 'font-size:' + expr; }
  var SZ = {
    title: 'calc(15px * var(--k, 1))',
    name: 'min(calc(24px * var(--k, 1)), 28px)',
    nameShort: 'min(calc(20px * var(--k, 1)), 34px)',
    sub: 'min(calc(13.5px * var(--k, 1)), 26px)',
    legend: 'min(calc(15px * var(--k, 1)), 28px)'
  };

  function buildPanel(p, i) {
    var g = el('g', { transform: 'translate(0,' + p.y + ')' });
    p.g = g;
    var title = el('text', { x: X0, y: 40, fill: INK2, 'class': 'pipes-title', 'font-weight': 500,
      style: fs(SZ.title) + ';letter-spacing:calc(4px * var(--k, 1))' }, g);
    title.textContent = p.title;

    p.stages.slice(1).forEach(function (s) {
      el('line', { x1: s[2], x2: s[2], y1: CY - H - 45, y2: CY + H + 105, stroke: LINE2,
        'stroke-dasharray': '5 6', 'stroke-width': 1.5 }, g);
    });

    var top = '', bot = '';
    for (var x = X0; x <= X1; x += 4) {
      var h = p.half(x);
      top += (x === X0 ? 'M' : 'L') + x + ',' + (CY - h) + ' ';
      bot += (x === X0 ? 'M' : 'L') + x + ',' + (CY + h) + ' ';
    }
    var wall = { fill: 'none', stroke: INK2, 'stroke-width': 2.5, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' };
    el('path', Object.assign({ d: top }, wall), g);
    el('path', Object.assign({ d: bot }, wall), g);

    if (p.kind === 3) {
      var et = '', eb = '';
      for (var ex = X0; ex <= ENV_END; ex += 4) {
        var eh = Math.min(H, env(ex) + R + 4);
        et += (ex === X0 ? 'M' : 'L') + ex + ',' + (CY - eh) + ' ';
        eb += (ex === X0 ? 'M' : 'L') + ex + ',' + (CY + eh) + ' ';
      }
      var dash = { fill: 'none', stroke: INK2, 'stroke-width': 2.5, 'stroke-dasharray': '8 8', 'stroke-linecap': 'round', opacity: 0.85 };
      el('path', Object.assign({ d: et }, dash), g);
      el('path', Object.assign({ d: eb }, dash), g);
    }

    /* dots live in a clipped group so they can slide in and out of the pipe ends */
    var clip = el('clipPath', { id: 'pipes-clip-' + i }, g);
    el('rect', { x: X0, y: CY - H - 4, width: X1 - X0, height: 2 * H + 8 }, clip);
    p.dotsG = el('g', { 'clip-path': 'url(#pipes-clip-' + i + ')' }, g);

    p.stageEls = p.stages.map(function (s) {
      var t = el('text', { x: (s[2] + s[3]) / 2, y: CY + H + 78, 'text-anchor': 'middle',
        'font-weight': s[4] ? 600 : 500, fill: s[4] ? BRASS : INK, style: fs(SZ.name) }, g);
      t.textContent = s[0];
      return t;
    });
    if (p.kind === 3) {
      var sx = (p.stages[0][2] + p.stages[0][3]) / 2;
      p.legend = {
        dot: el('circle', { cx: 197, cy: 387, r: 7, fill: BRASS }, g),
        kept: el('text', { x: 212, y: 392, fill: INK2, style: fs(SZ.legend) }, g),
        ring: el('circle', { cx: 275, cy: 387, r: 6.5, fill: 'none', stroke: BRASS, 'stroke-width': 2, opacity: 0.5 }, g),
        dropped: el('text', { x: 290, y: 392, fill: INK2, style: fs(SZ.legend) }, g)
      };
      p.legend.kept.textContent = 'kept';
      p.legend.dropped.textContent = 'tried, then dropped';
      p.sub1 = el('text', { x: sx, y: 414, 'text-anchor': 'middle', fill: INK2, style: fs(SZ.sub) }, g);
      p.sub1.textContent = 'design + implementation merged';
      p.sub2 = el('text', { x: sx, y: 434, 'text-anchor': 'middle', fill: INK2, style: fs(SZ.sub) }, g);
      p.sub2.textContent = 'prototypes, tests, surveys, bake-offs';
    }
  }
  panels.forEach(buildPanel);

  /* ---- responsive text: --k keeps labels legible when the svg is narrow ---- */
  var compact = null;
  function layout(k) {
    var c = k >= 1.5;
    if (c === compact) return;
    compact = c;
    panels.forEach(function (p) {
      p.stageEls.forEach(function (t, i) {
        t.setAttribute('y', c ? 352 : 368);
        t.setAttribute('style', fs(c ? SZ.nameShort : SZ.name));
        t.textContent = c ? p.stages[i][1] : p.stages[i][0];
      });
      if (p.kind === 3) {
        /* wide: the legend sits under Collimate as in the sketch; narrow: two rows under Harden */
        var L = p.legend, y1 = c ? 386 : 392, y2 = c ? 416 : 392;
        L.dot.setAttribute('cx', c ? 600 : 197); L.dot.setAttribute('cy', y1 - (c ? 9 : 5)); L.dot.setAttribute('r', c ? 11 : 7);
        L.kept.setAttribute('x', c ? 620 : 212); L.kept.setAttribute('y', y1);
        L.ring.setAttribute('cx', c ? 600 : 275); L.ring.setAttribute('cy', y2 - (c ? 9 : 5)); L.ring.setAttribute('r', c ? 10 : 6.5);
        L.dropped.setAttribute('x', c ? 620 : 290); L.dropped.setAttribute('y', y2);
        p.sub1.setAttribute('y', c ? 406 : 414);
        p.sub2.setAttribute('y', c ? 433 : 434);
      }
    });
  }
  function fit() {
    var w = svg.getBoundingClientRect().width || 1000;
    var k = clamp(720 / w, 1, 1.9);
    svg.style.setProperty('--k', k.toFixed(3));
    layout(k);
  }
  fit();
  if (typeof ResizeObserver === 'function') new ResizeObserver(fit).observe(svg);
  else window.addEventListener('resize', fit);

  /* ---- static figure for reduced motion ---- */
  var motionQuery = null;
  try { motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)'); } catch (e) { motionQuery = null; }
  var reduced = !!(motionQuery && motionQuery.matches);
  function staticDot(g, x, y, hollow) {
    if (hollow) el('circle', { cx: x, cy: y, r: R - 1, fill: 'none', stroke: BRASS, 'stroke-width': 2, opacity: 0.38 }, g);
    else el('circle', { cx: x, cy: y, r: R, fill: BRASS }, g);
  }
  function drawStatic() {
    panels.forEach(function (p) {
      var g = p.dotsG, s = p.sim;
      while (g.firstChild) g.removeChild(g.firstChild);
      if (s.scatter) s.scatter.forEach(function (q) { staticDot(g, q[0], CY + q[1], false); });
      poisson(p.half, s.crowd[0], s.crowd[1], s.seed).forEach(function (q) {
        staticDot(g, q[0], q[1], p.kind === 3 && Math.abs(q[1] - CY) > env(q[0]));
      });
      s.line.forEach(function (x) { staticDot(g, x, CY, false); });
    });
  }
  if (reduced) { drawStatic(); return; }

  /* ---- particles ----
     states: WAIT parked off-screen, FREE moving in the open (or queued), LINE released
     into the single file, COAST dropped and rolling outward to a stop, RING stopped
     and hollow, FADE fading out before it re-enters. */
  var WAIT = 0, FREE = 1, LINE = 2, COAST = 3, RING = 4, FADE = 5;
  var CELL = 32;
  var GX0 = X0 - 48, GY0 = CY - H - 16, GW = Math.ceil((X1 - X0 + 96) / CELL), GH = Math.ceil((2 * H + 32) / CELL);

  function makeSim(p) {
    var s = p.sim, explore = p.kind === 3, rand = rng(s.seed * 7 + 3), g = p.dotsG;
    var VLO = explore ? 1.04 : 0.88, VHI = explore ? 1.2 : 1.12;   // spread of free speeds
    var dots = [], waiting = 0, now = 0, lastRel = 0, nextSpawn = 0, keptNext = 0, tipSlot = 1e9;
    var TIP = 470;   // panel 3: where a kept dot takes its place in the line
    var head = new Int16Array(GW * GH), next;
    head.fill(-1);

    /* soft radius: in the queued panels a two-size mix (a glass former), so the crowd packs
       irregularly like the sketch's blue noise instead of settling into a hex lattice */
    function pickRad() { return explore ? s.rad + rand() * 4 : s.rad - 1 + rand() * 2 + (rand() < 0.4 ? 8 : 0); }
    function newDot(x, y, state) {
      var d = {
        x: x, y: y, ox: x, oy: y, state: state, spd: 0, vi: s.vFree, rad: 0, wg: rand() * 9, w: 1,
        ph: rand() * 6.283, sl: rand() * 6.283, om: 1.2 + rand() * 1.6, kept: false, lane: 0, drift: 0, dropX: 0,
        blk: 0, cst: 0, cd: 1, ca: 0, t: 0, lg: 0, f: 0, fa: 1, fr: 1, slot: 0, lx: '', ly: '', el: null
      };
      d.rad = pickRad();
      d.vi = s.vFree * (VLO + rand() * (VHI - VLO));
      d.el = el('circle', { cx: -200, cy: CY, r: R, fill: BRASS, 'fill-opacity': 1, stroke: BRASS, 'stroke-width': 2, 'stroke-opacity': 0 }, g);
      dots.push(d);
      return d;
    }
    function setKept(d, kept) {
      d.kept = kept;
      if (kept) {
        d.lane = clamp((d.y - CY) / Math.max(1, (env(Math.max(d.x, X0)) - 14) * 0.92), -1, 1);
        d.drift = 0; d.vi = s.vFree * 1.08; d.w = 0.3; d.slot = NaN;
      } else {
        d.w = 1;
        d.drift = (d.y >= CY ? 1 : -1) * (2 + rand() * 5);
        d.dropX = Math.max(d.x + 20, 260 + rand() * 220);
      }
    }
    /* f: how far along the fill-to-ring blend a dot is (0 solid, 1 hollow ring) */
    function look(d, f) {
      if (f === d.f) return;
      d.f = f;
      d.el.setAttribute('fill-opacity', (1 - f).toFixed(3));
      d.el.setAttribute('stroke-opacity', (0.38 * f).toFixed(3));
      d.el.setAttribute('r', (R - f).toFixed(2));
    }
    function toRing(d) {
      var lim = H - WALL;
      if (d.y > CY + lim) d.y = CY + lim; else if (d.y < CY - lim) d.y = CY - lim;
      d.state = RING; d.spd = 0; d.w = 0; d.t = now; d.blk = 0; d.lg = 5.5 + rand() * 2.5;
      look(d, 1);
    }
    function toFade(d) { d.state = FADE; d.w = 0; d.t = now; d.fa = 1; d.fr = 1 / 1.2; }
    function toWait(d) {
      d.state = WAIT; d.w = 1; waiting++;
      d.x = d.ox = -200; d.y = d.oy = CY; d.spd = 0;
      d.el.setAttribute('cx', '-200'); d.lx = '-200';
      d.el.setAttribute('opacity', '1');
      look(d, 0);
    }
    function toLine(d) { d.state = LINE; d.w = 0; }

    /* initial state: the sketch's own fill, so the figure reads right from the first frame */
    if (s.scatter) s.scatter.forEach(function (q) { var d = newDot(q[0], CY + q[1], FREE); d.spd = d.vi; });
    var seeded = poisson(p.half, s.crowd[0], s.crowd[1], s.seed);
    if (explore) {
      var inside = [];
      seeded.forEach(function (q) {
        if (Math.abs(q[1] - CY) > env(q[0])) {
          var d = newDot(q[0], q[1], RING); d.w = 0; d.t = -rand() * 2; d.lg = 3 + rand() * 7; look(d, 1);
          if (q[0] > 430 && Math.abs(q[1] - CY) < 36) d.lg = rand() * 1.5;   // not in the line's lane for long
        } else inside.push(q);
      });
      /* the kept seeds sit G3 apart in x, so the line stays continuous from the first frame */
      var keep = [], target, k, best, bi, j;
      for (k = 1; k < 12; k++) {
        target = s.line[0] - G3 * k; best = 24; bi = -1;
        for (j = 0; j < inside.length; j++) {
          if (keep.indexOf(j) >= 0) continue;
          var dist = Math.abs(inside[j][0] - target);
          if (dist < best) { best = dist; bi = j; }
        }
        if (bi >= 0) keep.push(bi);
      }
      inside.forEach(function (q, idx) {
        var d = newDot(q[0], q[1], FREE); d.spd = d.vi;
        setKept(d, keep.indexOf(idx) >= 0);
      });
    } else {
      seeded.forEach(function (q) { var d = newDot(q[0], q[1], FREE); d.spd = d.vi * 0.3; });
    }
    s.line.forEach(function (x) { var d = newDot(x, CY, LINE); d.spd = s.vLine; d.w = 0; d.kept = true; d.vi = s.vLine; d.slot = x; });
    if (explore) {
      tipSlot = s.line[0];
      dots.filter(function (d) { return d.kept && d.state === FREE && d.x >= TIP; })
        .sort(function (a, b) { return b.x - a.x; }).forEach(takeSlot);
    }
    if (!explore) lastRel = -(s.line[0] - s.gate) / s.vLine;
    for (var i = 0; i < s.extra; i++) { newDot(-200, CY, WAIT); waiting++; }
    next = new Int16Array(dots.length);

    /* a faded ring stops being an obstacle: nothing swerves around what can no longer be seen */
    function present(d) { return d.state !== WAIT && !(d.state === FADE && d.fa < 0.4); }

    function buildGrid() {
      head.fill(-1);
      for (var i = 0; i < dots.length; i++) {
        var d = dots[i]; if (!present(d)) continue;
        var cx = Math.floor((d.x - GX0) / CELL), cy = Math.floor((d.y - GY0) / CELL);
        if (cx < 0 || cy < 0 || cx >= GW || cy >= GH) continue;
        var c = cx + cy * GW; next[i] = head[c]; head[c] = i;
      }
    }
    function held(a, b) {
      /* a dot held at the throat mouth does not yield to the queue behind it */
      return !explore && a.state === FREE && a.x >= s.gate - 0.01 && b.x < a.x;
    }
    function separate() {
      for (var i = 0; i < dots.length; i++) {
        var a = dots[i]; if (!present(a)) continue;
        var cx = Math.floor((a.x - GX0) / CELL), cy = Math.floor((a.y - GY0) / CELL);
        for (var gx = cx - 1; gx <= cx + 1; gx++) {
          if (gx < 0 || gx >= GW) continue;
          for (var gy = cy - 1; gy <= cy + 1; gy++) {
            if (gy < 0 || gy >= GH) continue;
            for (var j = head[gx + gy * GW]; j >= 0; j = next[j]) {
              if (j <= i) continue;
              var b = dots[j];
              var dx = b.x - a.x, dy = b.y - a.y, need = (a.rad + b.rad) * 0.5;
              var d2 = dx * dx + dy * dy;
              if (d2 >= need * need) continue;
              var wa = held(a, b) ? 0 : a.w, wb = held(b, a) ? 0 : b.w;
              var ws = wa + wb; if (ws <= 0) continue;
              var dist = Math.sqrt(d2), nx, ny;
              if (dist < 0.01) { nx = (a.x < b.x || (a.x === b.x && i < j)) ? 1 : -1; ny = 0; dist = 0.01; }
              else { nx = dx / dist; ny = dy / dist; }
              /* in the throat, mostly vertical pairs get a horizontal lean so the narrow place
                 resolves along the pipe; in the open it would line the bonds up into rows */
              if (Math.abs(nx) < 0.45 && p.half(a.x) < 0.7 * H) {
                var sx = nx >= 0 ? 1 : -1; nx = nx + sx * 0.5;
                var nl = Math.sqrt(nx * nx + ny * ny); nx /= nl; ny /= nl;
              }
              var push = need - dist, fa = wa / ws, fb = wb / ws;
              a.x -= nx * push * fa; a.y -= ny * push * fa;
              b.x += nx * push * fb; b.y += ny * push * fb;
            }
          }
        }
      }
    }
    /* last pass: resolve what is left along the pipe only, so no wall is crossed */
    function separateX() {
      for (var i = 0; i < dots.length; i++) {
        var a = dots[i]; if (!present(a)) continue;
        var cx = Math.floor((a.x - GX0) / CELL), cy = Math.floor((a.y - GY0) / CELL);
        for (var gx = cx - 1; gx <= cx + 1; gx++) {
          if (gx < 0 || gx >= GW) continue;
          for (var gy = cy - 1; gy <= cy + 1; gy++) {
            if (gy < 0 || gy >= GH) continue;
            for (var j = head[gx + gy * GW]; j >= 0; j = next[j]) {
              if (j <= i) continue;
              var b = dots[j], dx = b.x - a.x, dy = b.y - a.y, need = SEP + 0.5;
              if (dx * dx + dy * dy >= need * need || Math.abs(dy) >= need) continue;
              var wa = held(a, b) ? 0 : a.w, wb = held(b, a) ? 0 : b.w;
              var ws = wa + wb; if (ws <= 0) continue;
              var want = Math.sqrt(need * need - dy * dy), push = want - Math.abs(dx);
              var sx = dx >= 0 ? 1 : -1;
              a.x -= sx * push * wa / ws; b.x += sx * push * wb / ws;
            }
          }
        }
      }
    }
    function walls() {
      for (var i = 0; i < dots.length; i++) {
        var d = dots[i]; if (d.state === WAIT || d.w === 0) continue;
        if (!explore && d.state === FREE && d.x > s.gate) d.x = s.gate;
        var h = p.half(clamp(d.x, X0, X1)), lim = h - WALL;
        /* in the open each free dot keeps its own gap from the wall (no flat rows along it);
           where the walls converge the gap fades out, so the clamp never shoves dots together */
        if (d.state === FREE) lim -= d.wg * clamp((h - 60) / 40, 0, 1);
        if (explore && d.kept && d.state === FREE && d.x > 200) lim = Math.min(lim, Math.max(0, env(d.x) - 2));
        if (d.y > CY + lim) d.y = CY + lim; else if (d.y < CY - lim) d.y = CY - lim;
      }
    }
    /* moving dots part around the rings ahead of them instead of pushing against them */
    function steer(d, dt) {
      var cx = Math.floor((d.x - GX0) / CELL), cy = Math.floor((d.y - GY0) / CELL), push = 0;
      for (var gx = cx; gx <= cx + 2; gx++) {
        if (gx < 0 || gx >= GW) continue;
        for (var gy = cy - 1; gy <= cy + 1; gy++) {
          if (gy < 0 || gy >= GH) continue;
          for (var j = head[gx + gy * GW]; j >= 0; j = next[j]) {
            var o = dots[j];
            if (o === d || !(o.state === RING || o.state === FADE || (o.state === COAST && o.spd < 8))) continue;
            var dx = o.x - d.x, dy = o.y - d.y;
            if (dx <= -6 || dx > 48 || dy > 30 || dy < -30) continue;
            var sg = dy > 0.5 ? -1 : dy < -0.5 ? 1 : (d.y > CY ? -1 : 1);
            /* at the wall the only way round is inward */
            if (sg * (d.y - CY) > 0 && Math.abs(d.y - CY) > H - WALL - 14) sg = -sg;
            push += sg * (1 - Math.max(0, dx) / 48);
          }
        }
      }
      if (push) d.y += clamp(push, -1, 1) * 34 * dt;
    }
    /* has a moving dot come up against this (fading) one */
    function reached(d) {
      for (var i = 0; i < dots.length; i++) {
        var o = dots[i];
        if (o === d || !(o.state === FREE || o.state === LINE || (o.state === COAST && o.spd >= 8))) continue;
        var dx = o.x - d.x, dy = o.y - d.y;
        if (dx * dx + dy * dy < (SEP + 3) * (SEP + 3)) return true;
      }
      return false;
    }
    function roomAt(x, y, r) {
      for (var i = 0; i < dots.length; i++) {
        var d = dots[i]; if (d.state === WAIT) continue;
        var dx = d.x - x, dy = d.y - y;
        if (dx * dx + dy * dy < r * r) return false;
      }
      return true;
    }
    /* panel 3: at the band's tip a kept dot takes the next place in the line, a slot that
       moves on at line speed, G3 behind the one handed out before it. One that arrives early
       eases off while it converges; one that arrives late simply takes its place. */
    function takeSlot(d) {
      d.slot = Math.min(tipSlot - G3, d.x - s.vLine * now);
      tipSlot = d.slot;
    }
    function slotSpeed(d, v, over) {
      return clamp(v + 2 * (d.slot + v * now - d.x), 0.5 * v, over * v);
    }

    function step(dt) {
      now += dt;
      var i, d;
      for (i = 0; i < dots.length; i++) {
        d = dots[i];
        d.ox = d.x; d.oy = d.y;
        switch (d.state) {
          case FREE:
            if (explore && d.kept && d.slot === d.slot) d.spd = Math.min(slotSpeed(d, s.vLine, 1.1), d.spd + ACC * dt);
            else d.spd = Math.min(d.vi, d.spd + ACC * dt);
            d.x += d.spd * dt;
            d.y += (explore ? 1.2 : 3) * d.om * Math.cos(d.om * now + d.ph) * dt;
            if (!explore) {
              /* queued dots jostle, and each leans slowly one way then the other (a 16 s cycle
                 of its own), so the crowd keeps shearing and never settles into a lattice */
              if (d.blk > 0) d.y += (rand() - 0.5) * 18 * dt;
              d.y += 4 * Math.cos(0.4 * now + d.sl) * dt;
            }
            if (explore) {
              steer(d, dt);
              if (d.kept) {
                var ty = CY + d.lane * Math.max(0, env(d.x) - 14) * 0.92;
                d.y += (ty - d.y) * (1 - Math.exp(-dt / 0.8));
                if (d.x >= TIP && d.slot !== d.slot) takeSlot(d);
                if (d.x >= 500) toLine(d);
              } else {
                d.y += d.drift * dt;
                var off = Math.abs(d.y - CY);
                /* dropped where the envelope passes it, or at its own drop point */
                if ((d.x > 260 && off > env(d.x) + 1) || d.x >= d.dropX) {
                  d.state = COAST; d.w = 0.8; d.cst = d.x; d.blk = 0;
                  d.cd = clamp(60 + rand() * 120, 16, 540 - d.x);
                  d.ca = d.vi * d.vi / (2 * 0.65 * d.cd);
                  d.spd = Math.max(d.spd, d.vi * 0.6);
                }
              }
            }
            break;
          case LINE:
            if (explore) d.spd = Math.min(slotSpeed(d, s.vLine, 1.1), d.spd + 90 * dt);
            else d.spd = Math.min(s.vLine, d.spd + 90 * dt);
            d.x += d.spd * dt;
            d.y += (CY - d.y) * (1 - Math.exp(-dt / 0.35));
            if (d.x > X1 + R + 2) toWait(d);
            break;
          case COAST:
            /* rolls on for a third of its way, then slows to a halt well outside the envelope */
            var o = Math.abs(d.y - CY), sg = d.y >= CY ? 1 : -1;
            /* a ring settles a clear dot's width outside the envelope, off the kept dots' way */
            var inBand = o < env(d.x) + 26 || (d.x > 430 && o < 52);
            if (!inBand && d.x - d.cst > 0.35 * d.cd) d.spd = Math.max(0, d.spd - d.ca * dt);
            d.x += d.spd * dt;
            if (!inBand && d.spd >= 8) steer(d, dt);
            if (d.x > 548) d.x = 548;   // a dropped candidate never leaves the stage
            if (inBand) d.y += sg * Math.max(Math.abs(d.drift), 26) * dt;
            else d.y += sg * (10 + Math.abs(d.drift) * 0.4) * (d.spd / d.vi) * dt;
            /* it turns hollow as it crosses the dashed line, not before */
            look(d, clamp((o - env(d.x) - 12) / 14, 0, 1));
            if (!inBand && (d.spd <= 0 || d.blk > 0.3)) toRing(d);
            else if (inBand && (d.blk > 1.2 || d.x >= 536)) toFade(d);   // wedged among the kept dots: it just goes
            break;
          case RING:
            if (now - d.t > d.lg) toFade(d);
            break;
          case FADE:
            /* something is about to cross it: hurry the fade to a quarter second, never cut it */
            if (d.fr < 4 && reached(d)) d.fr = 4;
            d.fa -= d.fr * dt;
            if (d.fa <= 0) toWait(d);
            else d.el.setAttribute('opacity', d.fa.toFixed(3));
            break;
        }
      }
      buildGrid();
      separate(); walls();
      separate(); walls();
      separate(); walls();
      separateX(); walls();
      separateX(); walls();
      for (i = 0; i < dots.length; i++) {
        d = dots[i];
        if (d.state === FREE) {
          if ((d.x - d.ox) < 0.4 * d.spd * dt) { if (!d.kept) d.spd *= explore ? 0.85 : 0.3; d.blk += dt; } else d.blk = 0;
        } else if (d.state === COAST) {
          if ((d.x - d.ox) < 0.3 * d.spd * dt) d.blk += dt; else d.blk = 0;
        }
      }
      /* the throat mouth: one dot through every T seconds, the one nearest the mouth */
      if (!explore && now - lastRel >= s.T) {
        var lead = null;
        for (i = 0; i < dots.length; i++) {
          d = dots[i];
          if (d.state !== FREE || d.x < s.gate - 40) continue;
          if (!lead || d.x > lead.x) lead = d;
        }
        if (lead) { toLine(lead); lead.spd = 30; lastRel = now; }
      }
      /* pour: dots that left at the right come back in at the left when there is room.
         In panel 3 a kept dot enters every G3 / vLine seconds, which is what spaces the line. */
      var keptDue = explore && now >= keptNext && keptRear() - XIN >= G3 - 4;
      if (waiting > 0 && keptDue && pour(true, 8, SEP + 3)) { keptNext = now + s.keptEvery; keptDue = false; }
      if (waiting > 0 && !keptDue && now >= nextSpawn && pour(false, 3, 31)) nextSpawn = now + s.gap[0] + rand() * (s.gap[1] - s.gap[0]);
    }
    function keptRear() {
      var rear = 1e9;
      for (var i = 0; i < dots.length; i++) {
        var d = dots[i];
        if ((d.state === LINE || (d.state === FREE && d.kept)) && d.x < rear) rear = d.x;
      }
      return rear;
    }
    function pour(kept, tries, room) {
      for (var t = 0; t < tries; t++) {
        var y = CY + (rand() * 2 - 1) * (H - WALL - 2);
        if (!roomAt(XIN, y, room)) continue;
        var d = null;
        for (var i = 0; i < dots.length; i++) if (dots[i].state === WAIT) { d = dots[i]; break; }
        if (!d) return false;
        d.state = FREE; d.w = 1; d.x = d.ox = XIN; d.y = d.oy = y; waiting--;
        d.vi = s.vFree * (VLO + rand() * (VHI - VLO)); d.spd = d.vi * 0.8; d.blk = 0; d.rad = pickRad();
        if (explore) { setKept(d, kept); if (kept) d.spd = d.vi; }
        return true;
      }
      return false;
    }
    function render(alpha) {
      for (var i = 0; i < dots.length; i++) {
        var d = dots[i]; if (d.state === WAIT) continue;
        var x = d.ox + (d.x - d.ox) * alpha, y = d.oy + (d.y - d.oy) * alpha;
        var sx = x.toFixed(1), sy = y.toFixed(1);
        if (sx !== d.lx) { d.el.setAttribute('cx', sx); d.lx = sx; }
        if (sy !== d.ly) { d.el.setAttribute('cy', sy); d.ly = sy; }
      }
    }
    return { step: step, render: render, dots: dots, panel: p };
  }

  var sims = panels.map(makeSim);
  /* settle the sketch layout into the moving model before the first paint: two model seconds
     in coarse steps, short enough (about 25 ms) not to hold up the first paint */
  for (var w = 0; w < 60; w++) for (var si = 0; si < sims.length; si++) sims[si].step(2 * STEP);
  for (var ri = 0; ri < sims.length; ri++) sims[ri].render(1);

  /* ---- one animation loop for all panels ---- */
  var running = false, visible = true, dead = false, paused = false, last = 0, acc = 0, raf = 0;
  var toggle = document.getElementById('pipes-toggle');
  function frame(now) {
    raf = 0;
    if (!running) return;
    var dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    acc += dt;
    var steps = 0, i;
    while (acc >= STEP && steps < 3) {
      for (i = 0; i < sims.length; i++) sims[i].step(STEP);
      acc -= STEP; steps++;
    }
    if (acc > STEP) acc = STEP;
    var alpha = acc / STEP;
    for (i = 0; i < sims.length; i++) sims[i].render(alpha);
    raf = requestAnimationFrame(frame);
  }
  function start() {
    if (running || dead) return;
    running = true; last = performance.now(); acc = 0;
    if (!raf) raf = requestAnimationFrame(frame);
  }
  function stop() { running = false; if (raf) { cancelAnimationFrame(raf); raf = 0; } }
  function sync() { if (dead) return; if (visible && !document.hidden && !paused) start(); else stop(); }
  /* the one control on the figure: pause and resume (hidden while the figure is static) */
  if (toggle) {
    toggle.hidden = false;
    toggle.addEventListener('click', function () {
      paused = !paused;
      toggle.setAttribute('aria-pressed', paused ? 'true' : 'false');
      toggle.textContent = paused ? 'Play animation' : 'Pause animation';
      sync();
    });
  }
  if (typeof IntersectionObserver === 'function') {
    new IntersectionObserver(function (entries) {
      visible = entries.some(function (e) { return e.isIntersecting; }); sync();
    }, { rootMargin: '80px' }).observe(svg.closest('figure') || svg);
  }
  document.addEventListener('visibilitychange', sync);
  if (motionQuery && typeof motionQuery.addEventListener === 'function') {
    motionQuery.addEventListener('change', function (e) {
      if (!e.matches || dead) return;
      dead = true; stop(); drawStatic();
      if (toggle) toggle.hidden = true;
    });
  }
  sync();

  if (svg.hasAttribute('data-debug')) svg.__pipes = { sims: sims, step: function (n) { for (var k = 0; k < n; k++) for (var i = 0; i < sims.length; i++) sims[i].step(STEP); } };
})();
