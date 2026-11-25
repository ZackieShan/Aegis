"""Translation between languages, on this machine.

``translate_text`` and ``translate_image`` do the work (:mod:`.core`), with a
dedicated translation model when one is installed and the ordinary chat
model otherwise (:mod:`.engine`). See ``docs/translate.md``.
"""

from .core import MAX_CHARS, TranslateError, language_of, translate_image, translate_text

__all__ = ["MAX_CHARS", "TranslateError", "language_of", "translate_image", "translate_text"]
