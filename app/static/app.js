/* goodreads UI.
 *
 * No framework, no build step — the app is a handful of render functions and
 * one polling loop. The structure that matters:
 *
 *   - `state` holds the last /api/state payload; views are pure functions of it.
 *   - Routing is hash-based so deep links to a book or a service survive a refresh.
 *   - Anything that needs fresh data calls /api/... and refreshes; nothing is
 *     cached client-side, because the server is the source of truth and it is
 *     a few milliseconds away.
 *
 * The UX goal throughout: a failure should be findable in one click, should
 * arrive with its cause and its fix already attached, and should say *which
 * service* it came from — because that is the first question anyone asks of a
 * self-hosted stack, and the answer is now recorded rather than guessed at.
 */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

let state = null;
let issues = null;
let settingsFields = null;
let currentFilter = 'attention';
let searchTerm = '';
let serviceDetail = null;      // the last /api/services/<name> payload

const BASE = '';

/* ------------------------------------------------------------ utilities */
function escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function toast(title, body = '', kind = '') {
  const host = $('#toasts');
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.innerHTML = `<div class="tt">${escapeHtml(title)}</div>` +
                 (body ? `<div class="tb">${escapeHtml(body)}</div>` : '');
  host.appendChild(el);
  setTimeout(() => el.remove(), kind === 'err' ? 11000 : 6000);
}

