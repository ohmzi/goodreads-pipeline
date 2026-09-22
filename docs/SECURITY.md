# Security

Written for someone deciding whether to run this, and for someone who has just
deployed it and wants to know what is protecting what. It states the posture as
it is, including the parts that are weaker than the README's summary of it
implies. This file is the authority; the README only points here.

If you are installing this for the first time, read **What is enforced, and
where** below first — it is the whole posture on one page. Everything after it
is the reasoning.

---

## What is enforced, and where

Every control the app applies by itself, what it stops, and the file that
implements it. Anything not on this list is either the deployment's job (TLS,
the network) or an accepted limit (below).

| Threat | What the app does | Where |
|---|---|---|
| Someone reaches the UI from off-host | Nothing in the app — **this is yours to set**, and `PUBLISH_HOST` is not it. That decides which of the host's addresses the port answers on; a tunnel in front reaches the app whichever value it has. What gates the app itself is its sign-in page. | `.env`, `docker-compose.yml` |
| A stored credential is read off disk | Fernet-encrypted per value; the key is `GOODREADS_SECRET_KEY`, never written by the app | `app/crypto.py` |
| A weak `GOODREADS_SECRET_KEY` | Refused at first use below 32 characters, with its own message | `app/crypto.py` |
| The data volume is readable by another account | `umask 0o077` for the whole process, plus a one-off tighten of what is already there | `app/permissions.py` |
| Someone guesses the password online | scrypt, then a per-username and per-client delay that doubles to 8s — a delay, never a lockout | `app/auth.py`, `app/main.py` |
| A flood of sign-ins pins the CPU | scrypt runs off the event loop behind a semaphore of 8, and a sign-in waits up to 1s for a slot | `app/main.py` |
| An attacker evicts the activity feed | Failed sign-ins are logged on the 1st and every 10th attempt per client, and every event message is bounded | `app/main.py`, `app/db.py` |
| Another site posts to this app with your cookie | `SameSite=Lax`, plus a server-side `Origin`-vs-`Host` check that refuses the request outright | `app/main.py` |
| Another site opens the VNC panel | The same origin check on the WebSocket handshake, refused with the same code a missing session gets | `app/main.py` |
| A stale session stays usable after sign-out | Signing out rotates `session_epoch`, which revokes every token for that user everywhere | `app/main.py`, `app/auth.py` |
| A path the router and the gate disagree about | The gate compares the same string the router routes on | `app/main.py` |
| A malicious service answers a request | Redirects refused, bodies capped at 16 MiB, and response bodies withheld from anything that carried a credential | `app/clients/base.py` |
| A hostile epub | The two XML documents are size-capped and entity definitions refused | `app/genres.py` |
| A sweep runs for hours and hides the fact | A 120s budget that phases are skipped under, worker pool outliving the sweep, and a per-book in-flight guard | `app/pipeline.py` |
| A rogue service is reached with credentials | Out of scope: nothing here restricts where the app may dial. `PUBLIC_ORIGIN` and the refusal forward are not that. | — |
| Browser-to-app traffic is encrypted | Out of scope: the app speaks plain HTTP and sets no HSTS. Terminate TLS in front. | — |

Two things this app deliberately does **not** set, and why:

- **No Content-Security-Policy.** The obvious one blanks the interface:
  `img-src 'self' data:` kills every book cover, because `cover_url` is scraped
  from Goodreads and never proxied locally, along with the two
  `s.gr-assets.com` backgrounds. `script-src 'unsafe-inline'` would have to be
  granted to keep the pages' inline handlers working, which is most of what a
  CSP is for. Four headers that cannot break anything are worth more than five
  that can.
- **No `Strict-Transport-Security`.** It is meaningless over the plain HTTP
  this app speaks by default, and actively harmful if set while plain HTTP is
  still reachable. The TLS terminator's job.

---

## What the service is trusted with

Two capabilities decide everything below.

**It holds the key that decrypts every stored credential.** Service API keys and
service passwords are entered through the UI and kept as ciphertext in SQLite
(`app/main.py:1559`, `app/db.py:705`). The key that unwraps them is
`GOODREADS_SECRET_KEY`, read from the environment (`app/config.py:36`) and used
in two places, for two different purposes (below).

**It drives a real browser signed into a real personal account.** The Goodreads
login is a Chromium on a virtual display inside the container, and the cookies
it captures are written to the data volume (`app/login.py:115`).

