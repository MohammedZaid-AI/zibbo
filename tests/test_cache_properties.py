"""Property-based invariants for the cache.

The safety-critical one: deserializing *any* byte string returns a value or ``None``,
never an exception. A corrupt or adversarial cache entry must degrade to a miss, so this
mirrors the document extractors' "never raises on random bytes" property.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from gateway.cache.models import CachedTransformation, CacheKey, content_hash

pytestmark = pytest.mark.property


@given(st.binary(max_size=2048))
def test_deserializing_arbitrary_bytes_never_raises(raw: bytes) -> None:
    result = CachedTransformation.from_bytes(raw)
    assert result is None or isinstance(result, CachedTransformation)


_TEXT = st.text(max_size=200)
_STEPS = st.lists(st.text(max_size=20), max_size=5).map(tuple)


@given(
    name=_TEXT,
    version=_TEXT,
    content_type=_TEXT,
    content=_TEXT,
    steps=_STEPS,
    ob=st.integers(min_value=0, max_value=10**9),
    tb=st.integers(min_value=0, max_value=10**9),
    ot=st.integers(min_value=0, max_value=10**9),
    tt=st.integers(min_value=0, max_value=10**9),
    ms=st.floats(min_value=0, max_value=1e6, allow_nan=False, allow_infinity=False),
)
def test_entry_round_trips_for_any_content(
    name: str,
    version: str,
    content_type: str,
    content: str,
    steps: tuple[str, ...],
    ob: int,
    tb: int,
    ot: int,
    tt: int,
    ms: float,
) -> None:
    entry = CachedTransformation(
        transformation_name=name,
        transformer_version=version,
        content_type=content_type,
        transformed_content=content,
        original_size_bytes=ob,
        transformed_size_bytes=tb,
        original_token_count=ot,
        transformed_token_count=tt,
        steps=steps,
        execution_time_ms=ms,
    )
    restored = CachedTransformation.from_bytes(entry.to_bytes())
    assert restored == entry


@given(st.binary(max_size=1024), st.binary(max_size=1024))
def test_content_hash_is_collision_free_for_distinct_input(a: bytes, b: bytes) -> None:
    if a == b:
        assert content_hash(a) == content_hash(b)
    else:
        assert content_hash(a) != content_hash(b)


_FIELD = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=40)


@given(st.lists(_FIELD, min_size=7, max_size=7, unique=True))
def test_distinct_field_tuples_give_distinct_digests(fields: list[str]) -> None:
    """Two keys that differ in any single field must not share a digest."""
    a = CacheKey(*fields)  # type: ignore[arg-type]
    for i in range(7):
        swapped = list(fields)
        swapped[i] = swapped[i] + "!"  # a value no other field holds
        b = CacheKey(*swapped)  # type: ignore[arg-type]
        assert a.digest() != b.digest()