async function api(path, opts) {
  const res = await fetch(BASE + path, opts);
  if (res.status === 401) {
    window.location.replace('/login');
    throw new Error('session expired');
  }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
  if (!res.ok) {
    const msg = (data && (data.detail || data.error)) || `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return data;
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return '';
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  const h = Math.floor(s / 3600);
  return `${h}h ${Math.floor((s % 3600) / 60)}m`;
}

function relTime(iso) {
  if (!iso) return 'never';
  const then = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : iso + 'Z');
  const secs = Math.max(0, (Date.now() - then.getTime()) / 1000);
  if (secs < 60) return 'just now';
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

function stamp(ts) {
  return String(ts || '').replace('T', ' ').slice(5, 19);
}

/* A download in flight, with a real percentage and a projected finish.
 * Without this, "working" and "wedged" read identically. */
function progressBlock(book) {
  const info = (state.progress || {})[String(book.id)];
  if (!info) return '';
  const pct = Math.max(0, Math.min(100, info.progress || 0));
  const bits = [];
  if (info.elapsed_seconds != null) bits.push(`${formatDuration(info.elapsed_seconds)} elapsed`);
  if (info.eta_seconds != null) bits.push(`~${formatDuration(info.eta_seconds)} left`);
  if (info.source) bits.push(info.source);
  if (info.message) bits.push(info.message);

  const label = info.state === 'queued'
    ? 'queued — waiting for a slot'
    : `${pct.toFixed(0)}%`;

  return `<div class="progress">
      <div class="bar"><span style="width:${info.state === 'queued' ? 0 : pct}%"></span></div>
      <div class="pmeta"><b>${escapeHtml(label)}</b>${bits.length ? ' · ' + escapeHtml(bits.join(' · ')) : ''}</div>
    </div>`;
}

/* ---------------------------------------------------------- vocabulary */
const STAGE_LABEL = {
  classify: 'classify',
  acquire_ebook: 'ebook',
  acquire_audiobook: 'audiobook',
  place: 'place',
  index: 'index',
  notebook: 'notebook',
  verify: 'verify',
  shelve: 'shelve',
};

const KIND_LABEL = {
  auth: 'credentials',
  network: 'unreachable',
  server: 'service error',
  notfound: 'not found',
  data: 'no data',
};

/* Mirrors health.canonical() in the Python. The client classes name
 * themselves in prose ("open notebook") while the service registry uses a
 * slug ("opennotebook"), and both end up in the database — so anything that
 * compares the two has to normalise first. */
function canonicalService(name) {
  return String(name || '').trim().toLowerCase().replace(/\s+/g, '');
}

/* A failure's service, as a link to that service's own page and log.
 *
 * `run.service` is recorded at the point of failure. The fallback below only
 * covers rows written before that column existed, and is deliberately narrow:
 * it never invents an attribution, it just recovers one that was embedded in
 * the message as "kavita: ...". */
const LEGACY_SERVICES = [
  ['shelfmark', 'Shelfmark'],
  ['kavita', 'Kavita'],
  ['booklore', 'BookLore'],
  ['grimmory', 'Grimmory'],
  ['audiobookshelf', 'Audiobookshelf'],
  ['open notebook', 'Open Notebook'],
];

function serviceOfRun(run) {
  const recorded = canonicalService(run && run.service);
  if (recorded) return recorded;
  const hay = String((run && run.detail) || '').toLowerCase();
  for (const [needle] of LEGACY_SERVICES) {
    if (hay.includes(needle)) return canonicalService(needle);
  }
  return '';
}

function labelForService(name) {
  const key = canonicalService(name);
  if (!key) return '';
  const match = (state?.health?.services || []).find(s => s.service === key);
  if (match) return match.label;
  return { goodreads: 'Goodreads', settings: 'Settings' }[key] || key;
}

function serviceChip(name) {
  const key = canonicalService(name);
  if (!key) return '';
  return `<a class="chip svc-tag" href="#/service/${encodeURIComponent(key)}"
             onclick="event.stopPropagation()"
             title="Open ${escapeHtml(labelForService(key))}">${escapeHtml(labelForService(key))}</a>`;
}

const FORMAT_STAGES = ['acquire_ebook', 'acquire_audiobook'];
const ACTIONABLE_KINDS = ['auth', 'network', 'server'];

function housed(status) {
  return ['blocked', 'failed', 'pending'].includes(status);
}

/* A book's summary state. Mirrors book_state() in main.py.
 *
 * Four distinct things, kept apart on purpose. Lumping them together made the
 * attention list unreadable: 88 books "needed attention" when 81 of them were
 * simply books whose audiobook does not exist anywhere. */
function bookState(book) {
  const stages = book.stages || {};
  const entries = Object.entries(stages);
  const failed = entries.filter(([, r]) => (r || {}).status === 'failed');

  if (failed.length) {
    if (failed.some(([name]) => !FORMAT_STAGES.includes(name))) return 'failed';
    if (failed.some(([, r]) => ACTIONABLE_KINDS.includes(r.failure_kind))) return 'failed';
    const placed = (stages.place || {}).status === 'ok';
    return placed ? 'partial' : 'unavailable';
  }

  const outstanding = entries
    .filter(([, r]) => !['ok', 'skipped'].includes((r || {}).status))
    .map(([name]) => name);
  if (!outstanding.length) return 'done';

  if (outstanding.length === 1 && outstanding[0] === 'acquire_audiobook') {
    const placed = (stages.place || {}).status === 'ok';
    if (housed((stages.acquire_audiobook || {}).status) && placed) return 'partial';
  }
  return 'working';
}

/* ------------------------------------------------------------- routing */
function route() {
  const hash = window.location.hash.replace(/^#/, '') || '/';
  const [, head, param] = hash.split('/');
  return { name: head || 'dashboard', param: param || null };
}

function go(path) { window.location.hash = path; }

/* --------------------------------------------------------------- shell */
function renderMasthead() {
  const { name } = route();
  const nav = [
    ['/', 'Home', 'dashboard'],
    ['/books', 'My Books', 'books'],
    ['/services', 'Services', 'services'],
    ['/activity', 'Activity', 'activity'],
  ];
  const health = state?.health;
  const bad = health ? health.unhealthy.length : 0;

  $('#nav').innerHTML = nav.map(([href, label, key]) =>
    `<a href="#${href}" class="${name === key || (key === 'services' && name === 'service') ? 'active' : ''}">${label}` +
    (key === 'services' && bad ? ` <span class="chip failed">${bad}</span>` : '') +
    `</a>`).join('');

  const g = state?.goodreads || {};
  $('#headline').innerHTML = g.has_session
    ? `Goodreads<br><b>${escapeHtml(g.session_age)}</b>`
    : `Goodreads<br><b>not signed in</b>`;

  // The always-visible answer to "is anything broken right now".
  const pill = $('#health-pill');
  if (!health || !health.services.length) {
    pill.className = 'health-pill';
    pill.innerHTML = '<span class="dot idle"></span> Services';
    return;
  }
  if (bad) {
    const auth = (health.auth_failures || []).length;
    pill.className = 'health-pill bad';
    pill.innerHTML = `<span class="dot err"></span> ${bad} service${bad > 1 ? 's' : ''} ` +
      (auth ? `rejecting credentials` : `unreachable`);
    return;
  }
  const total = health.services.length;
  pill.className = 'health-pill';
  pill.innerHTML = `<span class="dot ok"></span> ${total} service${total > 1 ? 's' : ''} healthy`;
}

/* The single most important piece of feedback in the app: if a credential is
 * broken, say so loudly, at the top, with a link to the fix. */
function renderBanner() {
  const host = $('#banner');
  const health = state?.health;
  if (!host) return;
  if (!health) { host.innerHTML = ''; return; }

  const authFails = health.auth_failures || [];
  const down = health.unhealthy || [];

  if (authFails.length) {
    host.innerHTML = `
      <div class="banner err">
        <span class="dot err"></span>
        <span><b>${authFails.length} service${authFails.length > 1 ? 's' : ''} rejecting credentials</b>
        — ${authFails.map(s => `<a href="#/service/${s.service}">${escapeHtml(s.label)}</a>`).join(', ')}.
        Books touching ${authFails.length > 1 ? 'these' : 'this'} will keep failing until it is fixed.</span>
        <span class="grow"></span>
        <button class="primary" onclick="go('/services')">Fix credentials</button>
      </div>`;
    return;
  }
  if (down.length) {
    host.innerHTML = `
      <div class="banner warn">
        <span class="dot warn"></span>
        <span><b>${down.length} service${down.length > 1 ? 's' : ''} unreachable</b>
        — ${down.map(s => `<a href="#/service/${s.service}">${escapeHtml(s.label)}</a>`).join(', ')}.
        Retries are automatic.</span>
        <span class="grow"></span>
        <button onclick="go('/services')">Details</button>
      </div>`;
    return;
  }
  const g = state?.goodreads || {};
  if (g.has_session === false) {
    host.innerHTML = `
      <div class="banner warn">
        <span class="dot warn"></span>
        <span><b>No Goodreads session.</b> Books cannot be discovered and nothing can be shelved until you sign in.</span>
        <span class="grow"></span>
        <button class="primary" onclick="window.location='/goodreads'">Sign in to Goodreads</button>
      </div>`;
    return;
  }
  host.innerHTML = '';
}

/* The right rail. Service health north-star: on every page, without a click,
 * so "which one broke" is answerable by looking rather than navigating. */
function renderRail() {
  const host = $('#rail');
  if (!host) return;
  const health = state?.health || { services: [] };
  const { name, param } = route();
  const current = name === 'service' ? canonicalService(param) : '';

  const rows = health.services.map(s => {
    const cls = s.ok === true ? 'ok' : s.ok === false ? (s.failure_kind === 'auth' ? 'err' : 'warn') : 'idle';
    const note = s.ok === false
      ? escapeHtml(KIND_LABEL[s.failure_kind] || 'down')
      : escapeHtml(s.detail || 'never checked');
    // The check time lives in the tooltip rather than its own column: a
    // narrow rail spent more width on "just now" repeated seven times than on
    // the detail it was squeezing to "10 li…".
    const when = s.checked ? `checked ${relTime(s.checked_at)}` : 'never checked';
    return `<a class="svc-row ${s.ok === false ? 'bad' : ''}"
               href="#/service/${s.service}" title="${escapeHtml(s.label)} — ${escapeHtml(when)}"
               style="${s.service === current ? 'background:var(--cream-2)' : ''}">
      <span class="dot ${cls}"></span>
      <span class="name">${escapeHtml(s.label)}</span>
      <span class="note">${note}</span>
    </a>`;
  }).join('');

  const g = state?.goodreads || {};
  const grSocket = `<a class="svc-row ${g.has_session ? '' : 'bad'}" href="/goodreads"
      title="Goodreads session">
      <span class="dot ${g.has_session ? 'ok' : 'warn'}"></span>
      <span class="name">Goodreads</span>
      <span class="note">${g.has_session ? escapeHtml(g.session_age || '') : 'not signed in'}</span>
    </a>`;

  host.innerHTML = `
    <div class="rail-card">
      <h3><span class="grow">Service status</span>
        <button class="tiny ghost" onclick="recheckServices(this)">Re-check</button></h3>
      <div class="body flush">
        <div class="svc-list">${rows}${grSocket}</div>
      </div>
    </div>

    <div class="rail-card">
      <h3>Run control</h3>
      <div class="body">
        <div class="row" style="margin-bottom:9px">
          <button class="tiny" onclick="sweepNow(this)">Sweep now</button>
          <button class="tiny" onclick="reconcileNow(this)">Check shelf</button>
        </div>
        <label class="row tight small" style="cursor:pointer">
          <input type="checkbox" ${state?.auto_shelve ? 'checked' : ''}
                 onchange="setAutoShelve(this.checked)" style="width:auto">
          Move finished books to a collected shelf
        </label>
        ${state?.disk_free_gb != null ? `<div class="small faint" style="margin-top:9px">
          ${state.disk_free_gb} GB free where books land</div>` : ''}
      </div>
    </div>`;
}

/* ----------------------------------------------------------- dashboard */
function viewDashboard() {
  const t = state?.totals || {};
  const autoOff = state && !state.auto_shelve;

  const tiles = [
    ['books', t.books, 'Books tracked', ''],
    ['done', t.complete, 'Completed', 'ok'],
    ['working', t.in_flight, 'In progress', 'gold'],
    ['failed', t.failed, 'Need attention', t.failed ? 'err' : ''],
    ['partial', t.partial, 'Audiobook missing', ''],
    ['unavailable', t.unavailable, 'Not found', ''],
    ['review', t.needs_review, 'Genre review', t.needs_review ? 'warn' : ''],
    ['shelved', t.shelved, 'Shelved', 'ok'],
  ];

  return `
    <div class="tiles">
      ${tiles.map(([key, n, label, cls]) => `
        <button class="tile ${cls} clickable" onclick="tileGo('${key}')">
          <div class="n">${n ?? 0}</div>
          <div class="k">${label}</div>
        </button>`).join('')}
    </div>

    ${autoOff ? `<div class="banner warn">
      <span class="dot warn"></span>
      <span><b>Auto-shelve is off.</b> Finished books are not being moved to a collected shelf.</span>
      <span class="grow"></span>
      <button onclick="setAutoShelve(true)">Turn on</button>
    </div>` : ''}

    ${renderLiveDownloads()}

    <div class="panel">
      <h2><span class="grow">What needs you</span>
        <button class="tiny ghost" onclick="go('/books')">All books</button>
      </h2>
      <div class="body" id="issue-preview">${renderIssuePreview()}</div>
    </div>

    <div class="panel">
      <h2><span class="grow">Recent activity</span>
        <button class="tiny ghost" onclick="go('/activity')">Full log</button>
      </h2>
      <div class="body flush log">${renderEvents((state?.events || []).slice(0, 14))}</div>
    </div>`;
}

function renderLiveDownloads() {
  const entries = Object.entries(state.progress || {});
  if (!entries.length) return '';
  const byId = {};
  (state.books || []).forEach(b => { byId[String(b.id)] = b; });
  const rows = entries
    .map(([id, info]) => ({ book: byId[id], info }))
    .filter(x => x.book)
    .sort((a, b) => (b.info.progress || 0) - (a.info.progress || 0));

  return `<div class="panel">
    <h2><span class="grow">Downloading now</span>
      <span class="faint small">${rows.length} in flight</span></h2>
    <div class="body">
      ${rows.map(({ book }) => `
        <div style="padding:8px 0;border-bottom:1px solid var(--line-soft)">
          <a class="title" style="font-family:var(--serif);font-weight:700;color:var(--ink-2)"
             href="#/book/${book.id}">${escapeHtml(book.title)}</a>
          ${progressBlock(book)}
        </div>`).join('')}
    </div>
  </div>`;
}

function tileGo(key) {
  const map = {
    failed: 'attention', partial: 'partial', unavailable: 'unavailable',
    review: 'review', working: 'working', done: 'done',
    books: 'all', shelved: 'done',
  };
  currentFilter = map[key] || 'all';
  go('/books');
}

function renderIssuePreview() {
  if (!issues) return '<div class="muted">Loading…</div>';
  const actionable = (issues.groups || []).filter(g => g.actionable);
  const review = issues.counts.needs_review || 0;

  if (!actionable.length && !review) {
    return '<div class="muted">Nothing needs attention. Every book is either progressing or done.</div>';
  }

  const rows = actionable.slice(0, 5).map(g => `
    <div class="stage-row">
      <div>
        <div class="stage-name">${escapeHtml(STAGE_LABEL[g.stage] || g.stage)}</div>
        <div class="stage-attempts">${g.books.length} book${g.books.length > 1 ? 's' : ''}</div>
      </div>
      <div>
        ${g.service ? serviceChip(g.service)
          : `<span class="chip ${g.kind === 'auth' ? 'auth' : 'failed'}">${escapeHtml(KIND_LABEL[g.kind] || 'failed')}</span>`}
      </div>
      <div class="stage-detail">${escapeHtml(g.detail.slice(0, 190))}
        <span class="why">${escapeHtml(g.fix)}</span></div>
      <div>${g.service
        ? `<a class="btn tiny" href="#/service/${encodeURIComponent(canonicalService(g.service))}">Open</a>`
        : `<button class="tiny" onclick="retryGroup('${escapeHtml(g.stage)}')">Retry all</button>`}</div>
    </div>`).join('');

  // Genre review is not a *failure*, so it is not in `groups` — but it is
  // still work a human has to do, and without this row the panel rendered
  // completely empty while the dashboard tile above it said "2".
  const reviewRow = review ? `
    <div class="stage-row">
      <div>
        <div class="stage-name">classify</div>
        <div class="stage-attempts">${review} book${review > 1 ? 's' : ''}</div>
      </div>
      <div><span class="chip blocked">review</span></div>
      <div class="stage-detail">No genre rule matched, so the fallback filed these.
        <span class="why">Set the category by hand on the book, or add a rule to categories.yml.</span></div>
      <div><button class="tiny" onclick="tileGo('review')">Review</button></div>
    </div>` : '';

  return rows + reviewRow;
}

/* --------------------------------------------------------------- books */
/* Books matching a filter. Takes the filter explicitly — an earlier version
 * read a module-level variable and mutated it while counting, so rendering the
 * counts silently changed which filter was active. */
function booksMatching(filter, term = searchTerm) {
  const books = state?.books || [];
  let out = books;
  if (filter === 'attention') out = books.filter(b => bookState(b) === 'failed');
  else if (filter === 'partial') out = books.filter(b => bookState(b) === 'partial');
  else if (filter === 'unavailable') out = books.filter(b => bookState(b) === 'unavailable');
  else if (filter === 'working') out = books.filter(b => bookState(b) === 'working');
  else if (filter === 'done') out = books.filter(b => bookState(b) === 'done');
  // A book with nothing found has no genre worth reviewing — there is no book
  // to file. Showing it under "Genre review" sent you looking for a
  // categorisation problem when the real answer was "not available anywhere".
  else if (filter === 'review') out = books.filter(b =>
    b.needs_review && bookState(b) !== 'unavailable');

  if (term) {
    const q = term.toLowerCase();
    out = out.filter(b =>
      (b.title || '').toLowerCase().includes(q) ||
      (b.author || '').toLowerCase().includes(q) ||
      (b.category || '').toLowerCase().includes(q));
  }
  return out;
}

function filteredBooks() { return booksMatching(currentFilter); }

function viewBooks() {
  const labels = {
    attention: 'Needs attention',
    partial: 'Audiobook missing',
    unavailable: 'Not found',
    working: 'In progress',
    done: 'Completed',
    review: 'Genre review',
    all: 'All',
  };
  const counts = {};
  Object.keys(labels).forEach(f => { counts[f] = booksMatching(f).length; });
  const list = filteredBooks();

  const notes = {
    partial: `These books are <b>in the library and verified</b>. An audiobook for them does not
      exist in any configured source — nothing is broken and there is nothing to fix.`,
    unavailable: `No ebook or audiobook was found for these in any source. Also not a fault —
      they simply are not available anywhere the pipeline can reach.`,
  };

  return `
    <div class="toolbar">
      <div class="filters">
        ${Object.keys(labels).map(f => `
          <button class="${currentFilter === f ? 'on' : ''}" onclick="setFilter('${f}')">
            ${labels[f]} <span class="faint">${counts[f]}</span>
          </button>`).join('')}
      </div>
      <span class="grow"></span>
      ${searchTerm ? `<span class="small muted">filtered by “${escapeHtml(searchTerm)}”
        <button class="tiny ghost" onclick="onHeaderSearch('')">clear</button></span>` : ''}
    </div>

    <div class="panel">
      ${notes[currentFilter] ? `<div class="body tight small muted">${notes[currentFilter]}</div>` : ''}
      ${list.length
        ? `<div class="book-list">${list.map(bookRow).join('')}</div>`
        : `<div class="empty">Nothing here.${
            currentFilter === 'attention' ? ' No book is currently failing.' : ''}</div>`}
    </div>`;
}

function bookRow(book) {
  const st = bookState(book);
  const chips = (state.stages || []).map(stage => {
    const run = (book.stages || {})[stage] || { status: 'pending' };
    const isAuth = run.status === 'failed' && run.failure_kind === 'auth';
    const isNoData = run.status === 'failed' && run.failure_kind === 'data';
    const cls = isAuth ? 'auth' : (isNoData ? 'nodata' : run.status);
    return `<span class="chip ${cls}" title="${escapeHtml(run.detail || '')}">
      ${escapeHtml(STAGE_LABEL[stage] || stage)}</span>`;
  }).join('');

  // The most useful single line: what is blocking this book, and who to blame.
  const blocker = (state.stages || [])
    .map(s => ({ s, r: (book.stages || {})[s] || {} }))
    .find(x => x.r.status === 'failed' || x.r.status === 'blocked');
  const live = progressBlock(book);

  let where;
  if (live) {
    where = live;
  } else if (blocker && blocker.r.status === 'failed') {
    const svc = serviceOfRun(blocker.r);
    where = `<div class="blocker">
        <span class="chip failed">${escapeHtml(STAGE_LABEL[blocker.s])}</span>
        ${svc ? serviceChip(svc) : ''}
        <div class="msg">${escapeHtml((blocker.r.detail || '').slice(0, 110))}</div>
      </div>`;
  } else if (blocker && blocker.r.status === 'blocked') {
    const svc = serviceOfRun(blocker.r);
    where = `<div class="blocker">
        <span class="chip blocked">${escapeHtml(STAGE_LABEL[blocker.s])} waiting</span>
        ${svc ? serviceChip(svc) : ''}
        <div class="msg soft">${escapeHtml((blocker.r.detail || '').slice(0, 110))}</div>
      </div>`;
  } else {
    where = `<div class="blocker"><span class="msg quiet">${st === 'done' ? 'complete' : 'working'}</span></div>`;
  }

  // The title is a real link as well as the row being clickable: a link is
  // keyboard-usable and can be middle-clicked.
  const cover = book.cover_url
    ? `<img class="cover" src="${escapeHtml(book.cover_url)}" alt="" loading="lazy"
             onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'cover blank',textContent:'\u{1F4D5}'}))">`
    : `<div class="cover blank">&#128213;</div>`;

  return `<div class="book-row" onclick="go('/book/${book.id}')">
    ${cover}
    <div>
      <a class="title" href="#/book/${book.id}" onclick="event.stopPropagation()">${escapeHtml(book.title)}</a>
      <div class="byline">${escapeHtml(book.author || 'unknown author')}${book.year ? ' · ' + book.year : ''}</div>
      <div class="meta">
        <span class="chip">${escapeHtml(book.category || 'uncategorised')}</span>
        ${book.genre_source ? `<span class="faint"> via ${escapeHtml(book.genre_source)}</span>` : ''}
        ${book.needs_review ? '<span style="color:var(--warn)"> · no genre rule matched</span>' : ''}
      </div>
      ${where}
    </div>
    <div class="side">
      <span class="chip ${st === 'failed' ? 'failed' : st}">${st}</span>
      <div class="pipeline">${chips}</div>
    </div>
  </div>`;
}

