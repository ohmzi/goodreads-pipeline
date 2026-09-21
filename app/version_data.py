"""Version history data, transcribed from docs/VERSION.md.

Kept in sync by hand with the "## vX.Y" sections and the "Version history"
table in docs/VERSION.md. Not read from that file at runtime.
"""

from __future__ import annotations

VERSION_HISTORY: list[dict] = [
    {
        "version": "1.2",
        "date": "2026-09-21",
        "summary": (
            "A native-app-style mobile header with an animated search, a "
            "cohesive desktop header, and a stolen-focus fix."
        ),
        "sections": [
            {
                "heading": "Mobile header",
                "items": [
                    "The service status/run control rail is dashboard-only now; Run control's own controls (sweep now, check shelf, auto-shelve) also appear inline on the Activity page, above its log.",
                    "The header at phone width follows their own native app rather than their desktop navbar: a bare search icon on the left, the wordmark truly centred, and a status/account control on the right standing in for their avatar. Nav tabs share the row exactly five ways instead of scrolling sideways to fit.",
                    "Tapping the search icon slides the input open left to right as a real animated overlay, instead of an instant, jarring swap that also used to nudge the nav row underneath it.",
                    "The account dropdown (service status, sign out) closes on an outside tap, Escape, or navigating via a link inside it.",
                ],
            },
            {
                "heading": "Desktop header",
                "items": [
                    "The search box, the health pill and Sign out no longer read as three different controls. All three now share one visual language with the nav tabs: the search box is a full pill matching the mobile one exactly, and the health pill and Sign out take the nav links' own geometry and hover fill instead of a separate cream chip and a bare teal link. Sign out keeps its own colour — the app's error red, not the nav's brown ink — since it ends the session rather than taking you anywhere.",
                ],
            },
            {
                "heading": "Fixes",
                "items": [
                    "A search keystroke on any page but My Books used to steal keyboard focus and drop it on the page title, ending the keystroke — typing more than one character before My Books loaded was not possible. Fixed.",
                    "The category and status filter tabs (My Books, Genres) are bordered pills now instead of goodreads.com's own flat underlined tabs, which stopped being legible once a dynamic category list ran past the two or three tabs the real site ever draws there.",
                    "A book row's stage chips no longer sit flush against the detail message below them with no gap.",
                    "The app-wide footer dropped its \"Pages\" column, which only duplicated the always-visible header nav one click further away.",
                    "Pinch and double-tap zoom are disabled everywhere, so the mobile layout cannot be pinched out of the proportions it was built for.",
                    "The dashboard's version pill is its own small card now, separate from Run control, since it is metadata about the running instance and not a control.",
                ],
            },
        ],
    },
    {
        "version": "1.1",
        "date": "2026-09-21",
        "summary": (
            "Goodreads-faithful redesign, a service breaker for outages, "
            "and a fiction-classification fix."
        ),
        "sections": [
            {
                "heading": "Reliability",
                "items": [
                    "A per-service circuit breaker: three consecutive transient failures against one service holds every book waiting on it, instead of each one failing, waiting out its own clock and parking independently. A restart or a resolved outage is caught by a half-open probe on one book; a stuck hold can be released by hand from the service's page.",
                    "The issues panel folds every book a held service is blocking into one row naming the service, with a fix message that escalates once the outage outlives the 24-hour transient grace instead of saying \"nothing to do\" forever.",
                    "A 408/425/429 from a source is now its own failure kind (busy) instead of falling through unclassified, and a Retry-After header is parsed and honoured.",
                ],
            },
            {
                "heading": "Classification",
                "items": [
                    "A precedence rule in categories.yml: any genre that names fiction resolves to Fiction ahead of the longest-needle scan, so a book tagged both \"Historical Fiction\" and \"Fiction\" no longer files under History on the strength of the longer word.",
                    "The new reclassify CLI command re-runs classification over the whole library against the current rules and reports (or, with --apply, makes) the moves needed to match.",
                ],
            },
            {
                "heading": "Sign-in",
                "items": [
                    "The sign-in page is rebuilt against the real goodreads.com sign-in page, measured rather than approximated: a white page, a narrow centred column, no card, no cream — matching its actual geometry down to the wordmark, heading and input sizes.",
                    "A working \"Keep me signed in.\" checkbox: leaving it ticked keeps the existing 7-day session, unticking it issues a 12-hour one instead.",
                    "The browser tab icon is goodreads.com's own, served from a real /favicon.ico route instead of an inline placeholder.",
                ],
            },
            {
                "heading": "Interface",
                "items": [
                    "Page chrome follows goodreads.com's classic system: a white page, cream #f4f1ea callouts, a #faf8f6 header and footer, 1px #d8d8d8 borders, and a lowercase Merriweather \"goodreads\" wordmark.",
                    "A footer on every page with the section links, the project links and the running version, in place of the page just ending.",
                    "Book rows read like shelf rows (2:3 cover, serif title, Lato byline, eight stage dots with a count) and the book page puts the cover, retry action and Goodreads link in a left column beside a serif title and a genre link row.",
                    "Six-second polling no longer wipes what you typed into a credential field or drops keyboard focus: identical renders are skipped and typed values and focus are restored.",
                ],
            },
            {
                "heading": "Version page",
                "items": [
                    "#/version lists every release with its date and a one-line summary, changes grouped by area, the running build tagged current.",
                    "The right rail shows the running version with a \"What's new\" dot that clears once the page has been opened in this browser.",
                    "/api/state carries version and FastAPI reads the version from app/__init__.py.",
                ],
            },
            {
                "heading": "Accessibility and polish",
                "items": [
                    "Every link, button, input, row and tile has a visible 2px teal keyboard focus ring; service cards are real links; pages set their own tab title and move focus to the page title after navigation.",
                    "Toasts are announced to screen readers, can be dismissed, pause on hover, and error toasts stay until closed.",
                    "The Goodreads browser page shows an idle state with the three steps and the start button in place of a black frame.",
                ],
            },
        ],
    },
    {
        "version": "1.0",
        "date": "2026-09-20",
        "summary": (
            "Initial release. Goodreads to-read shelf to a shelved, indexed "
            "library, one file per book, no copies."
        ),
        "sections": [
            {
                "heading": "Shelf tracking",
                "items": [
                    "Scrapes the Goodreads to-read shelf with a captured browser session; books are keyed on the Goodreads id, so a re-run refreshes metadata instead of creating duplicates.",
                    "Fetches genres only for books not seen before, so a large shelf does not turn into hundreds of lookups per sweep.",
                    "Paginates a shelf read up to five pages, and the reconciliation pass up to eight, so a shelf that has outgrown one page is still read whole.",
                    "Reconciles recorded shelves against Goodreads every 6 hours and resets the shelve stage for anything that drifted.",
                    "Moves a finished book off the exclusive to-read shelf using the shelve / confirm / destroy / re-shelve / confirm sequence Goodreads requires, because both removal routes destroy the whole review record.",
                    "Creates a missing shelf before writing to it; add_to_shelf silently does nothing if the shelf does not exist.",
                    "Confirms each shelf write from the endpoint's own response rather than re-reading the shelf page, which stops being reliable once a shelf outgrows one page.",
                    "Detects the Goodreads user id from the stored session when it is not configured, with a 10-minute cooldown on detection attempts.",
                ],
            },
            {
                "heading": "Acquisition",
                "items": [
                    "Each acquire stage is a two-phase machine: search and rank, then hand the best release to Shelfmark and watch the task to completion.",
                    "Releases are rejected on the indexer's Newznab category, on size, and on video-release name patterns before ranking, so a film cannot be queued as an audiobook.",
                    "Records which release ids have been tried and falls through to the next-best candidate, up to four per book.",
                    "A queued task that is absent from Shelfmark's status listing for 10 minutes (QUEUE_ABANDON_SECONDS) is treated as lost, since the queue lives in memory and a restart drops it, and is re-queued.",
                    "Refuses to start an audiobook download below the configured free-space floor.",
                    "Treats an upstream 5xx during search as a passing outage and forgives it on a 24-hour clock measured from when the outage started.",
                ],
            },
            {
                "heading": "Classification",
                "items": [
                    "Genre lookup walks four providers in order — Goodreads AppSync, OpenLibrary, Google Books, embedded epub metadata — and stops at the first that answers.",
                    "Google Books is skipped entirely unless an API key is set, because the unkeyed endpoint rate-limits immediately.",
                    "The embedded source reads dc:subject straight out of the epub, so classification still works with every API down.",
                    "Each provider is isolated: one raising or timing out cannot stop the next.",
                    "A book resolves to exactly one category: genres are scanned in the source's own relevance order and the longest matching rule wins.",
                    "With no genres from any source, the title is matched against the same rules and stored with genre_source = title.",
                    "genre_source is recorded per book (goodreads / openlibrary / googlebooks / embedded / title) and shown in the UI.",
                    "A book matching no rule falls back and is flagged needs review rather than being quietly misfiled.",
                    "app/categories.yml ships in the image; /data/categories.yml overrides it so tuning survives a rebuild.",
                ],
            },
            {
                "heading": "Placement",
                "items": [
                    "Ebooks are renamed out of staging into <Category>/Author - Title (Year)/; audiobooks are moved into Author/Title/.",
                    "Every placement is os.rename. A move that would cross a filesystem raises rather than falling back to a copy, so a duplicate is impossible.",
                    "A destination that already exists is never clobbered. The incoming file is dropped only when os.path.samefile says it is the same inode as the one already there; everything else gets a numbered suffix, including a byte-identical copy sitting at a different path.",
                    "Placement succeeds as soon as one format is in place, so a missing audiobook cannot stall the ebook's whole chain.",
                    "Re-running placement is a no-op: an already-filed ebook is found in its category folder before staging is consulted.",
                    "Re-classifies against the embedded epub metadata once the file exists and re-files it if the category improved.",
                    "Only entries sitting directly in a staging root are moved. Anything already nested under an author folder is treated as organised and left alone.",
                    "When several entries match a book, only the best-scoring one is moved and the others are left where they are.",
                ],
            },
            {
                "heading": "Indexing",
                "items": [
                    "Rescans Kavita, BookLore and Grimmory for the books tree and Audiobookshelf for the audiobooks tree. Nothing is imported or uploaded.",
                    "An app whose tree did not change is skipped rather than failed, so an ebook-only book does not spend its retries on Audiobookshelf.",
                    "Partial failure is reported by name in the stage detail but does not block the rest of the chain.",
                    "Verification searches each service for the title before anything irreversible happens on Goodreads.",
                    "A miss within 10 minutes of a rescan blocks rather than fails, because rescans are asynchronous.",
                    "A book still missing past the grace window forces another rescan, up to 3 times, before it is handed to a human.",
                    "Titles are matched on a normalised, word-boundary form with the leading article ignored, so a service holding \"Decline in Prophets\" matches Goodreads' \"A Decline in Prophets (Rowland Sinclair #2)\".",
                    "A stated series number that differs is disqualifying, so two books in one series are never taken for each other.",
                ],
            },
            {
                "heading": "Integrations",
                "items": [
                    "Six services are probed on a 5-minute timer. Five get an authenticated call — listing libraries or notebooks — rather than a bare health endpoint that would pass with a wrong key; Shelfmark runs with auth off on this instance, so its probe is the unauthenticated GET /api/health.",
                    "Each service has its own page: health, its credentials, everything currently stuck on it, and its own log.",
                    "Open Notebook is handed a file_path, never a multipart upload, so no second copy of the epub is stored.",
                    "Notebook attachment is verified by reading the source back; a source attached more than once is collapsed.",
                    "Formats Open Notebook cannot read, and .epub files that are really a zipped folder, are skipped with the reason instead of retried to death.",
                    "Paths are translated into each service's own mount namespace before they are sent, because each app sees the same files at a different path.",
                    "Credentials are entered through the UI and stored Fernet-encrypted under GOODREADS_SECRET_KEY; the key itself lives only in .env.",
                    "A credential that was never set is reported as an auth failure pointing at Settings, not as a network outage.",
                ],
            },
            {
                "heading": "Library hygiene",
                "items": [
                    "audit reports duplicate audiobooks and titles present in more than one place. Read-only. Audiobooks are grouped by size, then by a SHA-256 of the first and last 1 MiB: a cheap head+tail digest, so files matching at both ends but differing in the middle are reported as duplicates.",
                    "AppleDouble ._ stubs are ignored throughout, so they do not read as thousands of duplicates.",
                    "rename proposes Author/Title moves using embedded tags and filename parsing together, printing which rule fired, and changes nothing without --apply.",
                    "Audiobook artist tags are frequently the narrator, so tags are trusted only when the filename's leading segment matches the tag's title.",
                    "repair fixes Open Notebook link gaps and resets stuck stages, dry-run unless --apply.",
                    "backfill-genres re-resolves every book missing genres and re-runs classification.",
                ],
            },
            {
                "heading": "Interface",
                "items": [
                    "Dashboard, Books, Services and Activity views, plus per-book and per-service detail pages, served as one page with no framework and no build step.",
                    "Failures are grouped by cause rather than by book, with a suggested next step in plain language and a Retry all for the groups where retrying helps.",
                    "Books are classified done / partial / unavailable / failed / working, so \"no audiobook exists anywhere\" does not read as a fault needing attention.",
                    "Every stage shows its status, attempt count, real error and a Retry button on the book detail page.",
                    "A failing credential raises a banner on every page naming the service and what it breaks, with a link to the fix.",
                    "Saving credentials re-probes immediately and toasts whether they work, while the form is still on screen.",
                    "Service health is on screen at all times in the header, one click from the page that explains it.",
                    "Manual controls for Sweep now, Reconcile, Test, category override and per-book auto-shelve.",
                ],
            },
            {
                "heading": "Operations",
                "items": [
                    "One background thread sweeps the book list; stages are independent and retried independently, so a stuck audiobook does not stop an ebook being filed.",
                    "A sweep has a 120-second budget and advances up to 6 books concurrently, and books are served least-recently-touched first so a bounded pass still rotates through the whole list.",
                    "A stage that exhausts its attempts (3 to 6, per stage) parks the book rather than looping forever, and resumes when someone hits Retry.",
                    "A sweep is lock-guarded, so a manual Sweep now cannot overlap the scheduled one.",
                    "A stage whose download completed after placement gave up re-runs placement, and a late audiobook re-runs shelving to the both-formats shelf.",
                    "SQLite in WAL mode behind one module-level connection and a lock. No ORM, one file to inspect.",
                    "/api/health is an unauthenticated liveness probe for the container healthcheck.",
                    "Sub-commands: report, repair, audit, rename, reconcile, backfill-genres, set-password, users, delete-user.",
                    "/api/version reports what the running build is actually serving.",
                ],
            },
            {
                "heading": "Security",
                "items": [
                    "Passwords are hashed with scrypt and compared in constant time. Nothing reversible is stored.",
                    "Sessions are stateless HMAC-SHA256 tokens, verified before the payload is parsed. They last 7 days.",
                    "Session tokens carry a password epoch, so changing a password evicts every outstanding session.",
                    "The cookie is HttpOnly and SameSite=Lax, which is what stops another site POSTing to /api/... with the cookie attached.",
                    "Failed logins cost a progressive delay, never a lockout, so an attacker cannot lock the owner out of their own UI by hammering a known username.",
                    "A nonexistent username is verified against a dummy hash, so a wrong username and a wrong password take the same work.",
                    "Concurrent sign-ins are capped, because scrypt is memory-hard and a flood would otherwise allocate far more than the box should be asked for.",
                    "X-Forwarded-For is honoured for rate limiting only when TRUST_PROXY is set by a proxy you control.",
                    "Everything except /login, /api/auth/login, /api/auth/status, /api/health and /favicon.ico requires a session.",
                    "The noVNC desktop has no published port: x11vnc binds to loopback inside the container and the app bridges to it over a session-checked WebSocket.",
                    "COOKIE_SECURE=1 is available for when the service is only ever reached over HTTPS.",
                ],
            },
        ],
    },
]
