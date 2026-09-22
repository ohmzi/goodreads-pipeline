"""The two XML documents read out of an epub.

Both arrive inside a file downloaded from somewhere else, so both go through
the same guard: a size taken from the archive's own directory, and a refusal of
XML entities. Guarding only the package document would leave the smaller file
in front of it — the one that says where the package document *is* — unguarded.
"""

from __future__ import annotations

import inspect
import zipfile

from app import genres

_CONTAINER = (
    b'<?xml version="1.0"?>'
    b'<container version="1.0" '
    b'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    b'<rootfiles><rootfile full-path="OEBPS/content.opf" '
    b'media-type="application/oebps-package+xml"/></rootfiles></container>'
)

_DC = 'xmlns:dc="http://purl.org/dc/elements/1.1/"'


def _epub(tmp_path, *, opf: bytes, container: bytes = _CONTAINER):
    """An epub, deflated — so an entry can declare far more than it costs."""
    path = tmp_path / "book.epub"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", opf)
    return path


def _opf(*subjects: str) -> bytes:
    items = "".join(f"<dc:subject>{subject}</dc:subject>" for subject in subjects)
    return (
        f'<?xml version="1.0" encoding="utf-8"?>\n'
        f'<package {_DC} version="3.0"><metadata>{items}</metadata></package>'
    ).encode()


def _read(path):
    return genres._from_embedded({"title": "x"}, str(path))


def test_a_normal_epub_still_yields_its_subjects(tmp_path):
    """The control. A guard that refuses everything is not a fix."""
    assert _read(_epub(tmp_path, opf=_opf("Fantasy", "Epic"))) == ["Fantasy", "Epic"]


def test_an_oversized_package_document_is_refused(tmp_path, monkeypatch):
    """A deflated entry declares megabytes and costs kilobytes on disk."""
    monkeypatch.setattr(genres, "MAX_OPF_BYTES", 1024)
    opf = _opf("Fantasy") + b"<!--" + b"x" * 64_000 + b"-->"
    path = _epub(tmp_path, opf=opf)

    assert path.stat().st_size < len(opf), "not the shape this guards against"
    assert _read(path) == []


def test_an_oversized_container_is_refused(tmp_path, monkeypatch):
    """The file in front of the OPF goes through the same guard."""
    monkeypatch.setattr(genres, "MAX_OPF_BYTES", 1024)
    container = b"<!--" + b"x" * 8192 + b"-->" + _CONTAINER

    assert _read(_epub(tmp_path, opf=_opf("Fantasy"), container=container)) == []


def test_the_size_is_taken_from_the_directory_not_from_the_read(tmp_path):
    """`zipfile` clamps a read to the size the directory declares, so a
    read-then-notice-it-was-long branch could never fire and is not a gap."""
    source = inspect.getsource(genres._read_entry)

    assert "getinfo(" in source
    assert "file_size" in source
    assert "read(" in source


def test_the_container_read_uses_the_same_helper():
    assert "_read_entry(" in inspect.getsource(genres._opf_path)


def test_an_entity_definition_is_refused(tmp_path):
    opf = (
        '<?xml version="1.0"?>\n'
        '<!DOCTYPE package [<!ENTITY boom "boom">]>\n'
        f'<package {_DC}><metadata>'
        "<dc:subject>&boom;</dc:subject></metadata></package>"
    ).encode()

    assert _read(_epub(tmp_path, opf=opf)) == []


def test_a_doctype_alone_is_still_accepted(tmp_path):
    """`<!DOCTYPE` is ordinary in the OPF files this has to keep accepting.

    Refusing it would have failed books that parse perfectly well today, for
    no gain — the thing worth refusing is the entity definition.
    """
    opf = (
        '<?xml version="1.0"?>\n'
        "<!DOCTYPE package>\n"
        f'<package {_DC}><metadata>'
        "<dc:subject>Fantasy</dc:subject></metadata></package>"
    ).encode()

    assert _read(_epub(tmp_path, opf=opf)) == ["Fantasy"]


def test_a_broken_archive_is_still_just_an_empty_answer(tmp_path):
    """The embedded source is the last fallback in the chain, so a bad file
    must not raise — it means "nothing here", not "the lookup failed"."""
    path = tmp_path / "book.epub"
    path.write_bytes(b"this is not a zip file")

    assert _read(path) == []
