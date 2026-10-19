"""The eyes: a capture loop on the driving machine, not on the board.

`organ` serves the stream and, more usefully, answers questions about it -
"has this region changed", "wait until it does" - so an agent can follow a
screen without paying image tokens to discover that nothing has happened.

`cursor` locates the pointer from pixels alone, for calibrating open-loop moves
on a target that will not tell you where its cursor is.
"""
from .organ import Organ, changed_px, crop_gray, main as serve

__all__ = ["Organ", "changed_px", "crop_gray", "serve"]
