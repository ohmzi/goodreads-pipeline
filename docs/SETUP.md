# Setup

Getting a clone to a running instance with a login you can use. Day-to-day
running — upgrading, backups, the maintenance CLI, troubleshooting — is
[OPERATIONS.md](OPERATIONS.md). The full variable reference is
[CONFIGURATION.md](CONFIGURATION.md).

Requirements: docker with the compose plugin, a checkout, a host directory for
the book library, one for the audiobook library, and a directory for state.

## 1. Clone and configure

```bash
git clone https://github.com/ohmzi/goodreads-pipeline.git goodreads
cd goodreads
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # -> GOODREADS_SECRET_KEY
$EDITOR .env
chmod 600 .env
```

Only one value must be set before the first boot:

| Variable | Why it is required |
|---|---|
| `GOODREADS_SECRET_KEY` | Fernet key for the credentials table and the HMAC key for session tokens. The app refuses to decrypt, sign, or set a password without it. Generate it with the line above; do not reuse a password. |

`GOODREADS_USER_ID` is optional — it is auto-detected from the stored browser
session, and a value set in the UI takes precedence. Everything else has a
working default. See [CONFIGURATION.md](CONFIGURATION.md) for the full set,
including the variables that live in compose rather than `.env`.

### Library paths and networks

`docker-compose.yml` is deliberately portable: relative volume placeholders
(`./data`, `./books`, `./audiobooks`), no pinned subnet, and no external
networks, so a clone runs unedited. The host-specific half belongs in
`docker-compose.override.yml`, which is gitignored and which compose merges
automatically:

| Container path | What belongs there |
|---|---|
| `/data` | state: the database, the Goodreads session file, the browser profile, an optional `categories.yml` override |
| `/books` | the ebook library tree. Mounted read-write: `place` renames into it. |
| `/audiobooks` | the audiobook library tree. Same. |

Both library roots must be on the same filesystem as the staging directory the
downloader writes into, because `place` renames and raises rather than falling
back to a copy.

Joining the external networks BookLore, Grimmory, Open Notebook and the media
services sit on is what lets the app reach them by container name. A host whose
docker address pools are exhausted fails with *"all predefined address pools
have been fully subnetted"*, and a pin that collides with the LAN or with
another hypervisor's bridge fails similarly — pin a private /24 of your
choosing in the override file and leave it.

Passing any explicit `-f` disables automatic loading of the override file, so
keep it off the command line.

## 2. Start it

```bash
docker compose up -d --build
```

`8091` is the only published port. The noVNC desktop has none: x11vnc binds to
loopback inside the container and the app bridges to it over a session-checked
WebSocket at `/vnc/ws`.

**`8091` is published on every interface by default. That is more exposed than
it sounds** — ufw does not cover a published port, because docker's own iptables
rules are evaluated before the firewall's — so it is worth narrowing with
`PUBLISH_HOST` in `.env`. Two things to know before you do:

- **Narrowing it wrongly takes the app offline.** A binding to one address
  answers only on that address, and whatever sits in front then serves a 502.
- **A proxy running in a container dials you as its bridge gateway**
  (`172.x.0.1`), never as `127.0.0.1`. So `127.0.0.1` is correct only for a
  proxy or tunnel running on the host itself.

Once a front end is in place, find out where it reaches you from and set the
value to match:

```bash
docker compose logs goodreads | grep 'GET /login'
```

```
PUBLISH_HOST=0.0.0.0       # every interface — the safe default
PUBLISH_HOST=127.0.0.1     # a proxy or tunnel on the host, or an SSH tunnel
PUBLISH_HOST=100.64.0.1    # one VPN interface (Tailscale, WireGuard);
                           # find it with `ip -4 addr show tailscale0`
PUBLISH_HOST=192.168.1.50  # one LAN address
```

Then `docker compose up -d` again for it to take effect. Note that this decides
which of the host's own addresses the port answers on; it does not make the app
unreachable from the internet if a tunnel fronts it.

Open `http://<host>:8091`. There is nothing to sign in with yet.

## 3. Create a login

