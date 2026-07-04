"""Transcriptor — upload a media file, get a transcription.

Chunked (map), transcribed independently (fan-out), reassembled in order (reduce),
with per-chunk lineage. See docs/ for the design and decision log.
"""

__version__ = "0.1.0"
