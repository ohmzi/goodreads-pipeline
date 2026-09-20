"""Maintenance commands for the libraries that already exist.

    python -m app.cli audit              # read-only; changes nothing
    python -m app.cli rename             # dry run; prints what it would do
    python -m app.cli rename --apply     # actually does it

Both default to doing nothing. The audiobook folder on this host is genuinely
messy — flat files, `__yEnc` suffixes, `-6a6dea` disambiguators — and a
half-applied bulk rename would be worse than the mess. So `rename` proposes,
you read the list, and only `--apply` touches the disk.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import re
import sys
from collections import defaultdict
from pathlib import Path

from .config import settings
from .db import db
from .pathing import AUDIO_EXTS, is_junk, is_media_file, sanitize_component

CHUNK = 1024 * 1024

#: Below this, a file is too small to be a real audiobook track — a fragment
#: rather than a book. The AppleDouble `._` companions that litter this library
#: are excluded by name, not by size (see `pathing.is_junk`).
MIN_AUDIO_BYTES = 100 * 1024


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------
def cmd_audit(args: argparse.Namespace) -> int:
    duplicates = _duplicate_audio(settings.audiobooks_root)
    collisions = _title_collisions(settings.books_root)

    print(f"\n=== duplicate audiobooks under {settings.audiobooks_root} ===")
    if not duplicates:
        print("  none found")
    for digest, paths in duplicates:
        total = sum(p.stat().st_size for p in paths) / (1024 ** 2)
        print(f"\n  {digest[:12]}  {len(paths)} copies, {total:.0f} MB")
        for path in paths:
            print(f"    {path.relative_to(settings.audiobooks_root)}")

    print(f"\n=== titles appearing in more than one place under {settings.books_root} ===")
    if not collisions:
        print("  none found")
    for title, paths in collisions:
        print(f"\n  '{title}'")
        for path in paths:
            print(f"    {path.relative_to(settings.books_root)}")

    print("\nNothing was changed. This command is read-only.\n")
    return 0


def _duplicate_audio(root: Path) -> list[tuple[str, list[Path]]]:
    """Exact duplicates, found by size then a cheap head+tail digest.

    Hashing whole audiobooks would take minutes and the point is only to catch
    identical copies, so size-gate first and hash at most 2 MB per file.
    """
    if not root.exists():
        return []
    by_size: dict[int, list[Path]] = defaultdict(list)
    for path in root.rglob("*"):
        if not is_media_file(path, AUDIO_EXTS, min_bytes=MIN_AUDIO_BYTES):
            continue
        try:
            by_size[path.stat().st_size].append(path)
        except OSError:
            continue

    groups: list[tuple[str, list[Path]]] = []
    for size, paths in by_size.items():
        if len(paths) < 2 or size == 0:
            continue
        by_digest: dict[str, list[Path]] = defaultdict(list)
        for path in paths:
            try:
                by_digest[_head_tail_digest(path)].append(path)
            except OSError:
                continue
        for digest, same in by_digest.items():
            if len(same) > 1:
                groups.append((digest, sorted(same)))
    groups.sort(key=lambda g: -len(g[1]))
    return groups


def _head_tail_digest(path: Path) -> str:
    size = path.stat().st_size
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        hasher.update(fh.read(CHUNK))
        if size > 2 * CHUNK:
            fh.seek(-CHUNK, 2)
            hasher.update(fh.read(CHUNK))
    return hasher.hexdigest()


def _title_collisions(root: Path) -> list[tuple[str, list[Path]]]:
    if not root.exists():
        return []
    by_title: dict[str, list[Path]] = defaultdict(list)
    for path in root.rglob("*"):
        if is_junk(path) or not path.is_file():
            continue
        if path.suffix.lower() not in (".epub", ".mobi", ".azw3", ".pdf"):
            continue
        by_title[_normalise_title(path.stem)].append(path)
    found = [(t, sorted(p)) for t, p in by_title.items() if len(p) > 1 and t]
    found.sort(key=lambda item: item[0])
    return found


def _normalise_title(stem: str) -> str:
    text = re.sub(r"[-_]?(epub|mobi|azw3|pdf|retail|v\d+)$", "", stem, flags=re.I)
    text = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------
# rename
# --------------------------------------------------------------------------
def cmd_rename(args: argparse.Namespace) -> int:
    root = settings.audiobooks_root
    if not root.exists():
        print(f"{root} does not exist", file=sys.stderr)
        return 1

    moves = _proposed_audio_moves(root)
    if not moves:
        print(f"No audiobook folders under {root} need renaming.")
        return 0

    print(f"\n{len(moves)} proposed move(s) under {root}:\n")
    for source, destination, why in moves:
        print(f"  {source.name}")
        print(f"    -> {destination.relative_to(root)}")
        print(f"       via {why}")

    if not args.apply:
        print("\nDry run. Nothing was changed. Re-run with --apply to do it.\n")
        return 0

    print("\nApplying...\n")
    applied = 0
    for source, destination, _why in moves:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            print(f"  skip (exists): {destination.relative_to(root)}")
            continue
        try:
            source.rename(destination)
            applied += 1
            print(f"  moved: {destination.relative_to(root)}")
        except OSError as exc:
            print(f"  FAILED {source.name}: {exc}", file=sys.stderr)

    print(f"\n{applied} of {len(moves)} applied. Rescan Audiobookshelf afterwards.\n")
    return 0


def _proposed_audio_moves(root: Path) -> list[tuple[Path, Path, str]]:
    """Entries that are not already `Author/Title`, and where they should go."""
    moves: list[tuple[Path, Path, str]] = []

    for entry in sorted(root.iterdir()):
        if is_junk(entry):
            continue

        if is_media_file(entry, AUDIO_EXTS, min_bytes=MIN_AUDIO_BYTES):
            # A loose audio file at the root: give it a folder of its own.
            tag_author, tag_title = _tags_for(entry)
            author, title, source = _resolve_identity(entry.stem, tag_author, tag_title)
            if not title:
                continue
            destination = root / sanitize_component(author) / sanitize_component(title)
            moves.append((entry, destination, source))
            continue

        if not entry.is_dir():
            continue

        # Already Author/Title? Then its children are the book folders.
        if _looks_like_author_dir(entry):
            continue

        tag_author, tag_title = _tags_from_dir(entry)
        author, title, source = _resolve_identity(entry.name, tag_author, tag_title)
        if not title:
            continue
        author = author or "Unknown Author"
        destination = root / sanitize_component(author) / sanitize_component(title)
        if destination == entry:
            continue
        moves.append((entry, destination, source))

    return moves


def _looks_like_author_dir(path: Path) -> bool:
    """Heuristic: a folder whose children are themselves folders of audio."""
    children = [c for c in path.iterdir() if not is_junk(c)]
    if not children:
        return False
    dirs = [c for c in children if c.is_dir()]
    if not dirs:
        return False
    return all(
        any(
            is_media_file(f, AUDIO_EXTS, min_bytes=MIN_AUDIO_BYTES)
            for f in d.rglob("*")
        )
        for d in dirs
    )


def _tags_from_dir(path: Path) -> tuple[str, str]:
    for candidate in sorted(path.rglob("*")):
        if is_media_file(candidate, AUDIO_EXTS, min_bytes=MIN_AUDIO_BYTES):
            author, title = _tags_for(candidate)
            if title:
                return author, title
    return "", ""


def _tags_for(path: Path) -> tuple[str, str]:
    """(artist, album) from embedded tags, or ("", "")."""
    try:
        from mutagen import File as MutagenFile
    except ImportError:
        return "", ""
    try:
        audio = MutagenFile(path, easy=True)
    except Exception:  # noqa: BLE001 - a broken tag must not stop the audit
        return "", ""
    if audio is None or not getattr(audio, "tags", None):
        return "", ""
    tags = audio.tags
    artist = _first(tags, ("albumartist", "artist", "author", "performer"))
    album = _first(tags, ("album", "title", "albumtitle"))
    return artist, album


def _first(tags, keys) -> str:
    for key in keys:
        try:
            value = tags.get(key)
        except Exception:  # noqa: BLE001
            continue
        if value:
            if isinstance(value, list):
                value = value[0]
            return str(value).strip()
    return ""


def _resolve_identity(stem: str, tag_author: str, tag_title: str) -> tuple[str, str, str]:
    """Decide the author and title for one audiobook. Returns (author, title, source).

    Tags are usually the better source, but audiobook `artist` is frequently
    the *narrator* — "Aldous Huxley - 2008 - Brave New World" tags as
    `Michael York`. So when the filename yields an author that does not appear
    anywhere in the tag author, the tag is almost certainly a narrator credit
    and the filename wins.

    When the tag author *is* present in the filename — "Based on a True Story -
    Norm Macdonald", which tags correctly as `Norm Macdonald` despite the
    title-first name — the tag is trusted, because that reversed order is
    exactly the case filename parsing gets wrong.
    """
    file_author, file_title = _from_filename(stem)

    if tag_title:
        # If the filename's leading segment *is* the tag's title, the filename
        # is in "Title - Author" order — "Based on a True Story - Norm
        # Macdonald" — and the tags have it right despite looking reversed.
        title_first = file_author and file_author.lower() == tag_title.strip().lower()
        if title_first:
            return (tag_author or file_author), tag_title, "tags (title-first filename)"

        # Otherwise, a filename author absent from the tag author usually means
        # the tag is a narrator credit — "Aldous Huxley - 2008 - Brave New
        # World" tags as `Michael York`.
        if file_author and tag_author and file_author.lower() not in tag_author.lower():
            return file_author, file_title or tag_title, "filename (tag author was a narrator)"

        author = tag_author or file_author
        return author or "Unknown Author", tag_title, "tags"

    if file_title:
        return file_author or "Unknown Author", file_title, "filename"
    return "", "", ""


def _from_filename(stem: str) -> tuple[str, str]:
    """(author, title) parsed from a filename — author may be empty."""
    cleaned = _strip_debris(stem)

    # "Author - 2008 - Title"
    match = re.match(r"^(?P<a>[^-]{2,60}?)\s+-\s+(?:19|20)\d{2}\s+-\s+(?P<t>.+)$", cleaned)
    if match and _plausible_author(match.group("a")):
        return match.group("a").strip(), _clean_title(match.group("t"))

    # "Author - Title"
    match = re.match(r"^(?P<a>[^-]{2,60}?)\s+-\s+(?P<t>.+)$", cleaned)
    if match and _plausible_author(match.group("a")):
        return match.group("a").strip(), _clean_title(match.group("t"))

    # "NN - Title" and friends: a track number is not an author.
    return "", _clean_title(cleaned)


def _plausible_author(candidate: str) -> bool:
    text = candidate.strip()
    if len(text) < 3 or text.isdigit():
        return False
    if not re.search(r"[A-Za-z]{2}", text):
        return False
    # Track prefixes like "02" or "1." arrive here as bogus authors.
    return not re.match(r"^\d+\s*[-.)\]]", text)


def _strip_debris(stem: str) -> str:
    """Remove usenet/p2p noise so the structure underneath is visible."""
    text = re.sub(r"__yEnc", " ", stem, flags=re.I)
    text = re.sub(r"[-_]{1,2}[0-9a-f]{6,}$", "", text, flags=re.I)
    text = re.sub(r"\s*\{[^}]*\}\s*$", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -_.")


def _clean_title(text: str) -> str:
    text = re.sub(r"__yEnc", " ", text, flags=re.I)
    text = re.sub(r"[-_]{1,2}[0-9a-f]{6,}$", "", text, flags=re.I)
    text = re.sub(r"\s*\{[^}]*\}\s*$", "", text)
    text = re.sub(r"^\s*\d{1,3}\s*[-_.)\]]\s*", "", text)
    return re.sub(r"\s+", " ", text).strip(" -_.")


def cmd_report(args: argparse.Namespace) -> int:
    """Where every book got to, and why the failures failed."""
    from collections import Counter, defaultdict

    from . import models

    books = db().list_books()

    def stage_of(book, name):
        return (book.get("stages") or {}).get(name) or {}

    def is_complete(book):
        return all(
            stage_of(book, s).get("status") in (models.OK, models.SKIPPED)
            for s in models.STAGES
        )

    def parked_stage(book):
        """The stage that gave up — the first FAILED one past its retry cap."""
        for stage in models.STAGES:
            row = stage_of(book, stage)
            if row.get("status") == models.FAILED and int(row.get("attempts") or 0) >= \
                    models.MAX_ATTEMPTS.get(stage, 3):
                return stage, row
        return None, None

    complete = [b for b in books if is_complete(b)]
    parked = [(b,) + parked_stage(b) for b in books if parked_stage(b)[0]]
    in_flight = [b for b in books if not is_complete(b) and not parked_stage(b)[0]]

    print(f"\n{'=' * 74}")
    print(f"  goodreads report — {len(books)} book(s) on the to-read shelf")
    print(f"{'=' * 74}")
    print(f"  completed    {len(complete):4}   every stage done")
    print(f"  in progress  {len(in_flight):4}   still working")
    print(f"  failed       {len(parked):4}   gave up after their retries")
    if not args.show_all and not args.failed_only:
        print("\n  (use --show-all to list completed books, --failed-only for just failures)")

    if complete and not args.failed_only:
        print(f"\n--- completed ({len(complete)}) ---")
        shown = complete[: args.limit] if args.limit else complete
        for b in shown:
            shelf = stage_of(b, "shelve").get("artifact") or "?"
            print(f"  ✓ {b['title'][:44]:46} {b['category']:11} -> {shelf}")
        if len(shown) < len(complete):
            print(f"  ... and {len(complete) - len(shown)} more (--limit 0 for all)")

    if parked:
        print(f"\n--- failed ({len(parked)}), grouped by reason ---")
        grouped = defaultdict(list)
        for book, stage, row in parked:
            # Grouped by recorded service and kind as well as the message, so
            # the same error text against two services does not read as one
            # problem — which is the point of having recorded it at all.
            grouped[(
                stage,
                (row.get("service") or "").strip(),
                (row.get("failure_kind") or "").strip(),
                (row.get("detail") or "no detail recorded").strip(),
            )].append(book)
        for (stage, service, kind, detail), members in sorted(
            grouped.items(), key=lambda kv: -len(kv[1])
        ):
            where = f" via {service}" if service else ""
            why = f" ({kind})" if kind else ""
            print(f"\n  [{stage}{where}] {len(members)} book(s){why}")
            print(f"      reason: {detail[:200]}")
            for b in members[:8]:
                print(f"        - {b['title'][:60]}")
            if len(members) > 8:
                print(f"        ... and {len(members) - 8} more")

    if in_flight and not args.failed_only:
        print(f"\n--- in progress ({len(in_flight)}) ---")
        counts = Counter()
        for b in in_flight:
            for stage in models.STAGES:
                status = stage_of(b, stage).get("status")
                if status in (models.RUNNING, models.PENDING, models.BLOCKED):
                    counts[f"{stage}:{status}"] += 1
                    break
        for key, n in counts.most_common():
            print(f"  {n:4}  {key}")

    print(f"\n  disk free: {__import__('shutil').disk_usage(str(settings.books_root)).free / 1024**3:.0f} GB")
    print()
    return 0


def cmd_repair(args: argparse.Namespace) -> int:
    """Fix the gaps verification finds, rather than only reporting them.

    Three things go wrong often enough to be worth automating:
      * a source attached to its notebook more than once (the attach endpoint
        appends instead of being idempotent, so retries stacked up links);
      * a source that was created but never attached;
      * a book missing from a service, which a rescan usually fixes.

    Dry-run unless --apply, because the first two write to Open Notebook.
    """
    import json

    from . import models
    from .clients.opennotebook import OpenNotebookClient, to_opennotebook_path
    from .stages import classify
    from .stages.place import placed_paths

    apply_changes = args.apply
    print(f"\n{'Applying' if apply_changes else 'Dry run — nothing will change'}\n")

    # --- Open Notebook link hygiene -------------------------------------
    client = OpenNotebookClient()
    try:
        rows = db().query(
            "SELECT id, title, category FROM books ORDER BY id"
        )
        dup = unattached = ok = 0
        for row in rows:
            ebook = placed_paths(row["id"]).get("ebook")
            if not ebook:
                continue
            try:
                source_id = client.source_exists_for_path(to_opennotebook_path(ebook))
            except Exception as exc:  # noqa: BLE001
                print(f"  !! {row['title'][:40]}: {type(exc).__name__}")
                continue
            if not source_id:
                continue
            _folder, notebook_name = classify.destination_for(row["category"] or "")
            if not notebook_name:
                continue
            notebook = client.notebook_named(notebook_name)
            nid = str((notebook or {}).get("id") or "")
            if not nid:
                continue
            links = client.source_notebooks(source_id)
            count = links.count(nid)
            if count == 1:
                ok += 1
            elif count == 0:
                unattached += 1
                print(f"  {'fixed ' if apply_changes else 'would fix '}"
                      f"{row['title'][:44]:46} not attached to '{notebook_name}'")
                if apply_changes:
                    client.attach(nid, source_id)
            else:
                dup += 1
                print(f"  {'fixed ' if apply_changes else 'would fix '}"
                      f"{row['title'][:44]:46} attached {count}x to '{notebook_name}'")
                if apply_changes:
                    client.detach(nid, source_id)
                    client.attach(nid, source_id)
        print(f"\n  Open Notebook: {ok} correct, {dup} duplicated, {unattached} unattached")
    finally:
        client.close()

    # --- books with a failing stage: queue them for another attempt ------
    # Skip failures that are facts rather than faults. "No audiobook release
    # exists in any configured source" will not become true by trying again,
    # and resetting it also resets everything downstream — which previously
    # knocked 24 already-shelved books back to pending for no reason.
    failing = []
    for row in db().query(
        "SELECT DISTINCT b.id, b.title FROM stage_runs sr JOIN books b ON b.id=sr.book_id "
        "WHERE sr.status='failed' ORDER BY b.id"
    ):
        retryable = None
        for stage in models.STAGES:
            run = db().stage(row["id"], stage) or {}
            if run.get("status") != models.FAILED:
                continue
            if run.get("failure_kind") == "data" and stage.startswith("acquire_"):
                continue          # a fact about the world, not a fault
            retryable = stage
            break
        if retryable:
            failing.append((row, retryable))

    skipped = db().query_one(
        "SELECT COUNT(DISTINCT book_id) n FROM stage_runs WHERE status='failed' "
        "AND failure_kind='data' AND stage LIKE 'acquire_%'"
    )["n"]
    print(f"\n  {len(failing)} book(s) have a retryable failing stage"
          f" ({skipped} more only lack a format that does not exist — left alone)")

    if failing and apply_changes:
        for row, stage in failing:
            db().reset_stage(row["id"], stage)
        print(f"  reset their earliest failing stage so the next sweep retries")
    elif failing:
        print("  re-run with --apply to reset their earliest failing stage")

    print()
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    """Compare what goodreads believes against what Goodreads actually shows."""
    from .reconcile import reconcile

    print(f"\n{'Applying' if args.apply else 'Dry run — nothing will change'}\n")
    result = reconcile(apply_changes=args.apply)

    print(f"  {result['checked']} book(s) recorded as shelved")
    print(f"  {result['drifted']} have drifted from that\n")
    for item in result["details"]:
        print(f"    {item['title'][:44]:46} expected={item['expected'] or '?'}")
        print(f"        actually on: {item['actual'] or 'nothing'}  ({item['why']})")

    if result["drifted"] and args.apply:
        print(f"\n  {result['drifted']} book(s) queued to be re-shelved")
    elif result["drifted"]:
        print("\n  re-run with --apply to queue them for re-shelving")
    print()
    return 0


# --------------------------------------------------------------------------
# users
# --------------------------------------------------------------------------
def cmd_set_password(args: argparse.Namespace) -> int:
    from . import auth

    username = (args.username or "").strip()
    if not username:
        print("a username is required", file=sys.stderr)
        return 2
    if not settings.secret_key:
        print(
            "GOODREADS_SECRET_KEY is not set — sessions cannot be signed. Set it "
            "in .env first.",
            file=sys.stderr,
        )
        return 2

    if args.generate:
        password = auth.generate_password(args.length)
        generated = True
    else:
        password = getpass.getpass("password: ")
        confirm = getpass.getpass("confirm:  ")
        if password != confirm:
            print("passwords do not match", file=sys.stderr)
            return 2
        generated = False

    if len(password) < 12:
        print("refusing a password under 12 characters", file=sys.stderr)
        return 2

    existing = db().get_user(username) is not None
    # A fresh epoch on every password change is what evicts outstanding
    # sessions — without it, an old cookie keeps working after a reset.
    db().set_user(username, auth.hash_password(password), auth.new_epoch())

    if generated:
        # Only ever printed when we generated it, so a password the operator
        # typed themselves never lands in a terminal scrollback or log.
        print(f"\n  username: {username}\n  password: {password}\n")
        print("  Save this now — it is not stored anywhere in recoverable form.")
        if existing:
            print("  Existing sessions for this user have been signed out.\n")
        else:
            print()
    else:
        print(f"\n  password {'updated' if existing else 'set'} for '{username}'")
        if existing:
            print("  Existing sessions for this user have been signed out.")
        print()
    return 0


def cmd_backfill_genres(args: argparse.Namespace) -> int:
    """Re-fetch genres for books that have none, then re-classify them.

    Exists because genre lookup can fail silently: if the GraphQL shape is
    wrong every book stores an empty genre list and the whole shelf quietly
    classifies to the fallback category. This repairs that without re-running
    discovery or touching anything already downloaded.
    """
    import json
    import time as _time

    from . import genres as genres_lookup
    from .stages import classify

    rows = db().query(
        "SELECT id, goodreads_id, title, author, isbn, isbn13 FROM books "
        "WHERE genres IS NULL OR genres = '' OR genres = '[]' ORDER BY id"
    )

    filled = 0
    by_source: dict[str, int] = {}
    still_empty: list[str] = []
    if rows:
        print(f"\nResolving genres for {len(rows)} book(s) across all sources...\n")
        for index, row in enumerate(rows, 1):
            book = dict(row)
            # Now that downloads have landed, the embedded-metadata source can
            # contribute — it reads dc:subject straight out of the epub.
            local = genres_lookup.local_file_for(row["id"])
            found = genres_lookup.lookup(book, file_path=local)
            if found.found:
                db().set_genres(row["id"], json.dumps(found.genres), found.source)
                by_source[found.source] = by_source.get(found.source, 0) + 1
                filled += 1
            else:
                still_empty.append(row["title"])
            if index % 25 == 0 or index == len(rows):
                print(f"  {index}/{len(rows)} — {filled} resolved")
            # These are third-party APIs; keep the pace polite.
            _time.sleep(0.2)
        print(f"\n  {filled} resolved, {len(still_empty)} still empty")
        if by_source:
            print("  by source: " + ", ".join(f"{k}={v}" for k, v in sorted(by_source.items())))
    else:
        print("\nEvery book already has genres — re-checking categories only.")

    # Re-run classification for *every* book, unconditionally. The
    # needs_review flag has to be recomputed even when the category does not
    # change: a book that used to fall back to Fiction and later matched a real
    # rule keeps its stale flag otherwise, and the review pile never drains.
    rules = settings.load_categories()
    changed = 0
    cleared = 0
    for row in db().query("SELECT id, title, category, needs_review, genres FROM books"):
        genres = json.loads(row["genres"] or "[]")
        category, needs_review = classify.resolve_category(genres, rules)
        source = None
        if needs_review:
            # Mirror what the classify stage does, so running this command and
            # letting the scheduler work produce the same answer.
            from_title = classify.resolve_from_title(row["title"], rules)
            if from_title:
                category, needs_review, source = from_title, False, "title"

        if category != row["category"]:
            db().set_category(row["id"], category, needs_review=needs_review)
            if source:
                db().set_genres(row["id"], json.dumps([row["title"]]), source)
            # The category decides the destination folder, so an already-placed
            # book is in the wrong place now — clear `place` and everything
            # downstream so the next sweep moves it.
            db().reset_stage(row["id"], "place")
            changed += 1
        elif needs_review != bool(row["needs_review"]):
            db().set_category(row["id"], category, needs_review=needs_review)
            cleared += int(bool(row["needs_review"]) and not needs_review)

    print(f"  {changed} book(s) re-categorised; {cleared} cleared from needs-review")
    if still_empty:
        print(f"  no genres found for: {', '.join(still_empty[:8])}"
              + (" ..." if len(still_empty) > 8 else ""))
    print()
    return 0


def cmd_users(args: argparse.Namespace) -> int:
    names = db().list_users()
    if not names:
        print("no users defined — nobody can sign in")
        return 0
    print(f"\n{len(names)} user(s):")
    for name in names:
        print(f"  {name}")
    print()
    return 0


def cmd_delete_user(args: argparse.Namespace) -> int:
    if db().get_user(args.username) is None:
        print(f"no such user: {args.username}", file=sys.stderr)
        return 1
    if db().user_count() <= 1:
        print(
            "refusing to delete the last user — that would lock you out of the UI",
            file=sys.stderr,
        )
        return 1
    db().delete_user(args.username)
    print(f"deleted '{args.username}'")
    return 0


# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="goodreads", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser("audit", help="report duplicates and title collisions (read-only)")
    audit.set_defaults(func=cmd_audit)

    rename = sub.add_parser("rename", help="move audiobooks into Author/Title (dry run by default)")
    rename.add_argument("--apply", action="store_true", help="actually perform the moves")
    rename.set_defaults(func=cmd_rename)

    setpw = sub.add_parser("set-password", help="create or change a login")
    setpw.add_argument("username")
    setpw.add_argument(
        "--generate", action="store_true", help="generate a strong password and print it"
    )
    setpw.add_argument("--length", type=int, default=20, help="generated password length")
    setpw.set_defaults(func=cmd_set_password)

    users = sub.add_parser("users", help="list logins")
    users.set_defaults(func=cmd_users)

    backfill = sub.add_parser(
        "backfill-genres",
        help="re-fetch missing genres and re-categorise (fixes an all-fallback shelf)",
    )
    backfill.set_defaults(func=cmd_backfill_genres)

    report = sub.add_parser("report", help="what completed, what failed, and why")
    report.add_argument("--failed-only", action="store_true", help="show only failures")
    report.add_argument("--show-all", action="store_true", help="suppress the hint pointing at the completed listing")
    report.add_argument("--limit", type=int, default=0, help="cap the completed listing")
    report.set_defaults(func=cmd_report)

    repair = sub.add_parser(
        "repair",
        help="fix the gaps verify finds (notebook links, stuck books); dry run by default",
    )
    repair.add_argument("--apply", action="store_true", help="actually make the changes")
    repair.set_defaults(func=cmd_repair)

    recon = sub.add_parser(
        "reconcile",
        help="check goodreads's records against Goodreads and queue retries (dry run by default)",
    )
    recon.add_argument("--apply", action="store_true", help="queue drifted shelf moves for retry")
    recon.set_defaults(func=cmd_reconcile)

    delete = sub.add_parser("delete-user", help="remove a login")
    delete.add_argument("username")
    delete.set_defaults(func=cmd_delete_user)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
