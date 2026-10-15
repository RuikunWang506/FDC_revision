"""Tests for perceptual hashing and the brute-force duplicate baseline.

No database, no network. The corpus is generated in-process, so the
relationships between images are known by construction rather than
assumed.

Most assertions here are about *ordering* rather than absolute distances.
A test that says "a JPEG copy is within 8 bits" encodes a number that
came from running it once; a test that says "a JPEG copy is closer than
an unrelated image" encodes the property the hash exists to have. The
first breaks when Pillow changes its resampling; the second does not, and
it is the one that would actually be wrong if the hash were broken.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _load_by_path(alias: str, relative: str):
    """Load a project module by file path.

    The project's top-level package is named `py`, which collides with the
    `py.py` shim pytest ships, so `from py.dam... import` cannot resolve.
    Several tests in this directory already work around it the same way.
    """
    spec = importlib.util.spec_from_file_location(alias, _ROOT / relative)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {relative}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


_phash = _load_by_path("fdc_phash", "py/dam/dedupe/phash.py")
_corpus = _load_by_path("fdc_corpus", "py/dam/dedupe/corpus.py")
_search = _load_by_path("fdc_dedupe_search", "py/dam/dedupe/search.py")

phash = _phash.phash
hamming = _phash.hamming
HASH_BITS = _phash.HASH_BITS


@pytest.fixture(scope="module")
def corpus():
    return _corpus.build_corpus(groups=8, seed=7)


@pytest.fixture(scope="module")
def hashes(corpus):
    return [phash(v.image) for v in corpus.variants]


# --- the hash itself -----------------------------------------------------


def test_hash_is_64_bits():
    image = _corpus.base_image(1)
    assert 0 <= phash(image) < (1 << HASH_BITS)


def test_hash_is_deterministic():
    image = _corpus.base_image(2)
    assert phash(image) == phash(image)


def test_same_image_has_distance_zero():
    image = _corpus.base_image(3)
    assert hamming(phash(image), phash(image.copy())) == 0


def test_different_images_are_far_apart():
    a = phash(_corpus.base_image(4))
    b = phash(_corpus.base_image(999))
    assert hamming(a, b) > 10, "unrelated images collided"


# --- the property the whole thing rests on -------------------------------
# Each transformation must leave a copy closer to its original than an
# unrelated image is. If any of these fail, the hash is not perceptual and
# nothing built on top of it means anything.


@pytest.mark.parametrize(
    "transform",
    [
        lambda img: _corpus.jpeg(img, 60),
        lambda img: _corpus.resized(img, 0.5),
        lambda img: _corpus.webp(img, 75),
        lambda img: _corpus.brightened(img, 1.15),
    ],
    ids=["jpeg60", "half", "webp75", "bright115"],
)
def test_transformed_copy_is_nearer_than_an_unrelated_image(transform):
    original = _corpus.base_image(11)
    unrelated = _corpus.base_image(12)

    h_original = phash(original)
    h_copy = phash(transform(original))
    h_unrelated = phash(unrelated)

    assert hamming(h_original, h_copy) < hamming(h_original, h_unrelated)


def test_heavier_compression_is_not_nearer_than_lighter():
    """Degradation should move the hash away, not randomly.

    Stated as "not nearer" rather than "further": at these sizes a mild
    re-encode can leave the hash untouched, and demanding a strict
    increase would be asserting noise.
    """
    original = _corpus.base_image(13)
    h_original = phash(original)
    light = hamming(h_original, phash(_corpus.jpeg(original, 90)))
    heavy = hamming(h_original, phash(_corpus.jpeg(original, 25)))
    assert heavy >= light


def test_dropping_the_dc_term_keeps_brightness_from_dominating():
    """A brightened copy is the case the DC term would have broken.

    Overall brightness lives in coefficient (0,0). Thresholding against a
    median that includes it would let an exposure change flip many bits at
    once, which is exactly the kind of difference this hash is supposed to
    ignore.
    """
    original = _corpus.base_image(14)
    bright = _corpus.brightened(original, 1.3)
    assert hamming(phash(original), phash(bright)) <= 12


# --- brute-force baseline ------------------------------------------------


def test_brute_force_compares_every_pair(hashes):
    _, stats = _search.brute_force_pairs(hashes, threshold=10)
    n = len(hashes)
    assert stats.comparisons == n * (n - 1) // 2


def test_threshold_zero_finds_only_exact_hash_matches(hashes):
    pairs, _ = _search.brute_force_pairs(hashes, threshold=0)
    for i, j in pairs:
        assert hashes[i] == hashes[j]


def test_pairs_are_ordered_so_sets_compare(hashes):
    pairs, _ = _search.brute_force_pairs(hashes, threshold=12)
    assert all(i < j for i, j in pairs)


def test_recall_rises_and_precision_falls_as_the_threshold_widens(
    corpus, hashes
):
    """The trade-off that makes the threshold a decision rather than a default."""
    expected = corpus.duplicate_pairs()
    results = _search.sweep(hashes, expected, thresholds=range(0, 25))

    recalls = [q.recall for q in results]
    assert recalls == sorted(recalls), "recall should never fall as t grows"

    assert results[0].recall < results[-1].recall, "threshold had no effect"
    assert results[-1].precision <= results[0].precision


def test_some_threshold_separates_duplicates_from_everything_else(
    corpus, hashes
):
    """The claim the detector is actually making.

    Not "distances are small" but "there is a cut that puts every known
    duplicate on one side and nothing else on the other". If no such
    threshold exists, grouping by distance cannot work no matter how it is
    indexed.
    """
    expected = corpus.duplicate_pairs()
    results = _search.sweep(hashes, expected, thresholds=range(0, 33))
    perfect = [q for q in results if q.recall == 1.0 and q.precision == 1.0]
    assert perfect, (
        "no threshold separated the groups cleanly; best was "
        + str(max(results, key=lambda q: q.recall + q.precision))
    )


def test_query_agrees_with_pairwise(hashes):
    """One-probe search and all-pairs search must not disagree."""
    threshold = 12
    pairs, _ = _search.brute_force_pairs(hashes, threshold)
    probe_index = 0
    from_pairs = {j for i, j in pairs if i == probe_index}
    from_query, _ = _search.brute_force_query(hashes, hashes[probe_index], threshold)
    assert from_pairs == set(from_query) - {probe_index}
