# Security

Written for someone deciding whether to run this. It states the posture as it
is, including the parts that are weaker than the README's summary of it
implies. This file is the authority; the README only points here.

## What the service is trusted with

Two capabilities decide everything below.

**It holds the key that decrypts every stored credential.** Service API keys and
service passwords are entered through the UI and kept as ciphertext in SQLite
(`app/main.py:1046`, `app/db.py:453`). The key that unwraps them is
`GOODREADS_SECRET_KEY`, read from the environment (`app/config.py:36`) and used
in two places, for two different purposes (below).

**It drives a real browser signed into a real personal account.** The Goodreads
login is a Chromium on a virtual display inside the container, and the cookies
it captures are written to the data volume (`app/login.py:93`, `app/login.py:114`).

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
  (`app/cli.py:746`), but nothing in the web UI creates or manages an account.

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
empty (`app/crypto.py:34`).

Two properties of that derivation matter to a deployer:

- There is no salt and no stretching between the secret and the key. SHA-256 is
  one pass, so the key is exactly as strong as the string in `.env`. A weak or
  reused value is offline-guessable by anyone holding a copy of the database.
  Generate it: `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
  The file documents its own trade here (`app/crypto.py:1`): the key lives in
  `.env` at mode 600 rather than baked into the image.
- **Losing the secret key makes stored credentials unrecoverable.** The
  ciphertext stays and the app raises `RuntimeError` on read. `InvalidToken`
  never reaches a caller: `app/crypto.py` catches it and re-raises from it, with
  the message spelling out that the key changed since the value was saved and
  the value must be re-entered in Settings (`app/crypto.py:37`). Every read path
  surfaces that `RuntimeError` unchanged — `cred()` in `app/clients/base.py` is
  one, and it does nothing but return `cipher().decrypt(blob)`
  (`app/clients/base.py:57`). Re-entering is the recovery procedure; there is no
  other one.

An empty `GOODREADS_SECRET_KEY` refuses loudly at first use rather than writing
credentials that look stored and cannot be read back after a restart
(`app/crypto.py:21`).

Scope of encryption: the `credentials` table only. Book titles, authors, file
paths, categories, service names and the event log are plaintext in the same
database. The event log also records sign-in attempts with the caller's address
(`app/main.py:175`).

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
| Cookie | `goodreads_session`, `HttpOnly`, `SameSite=Lax`, `Path=/`, `Secure` from `COOKIE_SECURE` | `app/main.py:188` |

Notes on why it is shaped this way:

- Passwords are hashed with a memory-hard KDF because, unlike a service API key,
  there is never a reason to read one back.
- The token payload is `{u, e, exp, n}` — username, password epoch, expiry, and
  a per-login nonce so two sign-ins of the same user never produce the same
  cookie (`app/auth.py:99`). Signature verification happens before the bytes are
  parsed as JSON (`app/auth.py:124`).
- The `e` field binds the token to the password it was minted against. Every
  request compares it to `users.session_epoch`, and `set-password` rotates the
  epoch, so a password change evicts outstanding sessions (`app/auth.py:143`,
  `app/cli.py:603`).
- Sessions are stateless: no session table, nothing to garbage-collect, and no
  way to name and revoke one session. Forcing everyone out means rotating
  `GOODREADS_SECRET_KEY`, which also makes stored credentials undecryptable, or
  changing the password.
- Failed sign-ins are delayed progressively rather than locked out: three free
  attempts, then 0.5s doubling to a ceiling of 8s, counted per username and per
  client address over a 15-minute window (`app/auth.py:179`). The class docstring
  is explicit that a hard lockout was rejected on purpose — it is a denial of
  service an unauthenticated attacker can aim at a known username.
- A nonexistent username is verified against a dummy scrypt hash so a wrong
  username costs the same work as a wrong password (`app/main.py:88`,
  `app/main.py:158`). The hash is computed once at import.
- scrypt is CPU-bound, so it runs off the event loop and behind a semaphore of
  8 concurrent sign-ins, which bounds how much memory a flood can pin
  (`app/main.py:139`, `app/main.py:162`).

Access control is one middleware rule: everything needs a session except
`/login`, `/api/auth/login`, `/api/auth/status`, `/api/health`,
`/favicon.ico`, `/static/app.css` and `/static/app.js`
(`app/main.py:49`, `app/main.py:94`). That covers the pages, the rest of
`/static`, `/api/*` and the VNC bridge. Unauthenticated API and VNC requests
get a 401, other paths get redirected to the login form. `/api/auth/status`
is public because the login page asks it whether to render the form; it
answers only whether the caller already holds a session. The two asset files
are public because the sign-in page loads its stylesheet from `/static/` and
rendered unstyled while it was gated (the gate answered with a 303 to
`/login`, and the browser parsed the sign-in page itself as CSS); both are
static files with no state in them.

**There is no default account.** No account is seeded at startup, no
first-visitor-becomes-admin path exists, and if zero users exist the app logs an
error saying nobody can sign in and how to fix it (`app/main.py:359`). The login
has to be created from a shell with `set-password` before the UI is usable. The
CLI refuses passwords under 12 characters (`app/cli.py:596`), and `--generate`
prints the password once and stores only the hash.

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
   socket unreachable from the compose network; the script then verifies with
   `ss` that the port is actually bound to `127.0.0.1` rather than trusting its
   own flags (`docker/start-vnc.sh:48`). There is no VNC password at all, so
   loopback binding is the entire access control on that socket.
3. **The app bridges to it.** The container talks to `127.0.0.1:5900`
   (`app/main.py:43`) and republishes it as a WebSocket at `/vnc/ws`, which
   checks the session cookie first and closes with 1008 if there is none
   (`app/main.py:218`). The noVNC static client is served from the image by the
   app, with a resolve-and-confine check so the asset path cannot escape its
   directory (`app/main.py:280`).
4. **No VNC port is published.** `websockify` is deliberately not installed —
   the comment in the Dockerfile says so at the point where it would have been
   (`Dockerfile:18`) — and the compose file publishes exactly one host port
   mapped to the container's 8090 (`docker-compose.yml`, the `ports:` entry).
   That is the only published port by design.

The browser itself runs with `--no-sandbox` and a persistent profile directory
on the volume (`app/login.py:93`, `app/login.py:88`). Sandboxing is not part of
this deployment's containment story; the container boundary and the loopback
bind are.

## Reverse-proxy deployment

Two environment variables exist because both defaults are wrong in one of the
two deployment shapes. Both are read in `app/config.py:77` and `app/config.py:83`.

| Variable | Default | Effect | Getting it wrong |
|---|---|---|---|
| `COOKIE_SECURE=1` | `0` | Adds `Secure` to the session cookie | On while serving plain HTTP: the browser refuses to store the cookie and sign-in appears to fail silently with no error anywhere. Off while serving HTTPS: the cookie is sent over the plaintext hop as well. |
| `TRUST_PROXY=1` | `0` | Uses the first `X-Forwarded-For` entry as the client address for rate limiting (`app/main.py:128`) | On without a proxy you control: the header is caller-supplied and trivially rotated, so the per-client throttle is bypassed at will. Off behind a real proxy: every request counts as the proxy's address, so one attacker's failures throttle everyone. |

Turn on `COOKIE_SECURE` at the same moment TLS goes in front, not before.
`TRUST_PROXY` is a statement about who sets the header, not about whether a
proxy exists.

Below that, TLS is entirely the proxy's job: the app speaks plain HTTP, sets no
`Strict-Transport-Security`, no `Content-Security-Policy` and no
`X-Frame-Options`, and `SameSite=Lax` is the CSRF defence rather than a token.
Expose this on a trusted network or terminate TLS in front of it.

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
if it stops working (`app/genres.py:70`). It is not the operator's secret and
not a leak of one; it is mentioned here so that a reader scanning the source for
"is anything committed that looks like a key" finds the answer rather than a
surprise.

The committed `docker-compose.yml` carries no host specifics. Its volumes are
relative placeholders (`./data`, `./books`, `./audiobooks`), it declares no
external networks and no `ipam` block, and it attaches only the default network.
The host mount paths, the external networks the other services sit on, and a
pinned subnet all live in `docker-compose.override.yml`, which is gitignored and
host-specific; compose loads it automatically. None of it is part of the app.
Point the mounts at your own paths there, and pin a subnet only if your host's
default address pools are exhausted.

### What a deployer must generate

| Thing | How |
|---|---|
| `GOODREADS_SECRET_KEY` | `python -c "import secrets; print(secrets.token_urlsafe(48))"`, then `chmod 600 .env` |
| A login | `python -m app.cli set-password <username> --generate`, from inside the container |
| Service credentials | Entered through the Settings page, encrypted on write: Kavita API key, BookLore / Grimmory / Audiobookshelf usernames and passwords, Open Notebook password, SABnzbd and Prowlarr API keys, the Goodreads AppSync key, and optionally a Google Books key and a Hardcover API key — Hardcover is labelled optional and not yet used (`app/main.py:61`) |
| The Goodreads user id | `GOODREADS_USER_ID` in `.env`, or left blank and detected from the saved browser session, or pinned from Settings (`app/goodreads.py:102`) |
| The Goodreads session | Once, through the browser flow: Start browser, sign in, Save session |
| TLS | A reverse proxy in front, with `COOKIE_SECURE=1` set at the same time |

Nothing above is generated for you, and none of it is stored anywhere recoverable
by the app: the secret key is the only copy of the credential key, and the
generated password is printed once.

## Known limits

Stated rather than softened, and kept here in full rather than summarised
anywhere else.

- **The session cookie is a bearer token.** Anyone who reads it is you until it
  expires. There is no per-session revocation: there is no way to kill one
  session and keep the rest. Changing the password evicts every session for that
  user, because the token carries the password epoch (`app/auth.py:143`);
  rotating `GOODREADS_SECRET_KEY` evicts everyone and additionally makes every
  stored credential undecryptable.
- **Login throttling is in-memory**, so restarting the process clears it
  (`app/auth.py:176`). It is also a delay, not a lockout — see above; a wrong
  password is slowed rather than stopped. A correct one is not exempt from the
  cap either: a global `asyncio.Semaphore(8)` bounds concurrent sign-ins
  (`app/main.py:139`), and `/api/auth/login` answers HTTP 429 before the
  password is verified once all eight slots are busy (`app/main.py:148`), so a
  burst of concurrent attempts can refuse a right answer.
- **Traffic is plain HTTP unless you put a TLS terminator in front.**
- **The VNC socket has no password.** Its only protection is the loopback bind
  inside the container plus the app's session check on the bridge. That is a
  deliberate trade — the alternative is a second credential to distribute — but
  it means anything already running as a process in this container, or able to
  reach the container's loopback, has an unauthenticated path to a desktop
  signed into Goodreads.
- **The browser runs with `--no-sandbox`** inside the container.
- **The database is not encrypted.** Credential values are; book titles, file
  paths, categories and the event log are not.
- **The master key is not stretched.** SHA-256 of the secret, one pass. Use a
  generated 48-byte value; do not reuse a password here.
- **No security headers are set by the app** and there is no CSP. A proxy can
  add them; the app will not.
- **The threat model is a host you control.** Nothing in this design assumes the
  network between the browser and the app is hostile — that assumption is what
  `COOKIE_SECURE` and a TLS terminator exist to restore.