There is deliberately no default account and no first-visitor-becomes-admin
path. With an empty user table the app starts, logs *"no login exists yet"* as
an error event, and serves the sign-in page to everyone.

```bash
docker compose exec goodreads python -m app.cli set-password <username> --generate
docker compose exec goodreads python -m app.cli users
docker compose exec goodreads python -m app.cli delete-user <username>
```

`--generate` prints a strong password once and stores only a scrypt hash, so it
never lands in scrollback or a log. Omit it to type your own; anything under 12
characters is refused, and so is running the command at all without
`GOODREADS_SECRET_KEY`. Setting or changing a password rotates the session
epoch, which signs out any existing session for that user.

## 4. First run in the UI

1. **Services** — enter each credential, press **Test** until every service
   reports ✓.
2. **Goodreads** — press *Start browser*, sign in (Amazon OTP and CAPTCHA
   included), then *Save session*. This is a one-off; see
   [SECURITY.md](SECURITY.md) for why it works this way and what it stores.
3. **Sweep now**, or wait for the poll interval.

Enter the credentials before the first sweep reaches `index`. A missing key
raises `ClientError` with kind `auth`, and `auth` failures are not forgiven on a
clock the way an unreachable service is: they count against the stage's attempt
cap (5 for `acquire_*`, 4 for `index` and `notebook`) and park the book. A book
parked that way is recovered with *Retry all* on the failure group, not by
restarting the container.

The services this expects to find, and the settings each of them needs on its
own side, are in [INTEGRATIONS.md](INTEGRATIONS.md).

## Secrets on disk

`.env` is the only place the secret key exists. It is mode 600, and
`.gitignore` excludes `.env`, `.env.*` (with `.env.example` re-included) and
compose override files, so a clone carries the example and never a key.
`.dockerignore` keeps `.env` and `data/` out of the image as well.

The database under `/data` is gitignored for the same reason and must stay that
way. It holds the encrypted credential blobs, the user table with scrypt
password hashes, and the Goodreads user id in the `settings` table.

## Running without the container

Everything above assumes compose. The app itself does not know where it runs:
it reads plain environment variables at import (`app/config.py:21-22`), and
`uvicorn app.main:app` is the entire entrypoint. What makes a bare-metal run
awkward is not the app but the Goodreads login, which needs a real browser and
a display.

Nothing in the repository installs or tests this path. There is no CI, no test
suite, no packaging metadata and no venv — `requirements.txt` is the only
dependency statement and the image is the only thing that consumes it. The
steps below are a development path, not a second supported deployment.

### Dependencies

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8090
```

The last line is the image's own command with the display step dropped
(`Dockerfile:46`). The image hardcodes `--host 0.0.0.0 --port 8090`, which is
why `BIND` and `PORT` are inert (`app/config.py:72-73`). Outside the container
uvicorn's own flags are where the listen address is chosen, and those two
variables stay just as dead.

`0.0.0.0` suits the image because the published port is the only route in. On a
host the same bind answers on every interface the host has, unless you pass
`127.0.0.1` or firewall it.

### Environment

`env_file` is what turns `.env` into process environment, and `python-dotenv`
is not in `requirements.txt`, so the app never reads the file itself. Outside
compose the values have to be exported:

```bash
set -a; . ./.env; set +a    # .env is plain KEY=value; nothing else parses it
```

Two groups are not optional, because their defaults only make sense inside the
image.

| Variable | Default | Why the default does not work here |
|---|---|---|
| `GOODREADS_SECRET_KEY` | empty | Required in every deployment: no credential can be stored and no session issued without it. |
| `DATA_DIR` | `/data` | An absolute container path. Holds `goodreads.db`, `goodreads_state.json` and `browser-profile/`. |
| `BOOKS_ROOT` | `/books` | Same. The staging dir is derived from it. |
| `AUDIOBOOKS_ROOT` | `/audiobooks` | Same. |
| `OPENNOTEBOOK_LIBRARY_ROOT` | `/app/data/uploads/library` | Open Notebook's view of the books tree. A value that is not how Open Notebook actually mounts it makes every `notebook` call refuse the path. |
| `SHELFMARK_URL` … `SABNZBD_URL` | `http://kavita:5000`, `http://shelfmark:8084`, and so on | Compose service names. They do not resolve outside compose, so each service needs its reachable URL. |