function setFilter(f) { currentFilter = f; render(); }

/* The header search is global: typing in it jumps to the book list and filters
   there, so it works from any view. */
function onHeaderSearch(value) {
  searchTerm = value;
  if (route().name !== 'books') {
    currentFilter = currentFilter === 'attention' ? 'all' : currentFilter;
    go('/books');
    return;
  }
  render();
}

/* ---------------------------------------------------------- book detail */
async function viewBook(id) {
  let detail;
  try { detail = await api(`/api/books/${id}`); }
  catch (err) { return `<div class="panel"><div class="body">Could not load book: ${escapeHtml(err.message)}</div></div>`; }

  const book = detail.book;
  const placed = detail.placed || {};
  const events = detail.events || [];

  const rows = (state.stages || []).map(stage => {
    const run = (book.stages || {})[stage] || { status: 'pending' };
    const isAuth = run.status === 'failed' && run.failure_kind === 'auth';
    const cls = isAuth ? 'auth' : run.status;
    const svc = serviceOfRun(run);
    const fix = issues
      ? (issues.groups.find(g => g.stage === stage
          && canonicalService(g.service) === svc
          && (g.sample === run.detail || g.detail === run.detail)) || {}).fix
      : '';
    return `<div class="stage-row">
      <div>
        <div class="stage-name">${escapeHtml(STAGE_LABEL[stage] || stage)}</div>
        ${run.attempts ? `<div class="stage-attempts">${run.attempts} attempt${run.attempts > 1 ? 's' : ''}</div>` : ''}
      </div>
      <div class="row tight">
        <span class="chip ${cls}">${escapeHtml(isAuth ? 'credentials' : run.status)}</span>
        ${svc ? serviceChip(svc) : ''}
      </div>
      <div class="stage-detail">${escapeHtml(run.detail || '—')}
        ${fix ? `<span class="why">${escapeHtml(fix)}</span>` : ''}</div>
      <div><button class="tiny" onclick="retryStage(${book.id},'${stage}',this)">Retry</button></div>
    </div>`;
  }).join('');

  const paths = Object.entries(placed).length
    ? `<div class="panel"><h2>Files</h2><div class="body"><dl class="kv">
        ${Object.entries(placed).map(([k, v]) =>
          `<dt>${escapeHtml(k)}</dt><dd class="mono">${escapeHtml(v)}</dd>`).join('')}
      </dl></div></div>`
    : '';

  const cats = (state.categories || []).map(c =>
    `<option value="${escapeHtml(c)}" ${c === book.category ? 'selected' : ''}>${escapeHtml(c)}</option>`).join('');
  const st = bookState(book);

  const cover = book.cover_url
    ? `<img class="cover-lg" src="${escapeHtml(book.cover_url)}" alt=""
             onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'cover-lg blank',textContent:'\u{1F4D5}'}))">`
    : `<div class="cover-lg blank">&#128213;</div>`;

  return `
    <div class="row" style="margin-bottom:12px">
      <button class="ghost" onclick="go('/books')">&larr; My Books</button>
      <span class="grow"></span>
      <span class="chip ${st === 'failed' ? 'failed' : st}">${st}</span>
    </div>

    <div class="panel">
      <div class="body">
        <div class="bookpage">
          <div>${cover}</div>
          <div>
            <h1>${escapeHtml(book.title)}</h1>
            <div class="byline">${escapeHtml(book.author || 'unknown author')}${book.year ? ' · ' + book.year : ''}</div>
            ${book.goodreads_url ? `<div class="small" style="margin-top:6px">
              <a href="${escapeHtml(book.goodreads_url)}" target="_blank" rel="noopener">View on Goodreads</a></div>` : ''}
            <dl class="kv mt">
              <dt>Category</dt>
              <dd><select onchange="setCategory(${book.id}, this.value)">${cats}</select></dd>
              <dt>Genres</dt>
              <dd>${(book.genres || []).slice(0, 8).map(escapeHtml).join(', ') || '—'}
                <span class="faint small">(${escapeHtml(book.genre_source || 'unresolved')})</span></dd>
              <dt>Auto-shelve</dt>
              <dd><label class="row tight"><input type="checkbox" style="width:auto"
                    ${book.auto_shelve ? 'checked' : ''}
                    onchange="setBookAutoShelve(${book.id}, this.checked)"> move when finished</label></dd>
            </dl>
          </div>
        </div>
      </div>
    </div>

    <div class="panel">
      <h2><span class="grow">Pipeline</span>
        <button class="tiny" onclick="retryBook(${book.id},this)">Retry failed stages</button>
      </h2>
      <div class="stages">${rows}</div>
    </div>

    ${paths}

    <div class="panel">
      <h2>Activity for this book</h2>
      <div class="body flush log">${renderEvents(events)}</div>
    </div>`;
}

