"""Reassemble chunk transcripts into one ordered transcript (the reduce step).

Chunks finish out of order, but the reader needs them in order. This buffer holds
completed chunks and only releases the longest *contiguous completed prefix*, so
the displayed transcript always grows front-to-back with no gaps. At each seam it
stitches away the words duplicated by the chunk overlap (see chunking.py).
"""

from __future__ import annotations

import re

_MAX_OVERLAP_WORDS = 30  # how far back we look to dedup an overlap seam
_NORM = re.compile(r"[^\w]+", re.UNICODE)


def _norm(word: str) -> str:
    return _NORM.sub("", word).lower()


class InOrderAssembler:
    """Accepts (index, text) in any order; emits the transcript in order."""

    def __init__(self, total_chunks: int) -> None:
        self._total = total_chunks
        self._next = 0
        self._pending: dict[int, str] = {}
        self._parts: list[str] = []       # stitched text, in order
        self._tail_words: list[str] = []   # recent words, for seam dedup

    def add(self, index: int, text: str) -> str:
        """Record a finished chunk. Returns the newly appended transcript text.

        The return value is the delta that just became visible (possibly empty if
        this chunk fills a later gap and is not yet contiguous).
        """
        self._pending[index] = text.strip()
        delta_parts: list[str] = []
        while self._next in self._pending:
            stitched = self._stitch(self._pending.pop(self._next))
            if stitched:
                self._parts.append(stitched)
                delta_parts.append(stitched)
            self._next += 1
        return " ".join(delta_parts)

    def _stitch(self, text: str) -> str:
        """Drop the leading words of `text` that duplicate the trailing words seen."""
        words = text.split()
        if not words or not self._tail_words:
            self._remember(words)
            return text.strip()

        max_k = min(len(self._tail_words), len(words), _MAX_OVERLAP_WORDS)
        overlap = 0
        for k in range(max_k, 0, -1):
            tail = [_norm(w) for w in self._tail_words[-k:]]
            head = [_norm(w) for w in words[:k]]
            if tail == head:
                overlap = k
                break

        kept = words[overlap:]
        self._remember(kept)
        return " ".join(kept)

    def _remember(self, words: list[str]) -> None:
        self._tail_words.extend(words)
        if len(self._tail_words) > _MAX_OVERLAP_WORDS:
            self._tail_words = self._tail_words[-_MAX_OVERLAP_WORDS:]

    @property
    def transcript(self) -> str:
        return " ".join(self._parts)

    @property
    def done(self) -> bool:
        return self._next >= self._total
