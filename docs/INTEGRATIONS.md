# Integrations

The services this app drives, what each one is for, and the settings each needs
on *its own* side. Nothing here is configured by this repository — these are
changes made in the other services' own UIs and config files, recorded because
they are invisible from this codebase and would otherwise be silently
re-broken.

How this app reaches them (URLs, credentials, the path each one sees the
library at) is [CONFIGURATION.md](CONFIGURATION.md). Why the paths differ per
service is [ARCHITECTURE.md](ARCHITECTURE.md), *The path namespace problem*.

## What each service does here

| Service | Role in the pipeline | Stage |
|---|---|---|
| **Shelfmark** | Searches indexers and downloads ebooks and audiobooks | `acquire_ebook`, `acquire_audiobook` |
| **SABnzbd** | The usenet downloader Shelfmark hands transfers to | (behind Shelfmark) |
| **Kavita** | Ebook library, rescanned after placement | `index`, `verify` |
| **BookLore** | Ebook library, rescanned after placement | `index`, `verify` |
| **Grimmory** | Ebook library, rescanned after placement | `index`, `verify` |
| **Audiobookshelf** | Audiobook library, rescanned after placement | `index`, `verify` |
| **Open Notebook** | Receives each ebook as a source in a per-category notebook | `notebook`, `verify` |
| **Goodreads** | The shelf itself: read by `discover`, written by `shelve` | `discover`, `shelve` |

Each library app is only ever asked to **rescan** a tree it already mounts. No
app imports, uploads, or re-stores anything — that is what keeps one file per
book. See [ARCHITECTURE.md](ARCHITECTURE.md), *Why the same-filesystem rename
is the other half of this*.

## Settings that must be right on the other side

Three of these were silent misconfigurations rather than code faults, and all
three are worth knowing about because nothing in this app's UI reveals them.

### Shelfmark

- **`pdf` was missing from `SUPPORTED_FORMATS`.** It ships with
  epub/mobi/azw3/fb2/djvu/cbz/cbr. Shelfmark would find a book and then refuse
  it with *"format not supported (.pdf). Enable in Settings"*. Add `pdf`.
- **AudiobookBay ships disabled with no hostname.** Enabling it
  (`ABB_ENABLED=true`, `ABB_HOSTNAME=audiobookbay.lu`) took *Atomic Habits* and
  *The 7 Habits* from **0 audiobook releases to 7 and 8**. A single Prowlarr
  indexer had 0–1 audiobooks per title against 27–30 ebooks, which is why so
  many books looked like they had no audiobook at all.
- **The audiobook destination must actually be mounted.** A dev compose file
  mounted only `/books` while `plugins/downloads.json` set
  `DESTINATION_AUDIOBOOK: "/audiobook"`, so every audiobook transfer wrote into
  the container's writable layer and vanished on the next recreate. Mount the
  audiobooks tree at `/audiobook` and verify it is writable from inside.
- **`DEFAULT_RELEASE_SOURCE` should be `libgen`, not `direct_download`.** Anna's
  Archive is unparseable by current Shelfmark, so `direct_download` returns zero
  releases. That setting only chooses which tab the UI opens on —
  `GET /api/releases?provider=manual` searches every enabled source regardless —
  so ebook acquisition was never actually blocked by it, but the UI opening on a
  dead source is misleading.

### SABnzbd

- **The `audiobook` category must complete into a staging folder**, not into the
  Audiobookshelf library. It pointed at `/data/<library>/Complete/Audiobooks/`,
  the live ABS library root, so raw usenet release names became library entries
  before anything organised them. Repoint it at `…/Audiobooks/.incoming/`.
- **`FILE_ORGANIZATION_AUDIOBOOK` must be `organize`, not `rename`.** `rename`
  drops files flat instead of building `Author/Title`.

## The one that was actively harmful

Shelfmark's audiobook search returns whatever Prowlarr gives it, and Prowlarr
was answering audiobook queries with **films**. The ranker picked the best title
match, so it queued `Five.Feet.Apart.2019.2160p.MA.WEB-DL…` — 24.6 GB — as an
audiobook. **88.5 GB of movies** accumulated in the audiobook staging folder
before this was caught.

The category filter exists because of it: releases are now rejected on the
indexer's own Newznab category, on size, and on video-release name patterns
*before* ranking, which is why `reject_reason()` checks the indexer's category
before anything else.

## Why this app places audiobooks itself

The obvious design was to let Shelfmark organise audiobooks into `Author/Title`
and have this app just verify. It does not work in practice: Shelfmark sees the
files at its own mount, while SABnzbd reports their paths in the host's
namespace, and that mismatch defeats Shelfmark's move step. So `place` does the
move itself — from `.incoming`, or from the Audiobooks root for anything
downloaded before that change.

`place` only ever moves entries sitting *directly* in a staging root. Anything
already nested under an author folder is treated as organised, which keeps the
stage idempotent and stops it re-shuffling a library that is already fine.

If two differently-named copies of one book are found, only the best match is
moved and the other is left alone — merging two rips automatically would be
worse than leaving them. `python -m app.cli audit` reports those.
