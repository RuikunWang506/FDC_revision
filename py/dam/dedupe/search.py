"""Brute-force near-duplicate search, and the quality measures for it.

This is the baseline, deliberately. Every pair is compared, so the answer
it gives is the definition of correct for this distance and threshold.
Whatever index replaces it has to return exactly the same groups -- an
index that is faster and slightly different is not an optimisation, it is
a different detector, and the difference has to be a choice rather than a
surprise.

It also gives the number the index will be measured against: comparisons
performed. For n hashes that is n(n-1)/2, which is the reason an index is
needed at all -- 50,000 images is 1.25 billion comparisons.

No project imports, so tests can load this by file path.
"""
from __future__ import annotations

from dataclasses import dataclass, field


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


@dataclass
class SearchStats:
    """What a run cost, so two strategies can be compared on more than time."""

    comparisons: int = 0
    candidates: int = 0

    def __str__(self) -> str:
        return f'comparisons={self.comparisons} candidates={self.candidates}'


def brute_force_pairs(
    hashes: list[int], threshold: int
) -> tuple[set[tuple[int, int]], SearchStats]:
    """Every index pair within `threshold` bits, plus what it cost.

    Pairs come back as (low, high) so that a set of them compares cleanly
    against a set built any other way.
    """
    stats = SearchStats()
    pairs: set[tuple[int, int]] = set()
    for i in range(len(hashes)):
        for j in range(i + 1, len(hashes)):
            stats.comparisons += 1
            if hamming(hashes[i], hashes[j]) <= threshold:
                pairs.add((i, j))
    stats.candidates = stats.comparisons
    return pairs, stats


def brute_force_query(
    hashes: list[int], probe: int, threshold: int
) -> tuple[list[int], SearchStats]:
    """Indices within `threshold` of one probe hash."""
    stats = SearchStats()
    hits: list[int] = []
    for index, value in enumerate(hashes):
        stats.comparisons += 1
        if hamming(value, probe) <= threshold:
            hits.append(index)
    stats.candidates = stats.comparisons
    return hits, stats


@dataclass
class Quality:
    """How a threshold did against known ground truth.

    Precision and recall move in opposite directions as the threshold
    changes, and which one matters depends on what happens to a detected
    duplicate. If the pipeline deletes files, a false positive destroys an
    asset and precision dominates. If it only flags them for review, a
    false negative means the duplicate stays forever and recall dominates.
    The threshold cannot be chosen without that decision, which is why
    this reports both instead of a single score.
    """

    threshold: int
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    missed: set[tuple[int, int]] = field(default_factory=set)
    spurious: set[tuple[int, int]] = field(default_factory=set)

    @property
    def precision(self) -> float:
        found = self.true_positives + self.false_positives
        return self.true_positives / found if found else 1.0

    @property
    def recall(self) -> float:
        actual = self.true_positives + self.false_negatives
        return self.true_positives / actual if actual else 1.0

    def __str__(self) -> str:
        return (
            f'threshold={self.threshold} '
            f'precision={self.precision:.3f} recall={self.recall:.3f} '
            f'tp={self.true_positives} fp={self.false_positives} '
            f'fn={self.false_negatives}'
        )


def evaluate(
    found: set[tuple[int, int]],
    expected: set[tuple[int, int]],
    threshold: int,
) -> Quality:
    return Quality(
        threshold=threshold,
        true_positives=len(found & expected),
        false_positives=len(found - expected),
        false_negatives=len(expected - found),
        missed=expected - found,
        spurious=found - expected,
    )


def sweep(
    hashes: list[int],
    expected: set[tuple[int, int]],
    thresholds: range = range(0, 25),
) -> list[Quality]:
    """Quality at each threshold, for picking one with the trade-off visible.

    A single recommended number would hide the shape of the curve, and the
    shape is the useful part: it shows where recall starts costing
    precision, which is the only place the decision is interesting.
    """
    return [
        evaluate(brute_force_pairs(hashes, t)[0], expected, t)
        for t in thresholds
    ]
