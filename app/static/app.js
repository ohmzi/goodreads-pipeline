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
let versionHistory = null;     // the last /api/version/history payload
/* How many service re-checks are in flight. Module state and not a DOM node,
 * because refresh() replaces #rail and #view from scratch every 6 seconds:
 * a class put on a row, or a disabled attribute put on a button, is gone by
 * the next poll — and on a real deployment the poll lands several times inside
 * one check (six sequential authenticated probes, each with its own 30-second
 * timeout; see check_all() in app/health.py). The detached button also cannot
 * be re-enabled, so the rail's Re-check used to look live again while the
 * request it had started was still running.
 * A count rather than a boolean: the rail's "Re-check" and the services page's
 * "Re-check all" are two buttons for the same endpoint and both are on screen
 * at once on #/services, so the last one to finish is the one that lowers it. */
let healthChecks = 0;

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
    ['/genres', 'Genres', 'genres'],
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

  // While a re-check is in flight every service row says "testing…" instead of
  // the count it was showing, because the count being shown is the answer the
  // operator just clicked to get rid of — and a row of stale numbers with no
  // marker is what made a slow check read as a button that did nothing. The
  // Goodreads row below is deliberately left alone: check_all() probes the six
  // SERVICES and nothing else, so a "testing…" there would be a claim about a
  // probe that is not running.
  const checking = healthChecks > 0;
  // `.bad` is dropped while testing so no pink alert surface is left
  // underneath a row that is saying "I do not know yet"; `.current` stays,
  // because which service you are looking at is still true.
  const rows = health.services.map(s => {
    const cls = checking ? 'testing'
      : s.ok === true ? 'ok'
      : s.ok === false ? (s.failure_kind === 'auth' ? 'err' : 'warn') : 'idle';
    const note = checking ? 'testing…'
      : s.ok === false ? escapeHtml(KIND_LABEL[s.failure_kind] || 'down')
      : escapeHtml(s.detail || 'never checked');
    // The check time lives in the tooltip rather than its own column: a
    // narrow rail spent more width on "just now" repeated seven times than on
    // the detail it was squeezing to "10 li…". While testing it keeps saying
    // when the last check was, which is still true.
    const when = s.checked ? `checked ${relTime(s.checked_at)}` : 'never checked';
    // The open service is marked with a class, not the inline background it
    // used to carry: a style attribute outranks .svc-row:hover, so the current
    // row was the one row in the list that gave no hover feedback.
    return `<a class="svc-row ${checking ? 'testing' : s.ok === false ? 'bad' : ''} ${s.service === current ? 'current' : ''}"
               href="#/service/${s.service}" title="${escapeHtml(s.label)} — ${escapeHtml(when)}"
               ${s.service === current ? 'aria-current="true"' : ''}>
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
        <button class="tiny ghost" onclick="recheckServices(this)"
                ${checking ? 'disabled' : ''}>${checking ? 'testing…' : 'Re-check'}</button></h3>
      <div class="body flush">
        <div class="svc-list"${checking ? ' aria-busy="true"' : ''}>${rows}${grSocket}</div>
      </div>
    </div>

    <div class="rail-card">
      <h3>Run control</h3>
      <div class="body">
        <div class="rail-actions">
          <button class="tiny" onclick="sweepNow(this)">Sweep now</button>
          <button class="tiny" onclick="reconcileNow(this)">Check shelf</button>
        </div>
        <label class="rail-check">
          <input type="checkbox" ${state?.auto_shelve ? 'checked' : ''}
                 onchange="setAutoShelve(this.checked)">
          <span>Move finished books to a collected shelf</span>
        </label>
        ${state?.disk_free_gb != null ? `<p class="rail-note">
          <b>${state.disk_free_gb} GB</b> free where books land</p>` : ''}
      </div>
      <div class="rail-foot">
        <a class="sidebar-version" href="#/version">
          <span class="vnum">v${escapeHtml(versionHistory?.current || '1.0.0')}</span>
          What's new<span class="new-dot"></span>
        </a>
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
      <div>${g.stage === 'held'
        /* The only row that offers a way out of the hold rather than a way to
         * look at it. Every other exit is the service answering, and the whole
         * point of a stuck breaker is that nothing can prove it did. */
        ? `<button class="tiny" onclick="clearBreaker('${escapeHtml(canonicalService(g.service))}', this)">Clear hold</button>
           <a class="btn tiny" href="#/service/${encodeURIComponent(canonicalService(g.service))}">Open</a>`
        : g.service
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

/* ---------------------------------------------------------------- genres */
/* The DIRECTORY MAP: which folder on disk each category files into, which Open
 * Notebook notebook it routes to, and the books that actually landed there.
 *
 * The axis is the system CATEGORY — the closed set in categories.yml — not the
 * free-text genres scraped from Goodreads. A genre string does not put a book
 * anywhere: it is only the *input* to the classify rule (case-insensitive
 * substring, longest needle first, one category per book; app/stages/classify.py
 * resolve_category), and the category is what names the directory:
 *
 *   category -> folder   a directory under books_root. Kavita, BookLore and
 *                        Grimmory each scan it as one of their own libraries,
 *                        so one folder is three libraries, not one.
 *            -> notebook the Open Notebook notebook the book is filed into.
 *
 * Four facts the flat mapping hides, all real, all shown rather than smoothed
 * over:
 *
 *   - category -> folder is NOT injective. Fantasy and Fiction are distinct
 *     categories that share the folder /books/Fiction, so a folder-level view
 *     and a category-level view are genuinely different groupings. The page
 *     names the other categories in a folder instead of implying a one-to-one
 *     map.
 *
 *   - a category may map to NO notebook (`notebook: ""`; Manga and Natgeo do).
 *     Deliberate, and not a fault: the notebook stage returns `skipped` for
 *     these, verify never asks Open Notebook for one of their books, and
 *     placement and indexing are untouched. Said in words, never rendered as a
 *     blank cell or as an error.
 *
 *   - a book may carry a category that is not in categories.yml at all — set by
 *     hand, or left behind by a rename. destination_for() still files those,
 *     falling back to the category name itself as the folder, so they do have a
 *     destination; it is simply not one anyone configured, and they get no
 *     notebook. They get their own pill rather than vanishing off the page.
 *
 *   - a configured category holding no books is still a real destination. Every
 *     configured category is listed, at 0 where it is empty, because an empty
 *     destination is one of the things the operator is here to check.
 *
 * The counts PARTITION: one category per book, so the category pills sum to the
 * library. The old genre strip overlapped by design, because a book carries
 * several genres; this axis does not, and the toolbar note says so.
 */

const NO_CATEGORY = '__unconfigured__';   // books whose category is not in categories.yml
const REVIEW = '__review__';              // the fallback bucket — books classify flagged
const PRIMARY_GENRE_SOURCE = 'goodreads'; // anything else is a fallback

/* The selected pill: '' = All, else a category key or one of the two sentinels. */
let currentCategory = '';

/* /api/categories -> {categories: {Name: {folder, notebook}}, fallback}.
 *
 * The folder and notebook are served ONLY here, so this page fetches it — see
 * the `wantsSettings` gate in refresh(), which must include this route or the
 * two columns render blank. Null means the fetch has not landed (or failed):
 * the page then still renders every category from /api/state and says the
 * destination detail is missing, rather than dropping columns or breaking. */
let categoryMap = null;
let categoryMapError = '';

function isConfiguredCategory(name) {
  return !!name && (state?.categories || []).includes(name);
}

/* The folder a category is filed under, or null when there is nothing honest to
 * show.
 *
 * With the map in hand this mirrors classify.destination_for exactly: a category
 * with no entry in categories.yml falls back to the category name itself as its
 * folder, so the path shown is the path the pipeline would use. Without it, that
 * same fallback would invent "/books/Fantasy" for a category that actually files
 * into /books/Fiction — a wrong path is worse than a blank one, so the map's
 * absence is returned as null and each caller says so in its own words. */
function categoryFolder(name) {
  if (!categoryMap) return null;
  return (categoryMap.categories?.[name]?.folder) || name;
}

/* The notebook for a category. `''` is a real configured answer ("no notebook
 * yet"), so it is distinct from "we do not know" — which is what null is, when
 * the map never arrived or the category is not configured at all. */
function categoryNotebook(name) {
  if (!categoryMap) return null;
  const entry = (categoryMap.categories || {})[name];
  if (!entry) return null;
  return entry.notebook || '';
}

/* A category with `notebook: ""` is a real state, not a gap, and the sentence is
   the whole point of the column: the notebook stage skips these books, which
   costs them nothing, because placement and the three indexers never consult
   the notebook. */
const NO_NOTEBOOK = 'no notebook — the notebook stage skips these books';

/* The books under one pill. `key` may be a category, a sentinel, or '' (All). */
function booksOfCategory(key) {
  const books = state?.books || [];
  if (key === NO_CATEGORY) return books.filter(b => !isConfiguredCategory(b.category));
  if (key === REVIEW) return books.filter(b => b.needs_review);
  if (!key) return books;
  return books.filter(b => (b.category || '') === key);
}

/* Every pill's count in one pass, off the same book list the pills filter —
   nothing is counted from a second source, so the two cannot disagree. */
function categoryCounts() {
  const counts = new Map();
  let review = 0;
  let off = 0;
  (state?.books || []).forEach(book => {
    const key = book.category || '';
    if (isConfiguredCategory(key)) counts.set(key, (counts.get(key) || 0) + 1);
    else off += 1;
    if (book.needs_review) review += 1;
  });
  return { counts, review, off };
}

/* The scraped genre strings the books in view arrived with, most common first.
 *
 * Provenance, not an axis. These are the strings the categories.yml rules were
 * matched against, so they are what explains why a given book sits where it
 * does — shown beside the destination they produced, never as a filter bar. A
 * book carries several, so unlike the pills these counts overlap. */
function genreBreakdown(books) {
  const counts = new Map();
  books.forEach(book => (book.genres || []).filter(Boolean)
    .forEach(g => counts.set(g, (counts.get(g) || 0) + 1)));
  return Array.from(counts.entries())
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

/* Where the genres for these books came from.
 *
 * Only the books that HAVE a genre are tallied: a book with none has no source
 * to name, and counting it here as "unresolved" would have reported the
 * no-genre bucket as a pile of fallbacks. The source is named rather than
 * labelled "authoritative"/"fallback" — "openlibrary" is the fact, and a label
 * would be a guess about why — but the fallbacks take the alert surface so they
 * read as a different thing at a glance, and the note says what that means.
 *
 * Returns nothing at all when not one book in view has a genre: there is no
 * source to report, and the page says so in its own line. */
function genreSources(books) {
  const withGenres = books.filter(b => (b.genres || []).filter(Boolean).length);
  if (!withGenres.length) return '';

  const counts = new Map();
  withGenres.forEach(b => {
    const src = b.genre_source || 'unresolved';
    counts.set(src, (counts.get(src) || 0) + 1);
  });
  const parts = Array.from(counts.entries())
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([src, n]) => `<span class="chip ${src === PRIMARY_GENRE_SOURCE ? '' : 'blocked'}">
      ${escapeHtml(src)} <span class="faint">${n}</span></span>`).join('');

  const notes = [];
  if (withGenres.length !== books.length) {
    notes.push(`${books.length - withGenres.length} of these have no genre from any source.`);
  }
  if (Array.from(counts.keys()).some(s => s !== PRIMARY_GENRE_SOURCE)) {
    notes.push('A genre from any source but goodreads is a fallback — that book’s own page carried none.');
  }
  return gline('Genres from', `${parts}
    ${notes.length ? `<span class="src-note">${notes.join(' ')}</span>` : ''}`);
}

/* One label/value line of the meta band, on the .kv grid's inset. */
function gline(label, body) {
  return `<div class="gline"><span class="lbl">${escapeHtml(label)}</span>${body}</div>`;
}

/* A sentence of explanation, spanning the band.
 *
 * Deliberately not a .gline: a label column only works when the value is short
 * enough to sit beside it, and these notes are prose. A label with a paragraph
 * under it leaves the label stranded on a line of its own, which reads as a
 * broken row. --muted, not italic: .src-note's italic is for a short aside
 * against a value ("no notebook"), and a paragraph of it is hard work. */
function bandNote(html) { return `<div class="band-note">${html}</div>`; }

function chipCount(text, n) {
  return `<span class="chip">${escapeHtml(text)} <span class="faint">${n}</span></span>`;
}

/* The scraped genres that put books here, capped so a big category does not
   turn the band into a wall. The cap is stated rather than silent, and the
   remainder is a number the operator can go and find on the books' own pages. */
function genresSeenLine(books) {
  const rows = genreBreakdown(books);
  if (!rows.length) return '';
  const CAP = 8;
  const shown = rows.slice(0, CAP);
  const rest = rows.length - shown.length;
  return gline('Genres seen', shown.map(([g, n]) => chipCount(g, n)).join('') +
    (rest ? `<span class="src-note">and ${rest} more, each counted once per book.</span>` : ''));
}

/* The destination half of the page: what the pipeline does with this category.
 * Rendered as the same label/value lines the book page's .kv uses, because it
 * is the same kind of fact — a name and its value. */
function destinationLines(name, books) {
  // The map never arrived. Say that and keep the books: the categories come
  // from /api/state and are still correct, only the two columns are missing.
  if (!categoryMap) {
    return gline('Destination', `<span class="src-note">folder and notebook
      unavailable — /api/categories did not load${categoryMapError
        ? ` (${escapeHtml(categoryMapError)})` : ''}. The categories themselves
      come from /api/state and are unaffected.</span>`);
  }

  const root = state?.paths?.books_root || '';
  const folder = categoryFolder(name);
  const notebook = categoryNotebook(name);
  const flagged = books.filter(b => b.needs_review).length;

  // Two categories can share one folder — Fantasy and Fiction both write to
  // /books/Fiction. Named, because it is the reason a folder count and a
  // category count are different numbers.
  const shared = Object.entries(categoryMap.categories || {})
    .filter(([other, entry]) => other !== name && (entry.folder || other) === folder)
    .map(([other]) => other);

  const out = [
    gline('Folder', `<span class="mono">${escapeHtml(root)}/${escapeHtml(folder)}</span>`),
    // notebook === null cannot happen for a configured category — both
    // endpoints read the same categories.yml — but a null reaching the template
    // would render as the literal text "null", so it is spelled out anyway.
    gline('Notebook', notebook === null
      ? '<span class="src-note">not a category in categories.yml</span>'
      : (notebook
          ? escapeHtml(notebook)
          : `<span class="src-note">${NO_NOTEBOOK}</span>`)),
    gline('Books', `${books.length} filed${flagged
      ? ` · <span class="flag-note">${flagged} flagged for review</span>` : ''}`),
  ];
  if (shared.length) {
    out.push(bandNote(`${shared.map(escapeHtml).join(', ')}
      ${shared.length === 1 ? 'also files' : 'also file'} into this folder, so
      ${escapeHtml(root)}/${escapeHtml(folder)} holds more books than this
      category does. A Kavita, BookLore or Grimmory library pointed at that
      folder sees all of them — which is why a folder count and a category count
      are different numbers.`));
  }
  if (categoryMap.fallback === name) {
    out.push(bandNote(`This is the fallback category: a book whose genres match no
      rule is filed here and flagged for review, so the flag — not the folder — is
      what separates it from a book that matched a rule.`));
  }
  out.push(genresSeenLine(books));
  out.push(genreSources(books));
  return out.filter(Boolean).join('');
}

/* The books whose category is not in categories.yml.
 *
 * destination_for() falls back to the category name as the folder, so these do
 * have a path on disk — it is just one no one configured, with no notebook and
 * no rule that can reach it. Grouped by value, because each distinct value is
 * its own folder. */
function unconfiguredLines(books) {
  const root = state?.paths?.books_root || '';
  const counts = new Map();
  books.forEach(b => {
    const key = b.category || '';
    counts.set(key, (counts.get(key) || 0) + 1);
  });
  const rows = Array.from(counts.entries())
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([key, n]) => gline(key || 'no category',
      `<span class="mono">${escapeHtml(root)}/${escapeHtml(key || '')}</span>
       <span class="src-note">no notebook — not in categories.yml</span>
       <span class="faint">${n}</span>`))
    .join('');
  return `${rows}
    ${bandNote(`categories.yml has no entry for these values, so the folder is the
      category name itself and there is no notebook. Adding the value under
      <span class="mono">categories:</span> gives it a folder and a notebook of
      your choosing; a library in Kavita, BookLore or Grimmory must contain that
      folder before anything is indexed into it.`)}
    ${genresSeenLine(books)}`;
}

/* The books the classify run could not place by rule. Their category IS
 * configured — the fallback — so they are somewhere real; the flag is the only
 * thing marking them, which is exactly why the page keeps them reachable. */
function reviewLines(books) {
  const fallback = categoryMap?.fallback || '';
  return `${bandNote(`No genre matched a rule, so classify filed each of these on
    the fallback${fallback ? ` category (<b>${escapeHtml(fallback)}</b>)` : ''} and
    flagged it. They are placed and indexed like any other book — the flag is a
    note to you, not a fault. Clear it by setting the category by hand on the
    book, or by adding a rule to categories.yml.`)}
    ${genresSeenLine(books)}
    ${genreSources(books)}`;
}

/* The pill strip. `.filters` is goodreads' own tab component, reused verbatim —
   the page does not own a second filter widget. The key travels in a data
   attribute: a category name is operator-written text and escapeHtml's &#39;
   decodes back to a quote before the inline JS is parsed. */
function categoryTab(key, label, count, active) {
  return `<button class="${active === key ? 'on' : ''}" data-cat="${escapeHtml(key)}"
      ${active === key ? 'aria-current="true"' : ''}
      onclick="setCategoryTab(this.dataset.cat)">
      ${escapeHtml(label)} <span class="faint">${count}</span>
    </button>`;
}

function setCategoryTab(key) { currentCategory = key; render(); }

/* Every configured category as a row of the map: the folder it writes to, the
 * notebook it routes to, and how many books sit there. Empty categories stay in
 * the table at 0 — they are destinations, and "is anything in Islamic yet" is a
 * question the table should answer without a click. */
function overviewPanel(counts) {
  const configured = state?.categories || [];
  const root = state?.paths?.books_root || '';
  const fallback = categoryMap?.fallback || '';

  const rows = configured.map(name => {
    const notebook = categoryNotebook(name);
    const folder = categoryFolder(name);   // null when /api/categories did not load
    // `data-k` is the cell's own column name. Below 780px the grid collapses to
    // one column and the head row is dropped, so each cell has to carry its own
    // label or "Fictional" alone would not say which column it came from.
    return `<div class="cat-row">
      <span data-k="Category"><button class="shelfLink" data-cat="${escapeHtml(name)}"
        onclick="setCategoryTab(this.dataset.cat)">${escapeHtml(name)}</button>${
        name === fallback ? '<span class="cat-tag">fallback</span>' : ''}</span>
      <span class="mono" data-k="Folder">${folder === null
        ? '<span class="src-note">unknown</span>'
        : escapeHtml(root) + '/' + escapeHtml(folder)}</span>
      <span data-k="Notebook">${notebook
        ? escapeHtml(notebook)
        : `<span class="src-note">${categoryMap ? NO_NOTEBOOK
            : 'not loaded'}</span>`}</span>
      <span class="faint" data-k="Books">${counts.get(name) || 0}</span>
    </div>`;
  }).join('');

  return `<div class="panel">
    <h2><span class="grow">Where each category files</span>
      <span class="faint small">${configured.length} in categories.yml</span></h2>
    <div class="body flush">
      ${categoryMap ? '' : `<div class="band-note cat-note">The folder and notebook
        columns are unknown: /api/categories did not load${
          categoryMapError ? ` (${escapeHtml(categoryMapError)})` : ''}. The
        categories themselves come from /api/state and are correct.</div>`}
      <div class="cat-table">
        <div class="cat-row head">
          <span>Category</span><span>Folder on disk</span><span>Notebook</span><span>Books</span>
        </div>
        ${rows}
      </div>
    </div>
  </div>`;
}

function emptyFor(selected) {
  if (selected === REVIEW) {
    return 'Nothing is flagged for review — every book matched a genre rule.';
  }
  if (selected === NO_CATEGORY) {
    return 'Every book carries a category from categories.yml.';
  }
  if (selected) {
    return `No book is filed under “${escapeHtml(selected)}” yet. It is a
      configured destination and simply empty — the folder is created when the
      first book lands in it.`;
  }
  return 'No book has been discovered yet.';
}

function viewGenres() {
  const books = state?.books || [];
  const configured = state?.categories || [];
  const { counts, review, off } = categoryCounts();
  const total = books.length;
  const root = state?.paths?.books_root || '';

  // No categories at all is a fact about categories.yml, not a broken page:
  // with an empty categories table there is no folder to file anything into and
  // no area of the site that can classify. Said plainly, with what to do.
  if (!configured.length) {
    return `<div class="panel">
      <h2><span class="grow">Where books are filed</span></h2>
      <div class="body"><div class="empty">
        categories.yml defines no categories, so there is no folder for the
        pipeline to file a book into and no notebook to route it to. Add one and
        the next classify run will use it.
      </div></div>
    </div>`;
  }

  // A selection that no longer exists — the taxonomy changed under a click, or
  // a stale module variable — drops back to All rather than rendering an empty
  // page under a dead pill.
  const known = !currentCategory
    || currentCategory === REVIEW
    || currentCategory === NO_CATEGORY
    || configured.includes(currentCategory);
  const selected = known ? currentCategory : '';

  const shown = booksOfCategory(selected);
  const heading = selected === REVIEW ? 'Flagged for review'
    : selected === NO_CATEGORY ? 'Not in categories.yml'
      : selected || 'All books';

  const meta = selected === REVIEW ? reviewLines(shown)
    : selected === NO_CATEGORY ? unconfiguredLines(shown)
      : selected ? destinationLines(selected, shown)
        : `${bandNote(`Every category in categories.yml, the folder it files into
            under <span class="mono">${escapeHtml(root)}</span>, and the Open
            Notebook notebook it routes to. Kavita, BookLore and Grimmory each
            scan the same folders, so one folder is three libraries. A book's
            scraped genres only decide which category it gets — open a book to
            see the genres it arrived with.`)}
          ${genresSeenLine(shown)}
          ${genreSources(shown)}`;

  return `
    <div class="toolbar">
      <div class="filters">
        ${categoryTab('', 'All', total, selected)}
        ${configured.map(name => categoryTab(name, name, counts.get(name) || 0, selected)).join('')}
        ${review ? categoryTab(REVIEW, 'Needs review', review, selected) : ''}
        ${off ? categoryTab(NO_CATEGORY, 'Unconfigured', off, selected) : ''}
      </div>
      <span class="grow"></span>
      <span class="faint small toolbar-note">one category per book, so the category
        pills add up to ${total}. “Needs review” is a flag on top of a category, not
        a category of its own.</span>
    </div>

    ${selected ? '' : overviewPanel(counts)}

    <div class="panel">
      <h2><span class="grow">${escapeHtml(heading)}</span>
        <span class="faint small">${shown.length} book${shown.length === 1 ? '' : 's'}</span></h2>
      <div class="body tight panel-meta">${meta}</div>
      ${shown.length
        ? `<div class="book-list">${shown.map(bookRow).join('')}</div>`
        : `<div class="empty">${emptyFor(selected)}</div>`}
    </div>`;
}

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
  // The same in-flight state the rail renders, from the same counter — the two
  // are on screen together on this route, so a card and a rail row disagreeing
  // about whether a check is running would be the bug, not the fix.
  const checking = healthChecks > 0;
  const cards = health.services.map(s => {
    const cls = checking ? 'testing'
      : s.ok === true ? 'ok'
      : s.ok === false ? (s.failure_kind === 'auth' ? 'err' : 'warn') : 'idle';
    return `<div class="svc ${checking ? 'testing' : s.ok === false ? 'down' : ''}" onclick="go('/service/${s.service}')">
      <div class="svc-head">
        <span class="dot ${cls}"></span>
        <span class="name">${escapeHtml(s.label)}</span>
        <span class="grow"></span>
        ${checking ? '<span class="chip testing">testing…</span>' : statusChip(s)}
      </div>
      <div class="meta">${checking ? 'testing…' : escapeHtml(s.detail || 'never checked')}</div>
      ${!checking && s.ok === false ? `<div class="impact">${escapeHtml(s.impact)}</div>` : ''}
      <div class="faint small" style="margin-top:6px">
        ${s.checked ? `checked ${escapeHtml(relTime(s.checked_at))}` : 'never checked'}
        ${!checking && s.ok_since ? ` · healthy since ${escapeHtml(relTime(s.ok_since))}` : ''}
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
        <button class="tiny" onclick="recheckServices(this)"
                ${checking ? 'disabled' : ''}>${checking ? 'testing…' : 'Re-check all'}</button>
      </h2>
      <div class="body">
        <div class="svc-grid"${checking ? ' aria-busy="true"' : ''}>${cards}</div>
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
          ${d.breaker && d.breaker.state !== 'closed' ? `
            <button class="tiny" onclick="clearBreaker('${d.service}', this)">Clear hold</button>` : ''}
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

/* ------------------------------------------------------------- version */
async function viewVersion() {
  let d = versionHistory;
  if (!d) {
    try { d = await api('/api/version/history'); versionHistory = d; }
    catch (err) {
      return `<div class="panel"><div class="body">
        Could not load version history: ${escapeHtml(err.message)}</div></div>`;
    }
  }

  const entries = (d.releases || []).map(rel => `
    <div class="version-entry">
      <div class="vhead">
        <span class="vnum">v${escapeHtml(rel.version)}</span>
        <span class="vdate">${escapeHtml(rel.date || '')}</span>
        ${rel.version === d.current ? '<span class="vtag">current</span>' : ''}
      </div>
      ${rel.summary ? `<div class="small muted">${escapeHtml(rel.summary)}</div>` : ''}
      ${(rel.sections || []).map(sec => `
        <div class="small" style="margin-top:8px"><b>${escapeHtml(sec.heading)}</b></div>
        <ul class="vchanges">${(sec.items || []).map(i => `<li>${escapeHtml(i)}</li>`).join('')}</ul>
      `).join('')}
    </div>`).join('');

  return `
    <div class="panel">
      <h2><span class="grow">Version history</span>
        <span class="chip ok">v${escapeHtml(d.current || '?')}</span></h2>
      <div class="body">
        <div class="version-page">${entries || '<div class="muted">No releases recorded.</div>'}</div>
      </div>
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

/* Release a service's breaker by hand — the panel's way out of a hold.
 *
 * It is here because every other exit from a hold is the service answering,
 * and a breaker wedged with no evidence to answer *with* has no other exit at
 * all: no cooldown that closes it, no restart that clears it (the state is a
 * row in the database), and nothing in the panel that could. The failure it
 * rescues you from is silent, so the hatch is on the two rows that show the
 * hold (the dashboard's, the service page's) rather than buried in settings.
 *
 * Confirmed, because this releases every book the service is holding at once
 * and the button cannot know whether the service is really back: the dialog
 * says so in as many words, and the toast afterwards repeats what the breaker
 * did rather than congratulating anyone on a recovery.
 */
async function clearBreaker(service, btn) {
  const label = labelForService(service);
  const ok = window.confirm(
    `Clear ${label}'s breaker?\n\n` +
    `This releases every book it is holding. Nothing has proved ${label} is ` +
    `answering — if it is still down, the next sweep will hold everything again.`
  );
  if (!ok) return;
  if (btn) btn.disabled = true;
  try {
    const r = await api(`/api/services/${encodeURIComponent(service)}/breaker/clear`,
                        { method: 'POST' });
    toast(r.cleared ? `Cleared ${label}'s hold` : `Nothing to clear`,
          r.message || '', r.cleared ? 'ok' : '');
    await refresh();
  } catch (err) { toast('Could not clear it', err.message, 'err'); }
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

/* Re-check every service. The endpoint runs check_all(): six sequential
 * authenticated probes, each with its own 30-second timeout (app/health.py,
 * app/clients/base.py), so on a stack with a host that is routable but not
 * answering this is the longest wait in the app — seconds to minutes. For the
 * whole of it the rail and the services page say "testing…".
 *
 * The state is `healthChecks`, not this button: the 6-second poll replaces
 * #rail's markup, so `btn` is detached by the time the request returns and the
 * finally below cannot use it to put anything back. A count rather than a flag
 * so two overlapping presses (the rail's button and the page's are on screen
 * together on #/services) cannot leave one of them holding the state open.
 */
async function recheckServices(btn) {
  healthChecks++;
  // Instant feedback on the button that was pressed, before the repaint. This
  // node may not survive to be un-set, which is fine — the repaint is what
  // puts the label and the disabled attribute back, from the counter.
  if (btn) { btn.disabled = true; btn.textContent = 'testing…'; }
  // renderRail() is called directly as well as through render(): on the book,
  // service and version routes render() awaits a fetch before it reaches the
  // rail, and the rail is the thing that was just pressed.
  renderRail();
  render().catch(() => {});
  try {
    const r = await api('/api/health/services/check', { method: 'POST' });
    const bad = r.unhealthy || [];
    toast(bad.length ? `${bad.length} service(s) unhealthy` : 'All services healthy',
      bad.length ? bad.map(s => `${s.label}: ${s.detail}`).join(' · ').slice(0, 200) : '',
      bad.length ? 'err' : 'ok');
  } catch (err) { toast('Health check failed', err.message, 'err'); }
  finally {
    healthChecks--;
    // The real values come back on every path. refresh() re-reads /api/state,
    // which is where check_all() wrote what it just found — so what is shown
    // after this is the server's answer, not the pre-click numbers.
    try { await refresh(); } catch (err) { render().catch(() => {}); }
    // ...and the rail is repainted unconditionally afterwards, not left to
    // refresh() to do. refresh() has a catch of its own: when /api/state is the
    // thing that is failing it writes the failure into #view and never reaches
    // render() at all, so the rail kept the testing marker until a poll finally
    // succeeded. A state that resolves only when the outage ends is stuck.
    finally { renderRail(); }
  }
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
  else if (r.name === 'genres') html = viewGenres();
  else if (r.name === 'book') html = await viewBook(r.param);
  else if (r.name === 'services') html = viewServices();
  else if (r.name === 'service') html = await viewService(r.param);
  else if (r.name === 'activity') html = viewActivity();
  else if (r.name === 'version') html = await viewVersion();
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
    // The Genres page needs the one payload /api/state does not carry: the
    // folder and notebook each category maps to, which exist only in
    // /api/categories. Without this gate the two columns render blank — and a
    // blank cell is indistinguishable from "no notebook configured", which is
    // a real state this page has to tell apart.
    const wantsCategories = r.name === 'genres';
    const [s, i] = await Promise.all([
      api('/api/state'),
      api('/api/issues').catch(() => null),
    ]);
    state = s;
    issues = i;
    if (wantsSettings && !settingsFields) {
      settingsFields = await api('/api/settings').catch(() => null);
    }
    // Retried on every visit while it is missing, so a failure is self-healing
    // rather than something the operator has to reload for.
    if (wantsCategories && !categoryMap) {
      try {
        const payload = await api('/api/categories');
        // Take it only if it is actually the shape the page renders from.
        // `api()` hands back `{raw: text}` for a 200 that is not JSON and
        // `null` for an empty one, and either would otherwise be stored as
        // authoritative: `categoryFolder()` falls back to the category name,
        // so the page would print a *guessed* directory as though it were the
        // configured one. On a page whose entire job is telling the operator
        // where their books go, a wrong path is worse than an absent one —
        // they would audit their libraries against it and find nothing wrong.
        const shaped = payload && typeof payload === 'object'
          && payload.categories && typeof payload.categories === 'object'
          && !Array.isArray(payload.categories);
        if (shaped) {
          categoryMap = payload;
          categoryMapError = '';
        } else {
          categoryMap = null;
          categoryMapError = 'the response carried no category map';
        }
      } catch (err) {
        categoryMap = null;
        categoryMapError = err.message;
      }
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
