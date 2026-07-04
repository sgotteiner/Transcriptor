"""In-order reassembly: contiguous-prefix emission and overlap-seam dedup."""

from app.core.reassembly import InOrderAssembler


def test_emits_only_contiguous_prefix():
    a = InOrderAssembler(3)
    assert a.add(2, "c") == ""       # arrives early, held back
    assert a.done is False
    assert a.add(0, "a") == "a"      # prefix now available
    assert a.add(1, "b") == "b c"    # 1 unlocks 1 and the buffered 2
    assert a.transcript == "a b c"
    assert a.done is True


def test_stitches_away_overlap_words():
    a = InOrderAssembler(2)
    a.add(0, "the quick brown fox")
    delta = a.add(1, "brown fox jumps over")  # "brown fox" duplicates the seam
    assert delta == "jumps over"
    assert a.transcript == "the quick brown fox jumps over"


def test_no_false_dedup_without_real_overlap():
    a = InOrderAssembler(2)
    a.add(0, "hello world")
    delta = a.add(1, "goodbye now")
    assert delta == "goodbye now"
    assert a.transcript == "hello world goodbye now"