/* ------------------------------------------------------------ services */
function statusChip(s) {
  if (s.ok === true) return '<span class="chip ok">healthy</span>';
  if (s.ok === false) {
    return `<span class="chip ${s.failure_kind === 'auth' ? 'auth' : 'failed'}">` +
           `${escapeHtml(KIND_LABEL[s.failure_kind] || 'down')}</span>`;
  }
  return '<span class="chip pending">unknown</span>';
}

function viewServices() {
  const health = state?.health || { services: [] };
  const cards = health.services.map(s => {
    const cls = s.ok === true ? 'ok' : s.ok === false ? (s.failure_kind === 'auth' ? 'err' : 'warn') : 'idle';
    return `<div class="svc ${s.ok === false ? 'down' : ''}" onclick="go('/service/${s.service}')">
      <div class="svc-head">
        <span class="dot ${cls}"></span>
        <span class="name">${escapeHtml(s.label)}</span>
        <span class="grow"></span>
        ${statusChip(s)}
      </div>
      <div class="meta">${escapeHtml(s.detail || 'never checked')}</div>
      ${s.ok === false ? `<div class="impact">${escapeHtml(s.impact)}</div>` : ''}
      <div class="faint small" style="margin-top:6px">
        ${s.checked ? `checked ${escapeHtml(relTime(s.checked_at))}` : 'never checked'}
        ${s.ok_since ? ` · healthy since ${escapeHtml(relTime(s.ok_since))}` : ''}
      </div>
    </div>`;
  }).join('');

  const fields = settingsFields
    ? (settingsFields.fields || []).map(f => `
        <label class="field">
          <span class="lbl">${escapeHtml(f.label)}
            ${f.is_set ? '<span class="chip ok" style="margin-left:6px">set</span>'
                       : '<span class="chip" style="margin-left:6px">not set</span>'}</span>
          <input type="password" data-key="${f.key}" data-label="${escapeHtml(f.label)}"
                 placeholder="${f.is_set ? '•••••• unchanged' : 'paste value'}">
          ${f.hint ? `<span class="hint">${escapeHtml(f.hint)}</span>` : ''}
        </label>`).join('')
    : '<div class="muted">Loading credentials…</div>';

  const g = state?.goodreads || {};

  return `
    <div class="panel">
      <h2><span class="grow">Services</span>
        <button class="tiny" onclick="recheckServices(this)">Re-check all</button>
      </h2>
      <div class="body">
        <div class="svc-grid">${cards}</div>
        <p class="small muted mt" style="margin-bottom:0">
          A service is only counted healthy if an <em>authenticated</em> call succeeds —
          a wrong API key against an open health endpoint would otherwise look fine.
          Open any service for its credentials, its log, and what it is holding up.
        </p>
      </div>
    </div>

    <div class="panel">
      <h2><span class="grow">Goodreads</span>
        <button class="tiny primary" onclick="window.location='/goodreads'">Re-authenticate</button>
      </h2>
      <div class="body">
        <dl class="kv">
          <dt>Session</dt>
          <dd>${g.has_session
            ? `<b style="color:var(--ok)">${escapeHtml(g.session_age || 'stored')}</b>`
            : '<b style="color:var(--warn)">not signed in</b>'}</dd>
          <dt>User id</dt><dd class="mono">${escapeHtml(g.user_id || 'not detected')}</dd>
        </dl>
      </div>
    </div>

    <div class="panel">
      <h2><span class="grow">Credentials</span>
        <button class="tiny primary" onclick="saveSettings(this)">Save</button>
      </h2>
      <div class="body">
        <p class="small muted" style="margin-top:0">
          Values are encrypted at rest. Leave a field blank to keep what is stored;
          type a value and save to replace it. Saving re-tests every service and tells
          you whether the new credentials work.
        </p>
        <div id="settings-body">${fields}<div id="settings-note"></div></div>
      </div>
    </div>`;
}