The consequences, plainly:

- Signed in, you are the operator. From Settings you can list which credentials
  are set, test each one, start a browser inside the account's session, and
  drive the pipeline. There is no read-only role and no per-resource scoping.
- The data volume is the secret store. It holds `goodreads.db` (ciphertext for
  credentials, plaintext for everything else), `goodreads_state.json` (a live
  Goodreads session), and `browser-profile/` (a Chromium profile with the same
  cookies). Copying the database plus knowing the secret key yields every
  credential. Copying `goodreads_state.json` alone yields the Goodreads account
  without the key — it is the single most valuable file the app writes, and it is
  written to the volume rather than into the repository.
- The service is single-user in practice. The schema allows several rows in
  `users`, and `python -m app.cli set-password|users|delete-user` manages them
  (`app/cli.py:1179`), but nothing in the web UI creates or manages an account.

### The data volume is owner-only

None of the above is worth much if another account on the host can read the
volume, and until recently it could: the process ran under the usual `022`
umask, so `goodreads.db`, its `-wal`/`-shm` sidecars, `goodreads_state.json` and
the whole Chromium profile tree were world-readable.

Two mechanisms now cover it, in `app/permissions.py`:

- **`umask 0o077`**, set once while the `app` package is imported — which is
  before either entrypoint has opened a file. Every file the process creates is
  owner-only without any call site having to remember, and the Chromium child
  inherits it, which is how the browser profile is covered.
- **A one-off tighten at startup**, because a umask only affects *creation* and
  a deployment that has already been running has a database at 0644. It
  `chmod`s `goodreads.db*` and `goodreads_state.json` to `0600` and
  `browser-profile/` to `0700`, logs what it changed, and is idempotent — so it
  is one line in the activity feed on the first start after an upgrade and
  nothing on every start after that.

The `-wal` and `-shm` sidecars are matched by prefix deliberately and are not
incidental: SQLite commits into the WAL before checkpointing, so the sidecar
holds the same credential ciphertext the database does.

**The media library is deliberately excluded.** `/books` and `/audiobooks` exist
to be read by other applications, which are separate containers with their own
uid. A category folder created owner-only would make every book beneath it
unindexable — silently, because this app can still read its own tree. The one
place the app creates directories there restores `0o755` on every level it
created (`app/pathing.py:330`), which is what a directory had before the umask
changed, so nothing about the library's accessibility moves.

---

## Credential storage

`app/crypto.py` is the whole of it.

The class key is the Fernet key produced by hashing the secret:

```python
key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
```

Encryption and decryption are Fernet (`cryptography==44.0.0`), so each value is
authenticated: a ciphertext edited in the database fails to decrypt rather than
returning wrong plaintext. Decryption failure raises with the cause named — the
stored value was written under a different secret key — instead of returning
empty (`app/crypto.py:57`).

Two properties of that derivation matter to a deployer:

