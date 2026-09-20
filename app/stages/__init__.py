"""The pipeline stages, and the registry the scheduler drives."""

from __future__ import annotations

from typing import Callable

from .. import models
from . import acquire, classify, discover, index, notebook, place, shelve, verify

Runner = Callable[[dict], models.StageResult]

REGISTRY: dict[str, Runner] = {
    "classify": classify.run,
    "acquire_ebook": acquire.run_ebook,
    "acquire_audiobook": acquire.run_audiobook,
    "place": place.run,
    "index": index.run,
    "notebook": notebook.run,
    "verify": verify.run,
    "shelve": shelve.run,
}

__all__ = ["REGISTRY", "discover", "classify", "place", "shelve", "verify"]
