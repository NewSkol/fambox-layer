// FamBox dashboard: keeps the page live. Everything also works without this file
// (forms post and the page reloads); this only makes it smooth.
(() => {
  const $ = s => document.querySelector(s);
  document.documentElement.classList.add('js');
  const keys = () => new Set([...document.querySelectorAll('[data-key]')].map(el => el.dataset.key));

  // Swap in the fresh page; whatever is new "pulls up a chair".
  let busy = false, again = false, lineSaid = '';
  // someone is filling in a form on this page (not yet sent): a live refresh would wipe it, so it waits
  const dirty = () => [...document.querySelectorAll('main form:not(.act):not(.task) :is(input, textarea, select)')].some(el =>
    el.type === 'checkbox' || el.type === 'radio' ? el.checked !== el.defaultChecked
      : el.tagName === 'SELECT' ? el.selectedIndex !== Math.max(0, [...el.options].findIndex(o => o.defaultSelected))
      : el.value !== el.defaultValue);
  document.addEventListener('input', () => { if (!dirty() && refresh.waiting) { refresh.waiting = false; refresh(); } });
  async function refresh() {
    if (dirty()) { refresh.waiting = true; return; }
    if (busy) { again = true; return; }
    busy = true;
    try {
      const before = keys();
      const url = new URL(location.href);
      url.searchParams.set('partial', '1');
      const res = await fetch(url, { cache: 'no-store' });
      if (res.ok && dirty()) refresh.waiting = true;  // they started filling in a form while the page was on its way
      else if (res.ok) {
        const doc = new DOMParser().parseFromString(await res.text(), 'text/html');  // inert: nothing in it runs
        const fresh = document.adoptNode(doc.querySelector('main'));
        document.body.classList.add('refreshed');
        const typing = document.activeElement;  // keep what someone is typing right now
        const keep = typing && typing.id && 'value' in typing ? [typing.id, typing.value] : null;
        $('main').replaceWith(fresh);
        const typed = keep && document.getElementById(keep[0]);
        if (typed) { typed.value = keep[1]; typed.focus(); }
        tick.chip = false;
        fresh.querySelectorAll('[data-key]').forEach(el => { if (!before.has(el.dataset.key)) el.classList.add('arrive'); });
        tick(); wallTick(); slides();
        const said = $('[data-line-out]');  // the Money line's last answer stays through live refreshes
        if (said && lineSaid && !said.textContent) said.textContent = lineSaid;
      }
    } catch (_) { /* offline for a moment: the next change or reconnect refreshes */ }
    busy = false;
    if (again) { again = false; refresh(); }
  }

  // Any change, from this screen, another screen or Pip, arrives here (pages marked data-live).
  if ($('main[data-live]')) {
    const es = new EventSource('/changes');
    es.onmessage = refresh;
    es.onopen = refresh;  // back after sleep or a restart: catch up on what changed meanwhile
  }

  // Buttons post without leaving the page (dashboard buttons only; moving-in forms post normally).
  document.addEventListener('submit', async ev => {
    const f = ev.target;
    if (f.method !== 'post' || !f.matches('form.act, form.task')) return;
    ev.preventDefault();
    try { await fetch(f.action, { method: 'POST', body: new URLSearchParams(new FormData(f, ev.submitter)), redirect: 'manual' }); }
    catch (_) { return f.submit(); }
    f.reset();  // a sent message leaves the box empty
    if (f.action.includes('/drop-') || f.action.endsWith('/dismiss') || f.action.endsWith('/accept')) {
      const url = new URL(location.href); url.searchParams.delete('look'); history.replaceState(null, '', url);
    }
    refresh();
  });

  // Theme switch: remembered on this device in a cookie so the server draws it straight away.
  document.addEventListener('click', ev => {
    const b = ev.target.closest('[data-theme-set]');
    if (!b) return;
    const t = b.dataset.themeSet;
    document.documentElement.dataset.theme = t === 'light' ? 'day' : t;  // Auto is set by tick() below
    document.cookie = `theme=${t}; path=/; max-age=31536000; SameSite=Lax`;
    document.querySelectorAll('[data-theme-set]').forEach(x => x.classList.toggle('on', x === b));
    tick();
  });

  // The clock and the sun move with the time, in the family's own time zone.
  function tick() {
    // On a phone the chosen person's chip may sit off screen: keep it in view.
    const on = $('.pchips .on');
    if (on && !tick.chip) { tick.chip = true; on.parentElement.scrollLeft = on.offsetLeft - 18; }
    const parts = new Intl.DateTimeFormat('en-GB', { timeZone: $('main').dataset.tz, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
      .formatToParts(new Date());
    const h = +parts.find(x => x.type === 'hour').value, m = +parts.find(x => x.type === 'minute').value;
    const hm = String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0');
    // the home screen is drawn for the time of day (morning, tonight, tomorrow, the week ahead): a new hour, a fresh look
    if (tick.hour !== undefined && tick.hour !== h && $('[data-moment]')) { tick.hour = h; refresh(); }
    tick.hour = h;
    document.querySelectorAll('[data-clock]').forEach(el => { el.textContent = hm; });
    // Auto is Night after sunset and Day from sunrise (the box knows where the family lives). The old "day" was Auto.
    // Day and Night never change by themselves. Same rule as shown_theme() in dashboard.py.
    const sun = $('main').dataset.sun, chosen = (document.cookie.match(/(?:^|; )theme=(\w+)/) || [])[1] || 'auto';
    if (!['light', 'night', 'paper'].includes(chosen) && !$('main.wall')) {
      const [rise, set] = (sun || '00:00,24:00').split(',');
      document.documentElement.dataset.theme = hm >= rise && hm < set ? 'day' : 'night';
    }
    const r = $('[data-ribbon]');
    if (r) {
      const [a, b] = r.dataset.ribbon.split(',').map(Number);
      const x = Math.max(0, Math.min(100, (h + m / 60 - a) / (b - a) * 100));
      r.querySelector('.sun').style.left = x.toFixed(1) + '%';
    }
  }
  setInterval(tick, 15000);
  tick();

  // A new day: draw it again.
  const today = () => new Date().toLocaleDateString('en-CA', { timeZone: $('main').dataset.tz });
  let day = today();
  setInterval(() => { if (today() !== day) { day = today(); refresh(); } }, 60000);

  // Moving in, step 5: the code is checked as soon as it is pasted.
  let wait;
  document.addEventListener('input', ev => {
    const el = ev.target;
    if (el.matches('[data-agent-name]')) document.querySelectorAll('[data-agent-say]').forEach(x => { x.textContent = el.value || 'Pip'; });
    if (!el.matches('[data-autocheck]')) return;
    const card = el.closest('.pastecard');
    card.classList.remove('bad');
    card.querySelectorAll('.err, [data-hint], .quietlink').forEach(x => { x.hidden = true; });
    card.querySelector('.checking').hidden = !el.value.trim();
    clearTimeout(wait);
    if (el.value.trim()) wait = setTimeout(() => el.form.requestSubmit(el.form.querySelector('[name=check]')), 900);
  });
  // On a phone the Telegram guide shows one step at a time (on a laptop, and without JavaScript, all four).
  const guides = [...document.querySelectorAll('details.guide')];
  if (matchMedia('(max-width: 599px)').matches) guides.forEach((g, i) => { g.open = i === 0; });
  guides.forEach(g => g.addEventListener('toggle', () => {
    if (g.open && matchMedia('(max-width: 599px)').matches) guides.forEach(x => { if (x !== g) x.open = false; });
  }));
  // Taking someone off the list asks first.
  document.addEventListener('click', ev => {
    const b = ev.target.closest('[data-confirm]');
    if (b && !confirm(b.dataset.confirm)) ev.preventDefault();
  });
  // "Show me where to find it", "I need help": open the right card.
  document.addEventListener('click', ev => {
    const a = ev.target.closest('[data-open]');
    if (!a) return;
    const box = document.getElementById(a.dataset.open);
    if (box) box.open = true;
  });

  // ---------- prompts 07 to 09 ----------
  const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches || document.documentElement.dataset.theme === 'paper';
  const familyNow = () => {
    const p = new Intl.DateTimeFormat('en-GB', { timeZone: $('main').dataset.tz, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(new Date());
    return +p.find(x => x.type === 'hour').value * 60 + +p.find(x => x.type === 'minute').value;
  };

  // Countdowns: each morning the top page tears off and falls (reduced motion: the number just changes).
  (function tearOff() {
    const pages = document.querySelectorAll('.countdowns .tear[data-n]');
    const day = new Date().toDateString();
    if (!pages.length || localStorage.getItem('fb-torn') === day) return;
    const first = localStorage.getItem('fb-torn') !== null;
    localStorage.setItem('fb-torn', day);
    if (!first || reduced()) return;  // a new screen has nothing to tear off yet
    pages.forEach(p => {
      const old = p.cloneNode(true);
      old.classList.add('tearing');
      old.removeAttribute('data-key');
      const b = old.querySelector('b');
      if (b && /^\d+$/.test(b.textContent)) b.textContent = +b.textContent + 1;
      p.appendChild(old);
      old.addEventListener('animationend', () => old.remove());
    });
  })();

  // Photos block (large): a slow slideshow.
  function slides() {
    clearInterval(slides.t);
    const imgs = document.querySelectorAll('.photos.slides img');
    if (imgs.length < 2) return;
    let i = 0;
    slides.t = setInterval(() => { imgs[i].classList.remove('on'); i = (i + 1) % imgs.length; imgs[i].classList.add('on'); }, 8000);
  }
  slides();

  // The add-a-dream sheet: the picture follows the choice; "about €590 a month" follows the amount and date.
  document.addEventListener('change', ev => {
    const r = ev.target.closest('.covers input[name=cover]');
    const hero = $('[data-cover-preview]');
    if (r && hero) hero.className = hero.className.replace(/cover-\d+/, 'cover-' + r.value);
  });
  function monthly() {
    const out = $('[data-monthly]');
    if (!out) return;
    const amount = parseFloat(($('[data-monthly-amount]').value || '').replace(/[^\d.,]/g, '').replace(',', '.'));
    const when = $('[data-monthly-date]').value, dated = $('input[name=when][value=date]').checked;
    if (!amount || !when || !dated) { out.textContent = ''; return; }
    const d = new Date(when), now = new Date();
    const months = Math.max(1, (d.getFullYear() - now.getFullYear()) * 12 + d.getMonth() - now.getMonth());
    out.textContent = `about €${(Math.ceil(amount / months / 10) * 10).toLocaleString('en-GB')} a month`;
  }
  document.addEventListener('input', ev => { if (ev.target.closest('#dreamform')) monthly(); });
  document.addEventListener('change', ev => { if (ev.target.closest('#dreamform')) monthly(); });
  monthly();

  // Photos: made smaller on this device before sending (phone photos are large), then sent straight away.
  document.addEventListener('change', async ev => {
    const input = ev.target.closest('form[data-photo] input[type=file]');
    if (!input || !input.files[0]) return;
    const form = input.form, file = input.files[0];
    let blob = file;
    try {
      const img = await createImageBitmap(file);
      const k = Math.min(1, 1600 / Math.max(img.width, img.height));
      const c = document.createElement('canvas');
      c.width = Math.round(img.width * k); c.height = Math.round(img.height * k);
      c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
      blob = await new Promise(ok => c.toBlob(ok, 'image/jpeg', 0.85)) || file;
    } catch (_) { /* this browser cannot read it here: send it as it is */ }
    const data = new FormData(form);
    data.set('photo', blob, 'photo.jpg');
    let res = null;
    try { res = await fetch(form.action, { method: 'POST', body: data }); } catch (_) { /* offline: say so below */ }
    location.href = res && res.ok && res.redirected ? res.url : (form.dataset.fail || '/photos?problem=photo');
  });

  // Edit mode: drag a block to move it; the others make room. Saved for every device when it is let go.
  document.addEventListener('pointerdown', ev => {
    const slot = ev.target.closest('.eslot');
    if (!slot || ev.target.closest('button') || ev.button) return;
    ev.preventDefault();  // no text selection while dragging
    const grid = slot.parentElement, r = slot.getBoundingClientRect();
    const dx = ev.clientX - r.left, dy = ev.clientY - r.top;
    let moved = false;
    const hole = slot.cloneNode(false);
    hole.className = slot.className + ' hole';
    hole.removeAttribute('data-key');
    const move = e => {
      if (!moved) {
        if (Math.hypot(e.clientX - ev.clientX, e.clientY - ev.clientY) < 6) return;
        moved = true;
        slot.before(hole);
        Object.assign(slot.style, { width: r.width + 'px', height: r.height + 'px' });
        slot.classList.add('lifted');
        grid.classList.add('dragging');
      }
      slot.style.left = e.clientX - dx + 'px';
      slot.style.top = e.clientY - dy + 'px';
      const over = document.elementFromPoint(e.clientX, e.clientY)?.closest('.eslot:not(.lifted):not(.hole), .dropslot');
      if (!over) return;
      if (over.classList.contains('dropslot')) { over.before(hole); return; }
      const o = over.getBoundingClientRect();
      const after = e.clientY > o.top + o.height / 2 || (e.clientX > o.left + o.width / 2 && e.clientY > o.top);
      if (after) over.after(hole); else over.before(hole);
    };
    const up = async () => {
      removeEventListener('pointermove', move);
      removeEventListener('pointerup', up);
      if (!moved) return;
      hole.replaceWith(slot);
      grid.classList.remove('dragging');
      slot.classList.remove('lifted');
      slot.removeAttribute('style');
      const order = [...grid.querySelectorAll('.eslot')].map(x => x.dataset.kind).join(',');
      await fetch('/do/block/0/order', { method: 'POST', body: new URLSearchParams({ order }), redirect: 'manual' });
      refresh();
    };
    addEventListener('pointermove', move);
    addEventListener('pointerup', up);
  });

  // Wall mode. Morning: far blocks. From 20:00: night, tomorrow, a sleepy agent; 30% at 22:30, dark from
  // midnight to 07:00. After five quiet minutes: the family's own photos. A tap wakes it for a minute;
  // a tap on the wall itself shows the full table for a minute.
  let touched = Date.now();
  function wallTick() {
    const w = $('main.wall');
    if (!w) return;
    const m = familyNow(), quiet = Date.now() - touched;
    const night = m >= 20 * 60 || m < 5 * 60, awake = quiet < 60000;
    w.querySelector('.wday').hidden = night;
    w.querySelector('.wnight').hidden = !night;
    const idle = !night && quiet > 5 * 60000;
    w.querySelector('.widle').hidden = !idle;
    w.classList.toggle('dim', !awake && m >= 22 * 60 + 30);
    w.classList.toggle('off', !awake && m < 7 * 60);
  }
  if ($('main.wall')) {
    setInterval(wallTick, 15000);
    wallTick();
    setInterval(() => {  // one picture every few minutes (the list may have changed with a live refresh)
      const pics = [...document.querySelectorAll('.widle img')];
      if (pics.length < 2) return;
      const f = (Math.max(0, pics.findIndex(p => p.classList.contains('on'))) + 1) % pics.length;
      pics.forEach((p, i) => p.classList.toggle('on', i === f));
      const cap = $('[data-cap]'); if (cap) cap.textContent = pics[f].dataset.caption || '';
    }, 180000);
    document.addEventListener('pointerdown', () => {
      const w = $('main.wall');
      const asleep = !w.querySelector('.widle').hidden || w.classList.contains('dim') || w.classList.contains('off');
      touched = Date.now();
      if (asleep) wallTick(); else location.href = '/?from=wall';
    });
  }
  if (new URLSearchParams(location.search).get('from') === 'wall') {  // back to the wall after a quiet minute
    let last = Date.now();
    ['pointerdown', 'keydown', 'scroll'].forEach(t => addEventListener(t, () => { last = Date.now(); }, { passive: true }));
    setInterval(() => { if (Date.now() - last > 60000) location.href = '/wall'; }, 5000);
  }
  // ---------- phases B and C: the Money room's one line, statement files, a dream's journal ----------
  // The line says what it will do while it is typed (the same rules Pip uses), then does it without leaving the page.
  let lineWait;
  document.addEventListener('input', ev => {
    const el = ev.target.closest('form[data-line] input[name=text]');
    if (!el) return;
    lineSaid = '';
    clearTimeout(lineWait);
    lineWait = setTimeout(async () => {
      const out = el.form.querySelector('[data-line-out]');
      if (!el.value.trim()) { out.textContent = ''; out.classList.remove('ok'); return; }
      try {
        const r = await (await fetch('/money/line?text=' + encodeURIComponent(el.value), { cache: 'no-store' })).json();
        out.textContent = r.ok ? r.said : 'Not sure what that is yet. Try "40 groceries", "paid electricity" or "put 20 in Lapland".';
        out.classList.toggle('ok', r.ok);
      } catch (_) { /* offline: the line still posts */ }
    }, 120);
  });
  document.addEventListener('submit', async ev => {
    const f = ev.target;
    if (!f.matches('form[data-line]')) return;
    ev.preventDefault();
    let r;
    try {
      r = await (await fetch(f.action, { method: 'POST', body: new URLSearchParams(new FormData(f)), headers: { Accept: 'application/json' } })).json();
    } catch (_) { return f.submit(); }
    lineSaid = r.said;
    if (r.ok) { f.querySelector('input[name=text]').value = ''; await refresh(); }
    const out = document.querySelector('form[data-line] [data-line-out]');
    if (out) { out.textContent = r.said; out.classList.toggle('ok', false); }
    if (r.ok) document.querySelector('form[data-line] input[name=text]')?.focus();
  });
  // A statement file: dropped on the box or chosen, it is sent straight away.
  document.addEventListener('dragover', ev => { const z = ev.target.closest('.dropzone'); if (z) z.classList.add('over'); });
  document.addEventListener('dragleave', ev => { const z = ev.target.closest('.dropzone'); if (z) z.classList.remove('over'); });
  document.addEventListener('change', ev => {
    const input = ev.target.closest('form[data-autosend] input[type=file]');
    if (input && input.files[0]) { input.form.classList.add('busy'); input.form.submit(); }
    const j = ev.target.closest('form[data-photo-optional] input[type=file]');
    if (j) j.closest('label').classList.toggle('has', !!j.files[0]);
  });
  // The journal: a photo, if there is one, is made smaller on this device before it is sent.
  document.addEventListener('submit', async ev => {
    const f = ev.target;
    const file = f.matches('form[data-photo-optional]') && f.querySelector('input[type=file]').files[0];
    if (!file) return;
    ev.preventDefault();
    const data = new FormData(f);
    data.set('photo', await smaller(file), 'photo.jpg');
    let res = null;
    try { res = await fetch(f.action, { method: 'POST', body: data }); } catch (_) { /* offline: say so below */ }
    location.href = res && res.ok && res.redirected ? res.url : f.action.replace(/\/do\/journal\/(\d+)\/add/, '/dreams/$1?problem=journal');
  });
  async function smaller(file) {
    try {
      const img = await createImageBitmap(file);
      const k = Math.min(1, 1600 / Math.max(img.width, img.height));
      const c = document.createElement('canvas');
      c.width = Math.round(img.width * k); c.height = Math.round(img.height * k);
      c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
      return await new Promise(ok => c.toBlob(ok, 'image/jpeg', 0.85)) || file;
    } catch (_) { return file; }
  }
})();