- There is no salt and no stretching between the secret and the key. SHA-256 is
  one pass, so the key is exactly as strong as the string in `.env`. A weak or
  reused value is offline-guessable by anyone holding a copy of the database.
  Generate it: `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
  The file documents its own trade here (`app/crypto.py:1`): the key lives in
  `.env` at mode 600 rather than baked into the image. Because nothing stretches
  it, a secret under **32 characters is refused** at first use, with a message
  of its own so it is not confused with the empty case
  (`app/crypto.py:26`). Losing the secret key means re-entering every
  credential, so this refusal is a floor, not a rotation.
- **Losing the secret key makes stored credentials unrecoverable.** The
  ciphertext stays and the app raises `RuntimeError` on read. `InvalidToken`
  never reaches a caller: `app/crypto.py` catches it and re-raises from it, with
  the message spelling out that the key changed since the value was saved and
  the value must be re-entered in Settings (`app/crypto.py:60`). Every read path
  surfaces that `RuntimeError` unchanged — `cred()` in `app/clients/base.py` is
  one, and it does nothing but return `cipher().decrypt(blob)`
  (`app/clients/base.py:121`). Re-entering is the recovery procedure; there is no
  other one.

An empty `GOODREADS_SECRET_KEY` refuses loudly at first use rather than writing
credentials that look stored and cannot be read back after a restart
(`app/crypto.py:35`). Note where that check *isn't*: the app deliberately does
**not** validate the key in its startup hook. Validating there would convert a
deployment whose key predates the 32-character floor into a crash loop under
`restart: unless-stopped`, with nobody watching and no way in. The refusal
happens where a credential is actually used, and `cli set-password` checks the
same constant itself so its refusal stays the documented exit 2
(`app/cli.py:690`) rather than a traceback.

Scope of encryption: the `credentials` table only. Book titles, authors, file
paths, categories, service names and the event log are plaintext in the same
database. The event log also records sign-in attempts with the caller's address
(`app/main.py:363`).

---

## Login and sessions

`app/auth.py` owns this.

| Concern | Implementation | Where |
|---|---|---|
| Password hash | scrypt, `n=2**14`, `r=8`, `p=1`, 32-byte output, 16-byte random salt | `app/auth.py:39`, `app/auth.py:57` |
| Stored format | `scrypt$n$r$p$salt$hash`, self-describing so cost can be raised later | `app/auth.py:68` |
| Comparison | `hmac.compare_digest` | `app/auth.py:86` |
| Session token | `base64url(json).base64url(HMAC-SHA256)` | `app/auth.py:98` |
| Signing key | `sha256("goodreads-session:" + GOODREADS_SECRET_KEY)` | `app/auth.py:95` |
| Token lifetime | 7 days, in the signed payload and as the cookie's `max_age` | `app/auth.py:35` |
| Cookie | `goodreads_session`, `HttpOnly`, `SameSite=Lax`, `Path=/`, `Secure` from `COOKIE_SECURE` | `app/main.py:382` |
| Revocation | Signing out rotates `users.session_epoch`; every token minted under the old one stops being accepted | `app/main.py:399`, `app/auth.py:143` |

Notes on why it is shaped this way:

- Passwords are hashed with a memory-hard KDF because, unlike a service API key,
  there is never a reason to read one back.
- The token payload is `{u, e, exp, n}` — username, password epoch, expiry, and
  a per-login nonce so two sign-ins of the same user never produce the same
  cookie (`app/auth.py:99`). Signature verification happens before the bytes are
  parsed as JSON (`app/auth.py:125`).
- The `e` field binds the token to the password it was minted against. Every
  request compares it to `users.session_epoch`, `set-password` rotates the
  epoch, and **so does signing out** — so a sign-out revokes outstanding
  sessions on every device, not merely the cookie in the browser that made the
  request (`app/auth.py:143`, `app/main.py:399`, `app/cli.py:727`). That is a
  deliberate trade with the sign-out button: signing out here signs you out
  everywhere. Naming and revoking one session would need a server-side session
  table, which this design does not have.
- Failed sign-ins are delayed progressively rather than locked out: three free
  attempts, then 0.5s doubling to a ceiling of 8s, counted per username and per
  client address over a 15-minute window (`app/auth.py:182`). The class docstring
  is explicit that a hard lockout was rejected on purpose — it is a denial of
  service an unauthenticated attacker can aim at a known username.
- A nonexistent username is verified against a dummy scrypt hash so a wrong
  username costs the same work as a wrong password (`app/main.py:102`,
  `app/main.py:342`). The hash is computed once at import.
- scrypt is CPU-bound, so it runs off the event loop and behind a semaphore of
  8 concurrent sign-ins, which bounds how much memory a flood can pin
  (`app/main.py:271`). The throttle's delay is deliberately held **inside** that
  slot: it is what bounds how many guesses a flood can land per second, and
  releasing the slot first would let an attacker pipeline scrypt at pure slot
  speed. A sign-in therefore *waits* up to `_LOGIN_SLOT_WAIT_SECONDS` (1s) for a
  slot rather than being refused the instant none is free
  (`app/main.py:279`). The residual is stated under Known limits.

### What an anonymous caller cannot do

`/api/auth/login` is one of the few routes reachable without a session, so it is
where anonymous input lands. Three things bound it:

- **The username is truncated where it is used, not rejected.** `MAX_USERNAME_CHARS`
  is 64 (`app/main.py:284`). A model-level `max_length` was deliberately not
  used: it would reject the request, and the only account there is was created
  from the CLI — so a cap the model enforced could lock the operator out of
  their own UI over a username that already exists. Truncation keeps the 401
  (so the sign-in page still shows the real message) and cannot make an existing
  credential unusable.
- **Every event message is bounded and normalised.** `Database.log` is the only
  writer of `events`, so one edit covers all thirty-odd call sites, most of which
  pass exception or service text straight through. Messages are whitespace-
  collapsed and clipped at 500 characters with a visible ellipsis
  (`app/db.py:178`, `app/db.py:821`).
- **Failed sign-ins do not get a log line each.** `/api/state` re-sends the
  newest 60 events to every open browser every six seconds, so one caller in a
  loop was enough to push every real line out of the activity feed. The first
  failure from a client is logged and then every tenth, each line carrying the
  running count (`app/main.py:295`), read from the counter the throttle already
  keeps.

### Cross-site requests

`SameSite=Lax` is the first line, and it is not sufficient on its own: it is a
browser-side control, so it does nothing at all for a caller that is not a
browser, and it withholds cookies on a cross-site POST but not on a cross-site
GET — which is the shape of several endpoints worth attacking here (`/api/sweep`
moves real files on disk, `app/pathing.py:379`).

So the origin is checked on the server, before the session is even looked at
(`app/main.py:122`). A request whose `Origin` disagrees with the host it was sent
to is refused with 403. Two properties matter:

- **A missing `Origin` is allowed**, and has to be: browsers omit it on
  same-origin navigations, and neither the container's own healthcheck nor the
  pipeline's service calls is a browser at all.
- **The check runs before the session check**, so a cross-site caller gets 403
  and learns nothing about whether it holds a session. Refusals are logged only
  when the caller *did* hold one — otherwise the check would itself be a way to
  fill the activity feed from an address with no account.
- `PUBLIC_ORIGIN` overrides the comparison when a reverse proxy rewrites `Host`
  (below). Without it a proxy deployment makes every request look foreign, and
  the symptom is a VNC panel that silently never connects.

Access control is one middleware rule: everything needs a session except
`/login`, `/api/auth/login`, `/api/auth/status`, `/api/health`,
`/favicon.ico`, `/static/app.css` and `/static/app.js`
(`app/main.py:57`, `app/main.py:147`). That covers the pages, the rest of
`/static`, `/api/*` and the VNC bridge. Unauthenticated API and VNC requests
get a 401, other paths get redirected to the login form. `/api/auth/status`
is public because the login page asks it whether to render the form; it
answers only whether the caller already holds a session. The two asset files
are public because the sign-in page loads its stylesheet from `/static/` and
rendered unstyled while it was gated (the gate answered with a 303 to
`/login`, and the browser parsed the sign-in page itself as CSS); both are
static files with no state in them.

**The gate compares the path the router routes on** (`request.scope["path"]`,
not `request.url.path`). The difference is not cosmetic: `request.url.path`
reparses the URL and so cuts it at a decoded `?`, which meant a path like
`/static/app.css%3F/../app.js` was compared as `/static/app.css` — a public
asset — and waved through to a static mount and an SPA fallback the router had
never matched it to. Comparing the same string the router uses closes that and
the Host-header variant of it at once (`app/main.py:147`).

### Response headers

Four, applied to every response — including the refusals the gate writes itself,
which is the traffic an unauthenticated caller generates most of
(`app/main.py:211`, `app/main.py:225`). They are set with `setdefault`, so a
route that decided its own value keeps it:

| Header | Value | Why |
|---|---|---|
| `X-Content-Type-Options` | `nosniff` | Stops a response that is not the type it claims from being sniffed into one that is. |
| `Referrer-Policy` | `no-referrer` | Nothing here needs to tell another site where the operator came from. |
| `X-Frame-Options` | `SAMEORIGIN` | `SAMEORIGIN` and not `DENY`, because `/goodreads` frames `/vnc/vnc.html` same-origin. |
| `Cache-Control` | `no-store`, on `/api/` only | API answers are per-session state. Set last and still via `setdefault`. |

**There is no `Content-Security-Policy`, deliberately.** See the top of this
file for why shipping the obvious one would blank the interface.

**There is no default account.** No account is seeded at startup, no
first-visitor-becomes-admin path exists, and if zero users exist the app logs an
error saying nobody can sign in and how to fix it (`app/main.py:634`). The login
has to be created from a shell with `set-password` before the UI is usable. The
CLI refuses passwords under 12 characters (`app/cli.py:721`), and `--generate`
prints the password once and stores only the hash.

---

## What the app sends to your other services

Every service call is a request carrying a credential — a password in a login
body, an API key or bearer token in a header — so `app/clients/base.py` treats
the answer as hostile by default. Three rules:

- **Redirects are refused, never followed** (`app/clients/base.py:182`). A
  service is addressed directly by URL, so a redirect means something is
  configured differently than the client believes, and following it could
  deliver credentials to a host nobody chose. The refusal carries `kind="auth"`
  explicitly, and that is load-bearing rather than decorative: raised as an
  `httpx.HTTPError` it would carry no status, so `kind` would derive "network" —
  which `breaker.TRANSIENT_KINDS` treats as transient, so a permanent
  misconfiguration would count as an outage and park every book for 24 hours,
  quietly. The client is constructed with `follow_redirects=False`
  (`app/clients/base.py:164`).
- **Response bodies are capped at 16 MiB** (`app/clients/base.py:26`). Nothing
  here expects a payload that size, so a service answering with one has either
  malfunctioned or is not the service we believe we are talking to. The body is
  refused whole rather than truncated — half a JSON document is a parse error
  somewhere else, which reads as the service being broken rather than as the
  answer having been too big.
- **Error bodies are not quoted back for any client that authenticates**
  (`app/clients/base.py:159`). A response that echoes its request — a hostile or
  hijacked endpoint, a proxy error page — would otherwise write the credential in
  clear into `stage_runs.detail`, which is the one invariant the `credentials`
  table's encryption exists to keep. Removing the submitted value from the text
  is not an alternative: the echo can be encoded, truncated or reworded, and the
  check would have to enumerate what to look for. Shelfmark, which takes no
  credential on this deployment, still quotes its bodies — and every live failure
  detail that quotes one comes from it.

The cap deliberately rebuilds the response without `Content-Encoding`,
`Content-Length` and `Transfer-Encoding` (`app/clients/base.py:37`). Those
describe a transfer that has already happened: `iter_bytes()` hands back
*decoded* bytes, and a response rebuilt with `Content-Encoding: gzip` still on it
runs the decoder over plain bytes a second time and raises `DecodingError` —
which is an `httpx.HTTPError`, so it would surface as a network failure and be
forgiven for a day. This is not hypothetical; it reproduces with a gzipped
upstream, and there is a test with one.

---

## The interactive Goodreads login

There is no password flow to automate. Goodreads removed its public API in 2020
and its public shelf pages in early 2026, and `/user/sign_in` now hands off to
Amazon's sign-in, complete with OTP and CAPTCHA. There is no form to POST a
password to, so every maintained tool reuses a persistent browser session
instead (`app/goodreads.py:1`). The browser is used for login only; reads and
writes afterwards go over HTTP with the captured cookies.

Isolation is four layers, and each one is load-bearing:

1. **A virtual display.** `Xvfb :99 -screen 0 1280x900x24 -nolisten tcp` — no
   real display, and no X TCP listener (`docker/start-vnc.sh:26`).
2. **x11vnc on loopback.** Started with `-rfbport 5900 -localhost` and no
   password (`docker/start-vnc.sh:38`). `-localhost` is what keeps the raw RFB
   socket unreachable from the compose network. The script then *tries* to
   confirm the bind with `ss` (`docker/start-vnc.sh:48`), but `iproute2` is not
   installed in this image, so that branch is skipped and has never run — the
   flags are the control, and the check is a no-op that would be a false comfort
   if it did run. There is no VNC password at all, so the loopback bind is the
   entire access control on that socket. *If you want the confirmation to be
   real, install `iproute2` in the Dockerfile; it is not there today.*
3. **The app bridges to it.** The container talks to `127.0.0.1:5900`
   (`app/main.py:44`) and republishes it as a WebSocket at `/vnc/ws`, which
   checks the **origin** first and the session cookie second, closing with 1008
   for either (`app/main.py:446`). The order is deliberate: a caller refused for
   its origin sees exactly what a caller refused for its session sees, so the
   handshake cannot be used to tell the two apart. Missing `Origin` is allowed,
   as everywhere else. The noVNC static client is served from the image by the
   app, with a resolve-and-confine check so the asset path cannot escape its
   directory (`app/main.py:521`).
4. **No VNC port is published.** `websockify` is installed — as a hard
   dependency of the `novnc` package — and is simply never started. **Do not
   purge it**: on jammy it takes `novnc` with it, and `/usr/share/novnc` is
   exactly what `/vnc/{asset}` serves, so the login desktop would go blank. The
   comment in the Dockerfile says so at the point where it would have been
   (`Dockerfile:17-20`), and the compose file publishes exactly one host port
   mapped to the container's 8090 (`docker-compose.yml`, the `ports:` entry).
   That is the only published port by design.

The browser itself runs with `--no-sandbox` and a persistent profile directory
on the volume (`app/login.py:98`, `app/login.py:89`). Sandboxing is not part of
this deployment's containment story; the container boundary and the loopback
bind are. Removing `--no-sandbox` is not a hardening step here: Docker's default
seccomp profile can block the namespace sandbox regardless, and losing it breaks
the interactive login, which is the only path to a Goodreads session.

### Ending the session locally

`goodreads forget-session` deletes `goodreads_state.json` **and** the browser
profile, and refuses while a login browser is running
(`app/login.py:137`). Both, because they carry the same cookies for the same
account: removing one of the two leaves a live session on the volume, which is
the opposite of what "forget" was asked to mean. This is the only supported way
to end a Goodreads session from the app's side; there is no button for it,
because it needs a shell anyway to be sure it happened.

---

## Reverse-proxy deployment

Four environment variables exist because their defaults are wrong in one of the
two deployment shapes. All are read in `app/config.py` and set in `.env`.

| Variable | Default | Effect | Getting it wrong |
|---|---|---|---|
| `PUBLISH_HOST` | every interface | Which host address the compose file publishes the UI on (`docker-compose.yml`, `ports:`) | Two failure modes, in opposite directions. Left at `0.0.0.0`, a published port is reachable by anything that can route to the host — and ufw's default-deny does **not** cover it, because docker's iptables rules are evaluated first. Narrowed to an address the front end does not dial, every request is refused and whatever sits in front serves a 502. **Confirm the target before changing it** — see below. |
| `COOKIE_SECURE=1` | `0` | Adds `Secure` to the session cookie | On while serving plain HTTP: the browser refuses to store the cookie and sign-in appears to fail silently with no error anywhere. Off while serving HTTPS: the cookie is sent over the plaintext hop as well. |
| `PUBLIC_ORIGIN` | unset | What this app is reached *as*, for the same-origin check (`app/config.py:97`) | Unset behind a proxy that rewrites `Host`: every state-changing request and the VNC handshake look cross-site, and the symptom is a panel that never connects with no error you would connect to a proxy. |
| `TRUST_PROXY=1` | `0` | Uses the first `X-Forwarded-For` entry as the client address for rate limiting (`app/main.py:252`) | On without a proxy you control: the header is caller-supplied and trivially rotated, so the per-client throttle is bypassed at will. Off behind a real proxy: every request counts as the proxy's address, so one attacker's failures throttle everyone. |

Turn on `COOKIE_SECURE` at the same moment TLS goes in front, not before.
`TRUST_PROXY` is a statement about who sets the header, not about whether a
proxy exists. `PUBLIC_ORIGIN` is a statement about what a browser sees, and
`PUBLISH_HOST` about what the host listens on — the two are not the same setting,
and which of them a deployment needs depends on the proxy.

### Working out what to set

`PUBLIC_ORIGIN` is only needed by a proxy that **rewrites `Host`**. A proxy that
passes the browser's `Host` through — which is the default for nginx, Caddy,
Traefik and a Cloudflare tunnel — needs nothing here, because `Origin` and
`Host` already agree. Telling them apart is one request: post something
deliberately wrong to `/api/auth/login` through the public URL with the real
`Origin` header. `401` means the check passed and the credentials were simply
wrong; `403` means it refused and `PUBLIC_ORIGIN` is what is missing.

`PUBLISH_HOST` is decided by *where the front end dials from*, and the app logs
that address:

```bash
docker compose logs goodreads | grep 'GET /login'
```

The left-hand address is the front end. The rule that catches people out: a
proxy or tunnel running in a **container** reaches a host-published port **as
its bridge gateway** (`172.x.0.1`), never as `127.0.0.1` — so `PUBLISH_HOST=127.0.0.1`
serves only a proxy running directly on the host, and a containerised one needs
`0.0.0.0`. Getting it wrong is a 502 from whatever sits in front, with nothing
in the app's own logs at all, because the request never arrives.

**What this setting is not.** It decides which of the host's own addresses the
port is published on. It does not decide whether the app is reachable from
outside: a tunnel in front reaches it whichever value is set. If your front end
is a Cloudflare tunnel with no Access policy, then the sign-in page is the only
thing between the internet and this app — which is a defensible posture here
(scrypt, progressive throttling, no default account, no first-visitor-admin
path), but it is worth knowing that it is the posture, rather than believing a
firewall is covering it. A tunnel with an Access policy in front is strictly
better, and costs nothing in this app.

Below that, TLS is entirely the proxy's job: the app speaks plain HTTP, sets no
`Strict-Transport-Security` and no `Content-Security-Policy`, and `SameSite=Lax`
plus the server-side origin check are the CSRF defence rather than a token.

---

## Host-side container hardening

`docker-compose.yml` sets four things. They are tier 1 only: **the container
still runs as root**, and dropping that is a separate, deliberate migration —
it needs a chown of the database, its WAL, the auth state file and the whole
Chromium profile, and it is not a config-line change.

| Setting | Why | What it is not for |
|---|---|---|
| `security_opt: no-new-privileges:true` | Ubuntu jammy still ships setuid `su`, `passwd` and `mount`, and this container never needs to gain privileges after starting. | Chromium's sandbox helper — `chrome_sandbox` is mode 0777 in this image, not setuid, so this flag does not touch it. |
| `pids_limit: 512` | Bounds a fork bomb. Measured, not guessed: idle is 12 and the peak with the interactive login browser open is 157, since Docker counts tasks and Chromium is thread-heavy. | A tight cap. Set too low it does not fail loudly — it appears as an intermittent `fork: Resource temporarily unavailable` when Chromium launches, which is easily misread as a VNC fault. |
| `cap_drop: [ALL]` | Removes NET_RAW, SETUID, SETGID, CHOWN, FOWNER, KILL, MKNOD, SYS_CHROOT, SETFCAP — none of which this app uses. | A complete drop. See below. |
| `cap_add: [DAC_OVERRIDE]` | The one capability uid 0 genuinely needs here. | Optional. Dropping it breaks `/data`. |

`DAC_OVERRIDE` is not optional and this is the trap: the `/data` bind source is
owned by the host user's uid (1000), so with `CapEff` cleared uid 0 falls
through owner (1000≠0) and group (1000≠0) to `other`, which is r-x. Writes then
fail — and they fail **after a restart**, which is the expensive moment to find
out. What that breaks: SQLite's `-wal`/`-shm` creation and cleanup
(`app/db.py:192`), `mkdir /data/browser-profile` (`app/login.py:89`),
Playwright's `storage_state()` write (`app/login.py:115`), and any
`destination.parent.mkdir` under `/books`. Verify before trusting a change here:

```bash
docker exec goodreads touch /data/.rwprobe && docker exec goodreads rm /data/.rwprobe
```

Deliberately **not** set:

- `read_only: true` — Xvfb and Chromium both write `/tmp`, so it needs `tmpfs`
  mounts on `/tmp` and `/dev/shm` alongside. The largest regression risk for the
  least additional benefit.
- `mem_limit` — host-dependent, so it belongs in the gitignored
  `docker-compose.override.yml` rather than in the committed file.

**`ports: !override` does not work**, and the compose files say so. Compose
merges port lists by `(target, published, protocol)` and ignores the tag, so an
override listing one mapping publishes that *and* the base file's — verified
against Compose v2.36.2, which does honour `!override` on ordinary fields like
`command`. `PUBLISH_HOST` is interpolated from `.env` instead, so there is a
single entry and nothing to merge.

The residual, stated plainly: still `--no-sandbox`, still uid 0, and `/data`'s
directory is owned by the host user's uid — which is exactly *why* the capability
set is not empty.

---

## What is not in this repository

No credential values are committed. `.env.example` carries names with empty
values (`GOODREADS_SECRET_KEY=`, `GOODREADS_USER_ID=`) and a comment pointing at
`GOODREADS_SECRET_KEY` as the thing whose loss is unrecoverable. `.env` itself is
excluded from git and from the image build (`.gitignore:2`, `.dockerignore`),
along with `data/`, `*.db`, `browser-profile/` and `*_state.json` — the last two
being where the Goodreads cookies would land if they were ever written inside
the tree. A scan of the working tree for literal secret-shaped assignments
outside `.env` returns nothing.

There is exactly one committed key-shaped literal: the default Goodreads AppSync
key at `app/goodreads.py:52`. It is a public key that ships inside Goodreads'
own pages, rotates with their frontend builds, and is overridable from Settings
if it stops working (`app/genres.py:75`). It is not the operator's secret and
not a leak of one; it is mentioned here so that a reader scanning the source for
"is anything committed that looks like a key" finds the answer rather than a
surprise.

The committed `docker-compose.yml` carries no host specifics. Its volumes are
relative placeholders (`./data`, `./books`, `./audiobooks`), it declares no
external networks and no `ipam` block, and it publishes on every interface by
default — deliberately, so that a clone runs without editing anything, and
narrowed per host through `PUBLISH_HOST`. The host mount paths, the external
networks the other services sit on, and a pinned subnet all live in
`docker-compose.override.yml`, which is gitignored and host-specific; compose
loads it automatically. None of it is part of the app.

### What a deployer must generate

| Thing | How |
|---|---|
| `GOODREADS_SECRET_KEY` | `python -c "import secrets; print(secrets.token_urlsafe(48))"`, then `chmod 600 .env`. Must be at least 32 characters. |
| A login | `python -m app.cli set-password <username> --generate`, from inside the container |
| Service credentials | Entered through the Settings page, encrypted on write: Kavita API key, BookLore / Grimmory / Audiobookshelf usernames and passwords, Open Notebook password, SABnzbd and Prowlarr API keys, the Goodreads AppSync key, and optionally a Google Books key and a Hardcover API key — Hardcover is labelled optional and not yet used (`app/main.py:71`) |
| The Goodreads user id | `GOODREADS_USER_ID` in `.env`, or left blank and detected from the saved browser session, or pinned from Settings (`app/goodreads.py:102`) |
| The Goodreads session | Once, through the browser flow: Start browser, sign in, Save session |
| The published address | `PUBLISH_HOST` in `.env` |
| TLS | A reverse proxy in front, with `COOKIE_SECURE=1` and `PUBLIC_ORIGIN` set at the same time |

Nothing above is generated for you, and none of it is stored anywhere recoverable
by the app: the secret key is the only copy of the credential key, and the
generated password is printed once.

---

## Known limits

Stated rather than softened, and kept here in full rather than summarised
anywhere else.

- **The session cookie is a bearer token.** Anyone who reads it is you until it
  expires. There is no per-*session* revocation: signing out rotates the epoch
  and so revokes every token for that user, on every device, and there is no way
  to end one session and keep the rest. Rotating `GOODREADS_SECRET_KEY` also
  evicts everyone and additionally makes every stored credential undecryptable.
- **Login throttling is in-memory**, so restarting the process clears it
  (`app/auth.py:175`). It is also a delay, not a lockout — see above; a wrong
  password is slowed rather than stopped.
- **A sustained sign-in flood can still refuse a correct password.** This is the
  residual of the fix rather than an oversight. The delay holds its concurrency
  slot on purpose (that is what bounds guess rate), so slots turn over slowly
  under load; a sign-in now waits up to 1s for one instead of being refused the
  instant none is free (`app/main.py:279`), and only then gets a 429. Getting
  both — never refusing the owner *and* never letting a flood through quickly —
  needs per-client slot limits, which is a larger change than this was.
- **An unauthenticated caller cannot be told apart from a busy one** on
  `/api/health`, which answers `{"status":"ok"}` to anyone. That is deliberate:
  it is the container's healthcheck.
- **Traffic is plain HTTP unless you put a TLS terminator in front.**
- **The VNC socket has no password.** Its only protection is the loopback bind
  inside the container plus the app's session and origin checks on the bridge.
  That is a deliberate trade — the alternative is a second credential to
  distribute — but it means anything already running as a process in this
  container, or able to reach the container's loopback, has an unauthenticated
  path to a desktop signed into Goodreads.
- **The browser runs with `--no-sandbox`** inside the container.
- **The container runs as root**, with `DAC_OVERRIDE` retained. See the
  hardening table above for why that capability specifically.
- **The database is not encrypted.** Credential values are; book titles, file
  paths, categories and the event log are not.
- **The master key is not stretched.** SHA-256 of the secret, one pass, with a
  32-character floor and nothing more. Use a generated 48-byte value; do not
  reuse a password here.
- **The app will dial wherever a service URL points.** Nothing here restricts
  outbound destinations, so a URL pointed at a hostile host sends it whatever
  credential that service's client attaches. The refusal of redirects and the
  withholding of error bodies bound what comes *back*; they do not bound where
  the app will go. This is accepted, not mitigated.
- **The threat model is a host you control.** Nothing in this design assumes the
  network between the browser and the app is hostile — that assumption is what
  `COOKIE_SECURE`, `PUBLISH_HOST` and a TLS terminator exist to restore.