The four storage variables are the ones compose fills in under `environment:`
(`docker-compose.yml:17-23`); the key arrives through `env_file`. Either way the
value has to exist in the process environment, which is what `.env` stops doing
once compose is out of the picture. `ABS_LIBRARY_ROOT` is not among them and is
a placeholder in every deployment, container or not — see
[CONFIGURATION.md](CONFIGURATION.md), *Library roots*.

The remaining knobs — `MIN_FREE_SPACE_GB`, `POLL_INTERVAL`, `COOKIE_SECURE`,
`TRUST_PROXY` — already default to the values the example file sets (and
`TRUST_PROXY`, absent from that file, defaults to `0`), so they need exporting
only to differ.

### The interactive login

The app launches Chromium itself, on a display number fixed in the code, and
reaches the VNC server over loopback. None of it is configurable:

| What | Value | Where |
|---|---|---|
| X display | `:99` | `app/login.py:22`, `docker/start-vnc.sh:13` |
| Screen geometry | `1280x900x24` | `docker/start-vnc.sh:14` |
| Xvfb TCP listener | disabled (`-nolisten tcp`) | `docker/start-vnc.sh:26` |
| x11vnc RFB port | `5900`, loopback only (`-localhost -nopw`) | `docker/start-vnc.sh:15`, `:38` |
| The app's bridge target | `127.0.0.1:5900` | `app/main.py:44` |
| noVNC client root | `/usr/share/novnc` | `app/main.py:42` |

The script also *tries* to confirm the loopback bind with `ss`
(`docker/start-vnc.sh:48`), but `iproute2` is not installed in this image, so
that branch is skipped and has never run. The `-localhost` flag is the actual
control; install `iproute2` if you want the confirmation to be real. If you do,
note that `novnc` hard-depends on `websockify` on jammy — **do not purge it**,
because the purge takes `novnc` with it and `/usr/share/novnc` is what
`/vnc/{asset}` serves. `websockify` is installed and simply never started.

So the host needs Playwright's browser and the same four packages the image
installs (`Dockerfile:21-28`):

```bash
sudo apt-get install -y xvfb x11vnc novnc procps
playwright install chromium      # --with-deps also apt-installs Chromium's libs
```

`playwright install` is pinned by `requirements.txt` to `playwright==1.49.1`,
which is deliberately the version the base image carries (`Dockerfile:4`).
`novnc` has to land at `/usr/share/novnc`: the path is a literal
(`app/main.py:42`) and `/vnc/{asset}` answers 503 *"noVNC is not installed in
this image"* when it is missing, which shows up as a blank login panel with the
rest of the app fine. `procps` supplies the `pgrep` guards the script uses to
avoid starting a second Xvfb (`docker/start-vnc.sh:19`, `:31`).

Do not run `docker/start-vnc.sh` by hand — `app/main.py` starts it during
startup (`app/main.py:374-385`) from its path beside `app/`. It is best-effort:
a missing binary logs `[vnc] ... not installed` and everything but the login
page keeps working. To check the display came up:

```bash
ss -tln | grep ':5900'    # want 127.0.0.1:5900, not 0.0.0.0:5900
```

The login procedure is unchanged: *Start browser* in the UI, sign in, *Save
session*, and the cookies land in `goodreads_state.json` under `DATA_DIR`
(`app/goodreads.py:91-92`).

### What the container provides that this does not

| Given up | Source |
|---|---|
| Chromium and every shared library it needs, present and version-matched to `playwright==1.49.1` | the Playwright base image (`Dockerfile:1-4`) |
| The healthcheck: unauthenticated `GET /api/health` on `localhost:8090`, 60 s interval, 10 s timeout, 3 retries, 30 s start period | `docker-compose.yml:38-44` |
| `restart: unless-stopped` | `docker-compose.yml:45` |
| One published port and nothing else | `docker-compose.yml:24-29` |

State, database and library files are the same files under the same variables,
and `app/cli.py` runs the same way without the `docker compose exec` prefix,
from the repository root.
