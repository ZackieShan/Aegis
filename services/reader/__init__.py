"""The reader: library documents opened for reading on Aegis's own page, with
a shelf, highlights and notes — and the kitchen, which sets a recipe out for
cooking from. See ``docs/reader.md``.
"""

from . import content, kitchen, shelf
from .content import ReaderError

__all__ = ["content", "kitchen", "shelf", "ReaderError"]