/* ------------------------------------------------------- service detail */
async function viewService(name) {
  const key = canonicalService(name);
  let d = serviceDetail;
  if (!d || d.service !== key) {
    try { d = await api(`/api/services/${encodeURIComponent(key)}`); serviceDetail = d; }
    catch (err) {
      return `<div class="panel"><div class="body">
        Could not load ${escapeHtml(key)}: ${escapeHtml(err.message)}</div></div>`;
    }
  }

  const h = d.health;
  const cls = !h ? 'idle' : h.ok === true ? 'ok' : (h.failure_kind === 'auth' ? 'err' : 'warn');

  const creds = d.fields.length
    ? `<div class="panel">
        <h2><span class="grow">Credentials</span>
          <button class="tiny primary" onclick="saveSettings(this)">Save</button></h2>
        <div class="body"><div id="settings-body">
          ${d.fields.map(f => `
            <label class="field">
              <span class="lbl">${escapeHtml(f.label)}
                ${f.is_set ? '<span class="chip ok" style="margin-left:6px">set</span>'
                           : '<span class="chip" style="margin-left:6px">not set</span>'}</span>
              <input type="password" data-key="${f.key}" data-label="${escapeHtml(f.label)}"
                     placeholder="${f.is_set ? '•••••• unchanged' : 'paste value'}">
              ${f.hint ? `<span class="hint">${escapeHtml(f.hint)}</span>` : ''}
            </label>`).join('')}
          <div id="settings-note"></div>
        </div></div>
      </div>`
    : '';

  // Capped: Shelfmark can hold 200+ books at once, and rendering every one of
  // them (with a 150-character reason each) made the page enormous for no
  // gain — the whole set is one click away under the matching book filter.
  const HELD_CAP = 25;
  const shown = d.held.slice(0, HELD_CAP);
  const held = d.held.length
    ? `<div class="panel">
        <h2><span class="grow">Waiting on ${escapeHtml(d.label)}</span>
          <span class="faint small">${d.held.length}${
            d.held.length > HELD_CAP ? ` · showing ${HELD_CAP}` : ''}</span></h2>
        <div class="body flush">
          ${shown.map(x => `
            <div class="held-row">
              <span class="chip ${x.status === 'failed' ? 'failed' : 'blocked'}">${escapeHtml(STAGE_LABEL[x.stage] || x.stage)}</span>
              <a href="#/book/${x.id}">${escapeHtml(x.title)}</a>
              <span class="faint small">${escapeHtml((x.detail || '').slice(0, 130))}</span>
            </div>`).join('')}
          ${d.held.length > HELD_CAP ? `<div class="held-row muted small">
            … and ${d.held.length - HELD_CAP} more on this service.</div>` : ''}
        </div>
      </div>`
    : `<div class="panel"><div class="body muted">
        Nothing is waiting on ${escapeHtml(d.label)} right now.</div></div>`;

  const stages = d.stages.length
    ? d.stages.map(s => escapeHtml(STAGE_LABEL[s] || s)).join(', ')
    : 'none';

  return `
    <div class="row" style="margin-bottom:12px">
      <button class="ghost" onclick="go('/services')">&larr; Services</button>
      <span class="grow"></span>
      ${h ? statusChip(h) : '<span class="chip pending">not probed</span>'}
    </div>

    <div class="panel">
      <div class="body">
        <div class="row" style="align-items:flex-start">
          <span class="dot ${cls}" style="margin-top:7px"></span>
          <div>
            <h1 style="font-family:var(--serif);font-size:23px;margin:0;color:var(--brown)">${escapeHtml(d.label)}</h1>
            <div class="small muted" style="margin-top:3px">
              ${h ? escapeHtml(h.detail || '') : 'never checked'}
              ${h && h.checked ? ` · checked ${escapeHtml(relTime(h.checked_at))}` : ''}
              ${h && h.ok_since ? ` · healthy since ${escapeHtml(relTime(h.ok_since))}` : ''}
            </div>
          </div>
          <span class="grow"></span>
          ${d.service !== 'settings' ? `
            <button class="primary" onclick="testService('${d.service}', this)">Test now</button>` : ''}
          ${d.login_url ? `<button onclick="window.location='${d.login_url}'">Sign in</button>` : ''}
        </div>

        <dl class="kv mt">
          <dt>Endpoint</dt>
          <dd class="mono">${escapeHtml(d.url || '—')}</dd>
          <dt>Cannot do</dt>
          <dd>${h && h.impact ? escapeHtml(h.impact) : '—'}</dd>
          <dt>Used by</dt>
          <dd>${stages}</dd>
        </dl>

        ${h && h.ok === false ? `<div class="banner err" style="margin:14px 0 0">
          <span class="dot err"></span>
          <span>${escapeHtml(d.label)} is not answering. ${escapeHtml(h.impact || '')}.</span>
        </div>` : ''}
      </div>
    </div>

    ${creds}
    ${held}

    <div class="panel">
      <h2><span class="grow">Log for ${escapeHtml(d.label)}</span>
        <span class="faint small">${d.events.length} recent${
          d.events_inferred ? ' · matched by name' : ''}</span></h2>
      ${d.events_inferred ? `<div class="body tight small muted">
        These were recovered by matching “${escapeHtml(d.label)}” in older log lines.
        New lines are recorded against the service directly.</div>` : ''}
      <div class="body flush log">${renderEvents(d.events)}</div>
    </div>`;
}

function renderEvents(events) {
  if (!events || !events.length) return '<div class="muted" style="padding:12px 15px">Nothing yet.</div>';
  return events.map(e => `<div class="ev ${escapeHtml(e.level || '')}">
    <span class="t">${escapeHtml(stamp(e.ts))}</span>
    <span>${e.service ? serviceChip(e.service) : ''}</span>
    <span class="msg">${escapeHtml(e.message || '')}</span>
  </div>`).join('');
}

/* ------------------------------------------------------------ activity */
function viewActivity() {
  const events = state?.events || [];
  return `<div class="panel">
    <h2><span class="grow">Activity</span><span class="faint small">${events.length} recent</span></h2>
    <div class="body flush log">${renderEvents(events)}</div>
  </div>`;
}

/* -------------------------------------------------------------- actions */
async function retryStage(bookId, stage, btn) {
  if (btn) btn.disabled = true;
  try {
    await api(`/api/books/${bookId}/retry/${stage}`, { method: 'POST' });
    toast('Retry queued', `${STAGE_LABEL[stage] || stage} will run on the next sweep.`, 'ok');
    await refresh();
  } catch (err) { toast('Retry failed', err.message, 'err'); }
  finally { if (btn) btn.disabled = false; }
}

async function retryBook(bookId, btn) {
  const book = (state.books || []).find(b => b.id === bookId);
  if (!book) return;
  const failed = Object.entries(book.stages || {})
    .filter(([, r]) => (r || {}).status === 'failed').map(([s]) => s);
  if (!failed.length) { toast('Nothing to retry', 'No stage of this book is failing.', ''); return; }
  if (btn) btn.disabled = true;
  try {
    for (const stage of failed) await api(`/api/books/${bookId}/retry/${stage}`, { method: 'POST' });
    toast('Retries queued', `${failed.length} stage(s): ${failed.join(', ')}`, 'ok');
    await refresh();
  } catch (err) { toast('Retry failed', err.message, 'err'); }
  finally { if (btn) btn.disabled = false; }
}

async function retryGroup(stage) {
  const targets = filteredBooks().filter(b =>
    ((b.stages || {})[stage] || {}).status === 'failed');
  if (!targets.length) { toast('Nothing to retry', `No book has ${stage} failing.`, ''); return; }
  try {
    for (const b of targets) await api(`/api/books/${b.id}/retry/${stage}`, { method: 'POST' });
    toast('Retries queued', `${targets.length} book(s) will re-run ${STAGE_LABEL[stage] || stage}.`, 'ok');
    await refresh();
  } catch (err) { toast('Retry failed', err.message, 'err'); }
}

async function setCategory(bookId, category) {
  try {
    await api(`/api/books/${bookId}/category`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ category }),
    });
    toast('Category set', `Re-filing as ${category}.`, 'ok');
    await refresh();
  } catch (err) { toast('Could not set category', err.message, 'err'); }
}

async function setBookAutoShelve(bookId, enabled) {
  try {
    await api(`/api/books/${bookId}/auto_shelve`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    });
    toast('Updated', `Auto-shelve ${enabled ? 'on' : 'off'} for this book.`, 'ok');
    await refresh();
  } catch (err) { toast('Could not update', err.message, 'err'); }
}

async function setAutoShelve(enabled) {
  try {
    await api('/api/auto_shelve', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    });
    toast('Updated', `Auto-shelve ${enabled ? 'on' : 'off'}.`, 'ok');
    await refresh();
  } catch (err) { toast('Could not update', err.message, 'err'); }
}

async function sweepNow(btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Sweeping…'; }
  try {
    const r = await api('/api/sweep', { method: 'POST' });
    toast('Sweep complete',
      `advanced ${r.advanced || 0}, parked ${r.parked || 0} of ${r.books || 0} books.`, 'ok');
    await refresh();
  } catch (err) { toast('Sweep failed', err.message, 'err'); }
  finally { if (btn) { btn.disabled = false; btn.textContent = 'Sweep now'; } }
}

async function reconcileNow(btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Checking…'; }
  try {
    const r = await api('/api/reconcile', { method: 'POST' });
    if (r.drifted) {
      toast(`${r.drifted} shelf record(s) had drifted`,
            `Re-queued for shelving. ${r.details.map(d => d.title).slice(0,2).join(', ')}`,
            'err');
    } else {
      toast('Shelf records agree with Goodreads', `${r.checked} book(s) checked.`, 'ok');
    }
    await refresh();
  } catch (err) { toast('Reconcile failed', err.message, 'err'); }
  finally { if (btn) { btn.disabled = false; btn.textContent = 'Check shelf'; } }
}

async function recheckServices(btn) {
  if (btn) btn.disabled = true;
  try {
    const r = await api('/api/health/services/check', { method: 'POST' });
    const bad = r.unhealthy || [];
    toast(bad.length ? `${bad.length} service(s) unhealthy` : 'All services healthy',
      bad.length ? bad.map(s => `${s.label}: ${s.detail}`).join(' · ').slice(0, 200) : '',
      bad.length ? 'err' : 'ok');
    await refresh();
  } catch (err) { toast('Health check failed', err.message, 'err'); }
  finally { if (btn) btn.disabled = false; }
}

async function testService(service, btn) {
  const original = btn.textContent;
  btn.disabled = true; btn.textContent = 'testing…';
  try {
    const r = await api(`/api/settings/test/${service}`, { method: 'POST' });
    toast(r.ok ? `${service}: OK` : `${service} failed`, r.detail, r.ok ? 'ok' : 'err');
    await refresh();
  } catch (err) { toast(`${service} test failed`, err.message, 'err'); }
  finally { btn.disabled = false; btn.textContent = original; }
}

async function saveSettings(btn) {
  const values = {};
  $$('#settings-body input[data-key]').forEach(input => {
    if (input.value !== '') values[input.dataset.key] = input.value;
  });
  if (!Object.keys(values).length) { toast('Nothing to save', 'All fields were left blank.', ''); return; }
  if (btn) btn.disabled = true;
  try {
    const r = await api('/api/settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ values }),
    });
    toast('Saved', `${r.saved.length} credential(s) updated. Re-checking…`, 'ok');
    const health = await api('/api/health/services/check', { method: 'POST' });
    const bad = health.unhealthy || [];
    if (bad.length) {
      toast('Still failing', bad.map(s => `${s.label}: ${s.detail}`).join(' · ').slice(0, 240), 'err');
    } else {
      toast('All services healthy', 'The new credentials work.', 'ok');
    }
    settingsFields = await api('/api/settings');
    serviceDetail = null;          // force the service page to re-read
    await refresh();
  } catch (err) { toast('Could not save', err.message, 'err'); }
  finally { if (btn) btn.disabled = false; }
}

/* --------------------------------------------------------------- render */
let renderToken = 0;

async function render() {
  const token = ++renderToken;
  const r = route();

  if (!state) {
    $('#view').innerHTML = '<div class="empty">Loading…</div>';
    return;
  }

  let html;
  if (r.name === 'books') html = viewBooks();
  else if (r.name === 'book') html = await viewBook(r.param);
  else if (r.name === 'services') html = viewServices();
  else if (r.name === 'service') html = await viewService(r.param);
  else if (r.name === 'activity') html = viewActivity();
  else html = viewDashboard();

  if (token !== renderToken) return;   // a newer render won

  renderMasthead();
  renderBanner();
  renderRail();
  $('#view').innerHTML = html;
  const headerSearch = $('#global-search');
  if (headerSearch && headerSearch.value !== searchTerm) headerSearch.value = searchTerm;
}

async function refresh() {
  try {
    const r = route();
    const wantsSettings = r.name === 'services' || r.name === 'service';
    const [s, i] = await Promise.all([
      api('/api/state'),
      api('/api/issues').catch(() => null),
    ]);
    state = s;
    issues = i;
    if (wantsSettings && !settingsFields) {
      settingsFields = await api('/api/settings').catch(() => null);
    }
    await render();
  } catch (err) {
    if (String(err.message).includes('session expired')) return;
    $('#view').innerHTML = `<div class="panel"><div class="body">
      Could not reach goodreads: ${escapeHtml(err.message)}</div></div>`;
  }
}

// Navigations go through refresh(), not render(): the Services view needs
// /api/settings, which is loaded in refresh(). Rendering straight from the
// hashchange left the credentials panel empty until the next 6-second poll.
window.addEventListener('hashchange', () => {
  // A different service means a different payload; never serve the last one.
  if (route().name !== 'service') serviceDetail = null;
  else if (serviceDetail && serviceDetail.service !== canonicalService(route().param)) serviceDetail = null;
  refresh();
});
document.addEventListener('DOMContentLoaded', () => { refresh(); setInterval(refresh, 6000); });
