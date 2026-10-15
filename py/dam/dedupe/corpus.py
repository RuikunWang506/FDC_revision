"""Synthetic image corpus with known duplicate groups.

There is no access to the real asset library, so correctness has to be
established against images whose relationships are known by construction.
That turns out to be the better starting point anyway: with real data you
can measure how a detector behaves, but you cannot tell a false positive
from a true one without labelling it by hand first.

Here the labels come free. A base image is generated from a seed, then put
through the transformations an asset library actually produces -- JPEG
re-encoding, resizing for thumbnails, format conversion, small exposure
shifts. Every variant of a base shares its group id, and images from
different bases are known to be unrelated.

What this cannot tell you is whether real duplicates in the FDC library
look like these. The transformations are chosen from how image pipelines
usually degrade a file, not from measurements of that library. Thresholds
tuned only on this corpus should be re-checked against real phash values
once they are available.

No project imports, so tests can load this by file path.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageEnhance

_BASE_SIZE = 512


@dataclass(frozen=True)
class Variant:
    """One image plus the ground truth about where it came from."""

    image: Image.Image
    group: int
    transform: str = 'original'

    @property
    def is_original(self) -> bool:
        return self.transform == 'original'


@dataclass
class Corpus:
    variants: list[Variant] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.variants)

    def groups(self) -> dict[int, list[Variant]]:
        out: dict[int, list[Variant]] = {}
        for variant in self.variants:
            out.setdefault(variant.group, []).append(variant)
        return out

    def duplicate_pairs(self) -> set[tuple[int, int]]:
        """Index pairs that a detector is supposed to find, by construction."""
        pairs: set[tuple[int, int]] = set()
        by_group: dict[int, list[int]] = {}
        for index, variant in enumerate(self.variants):
            by_group.setdefault(variant.group, []).append(index)
        for members in by_group.values():
            for i, left in enumerate(members):
                for right in members[i + 1:]:
                    pairs.add((left, right))
        return pairs


def base_image(seed: int, size: int = _BASE_SIZE) -> Image.Image:
    """A deterministic, structured image.

    Structure matters. Pure noise has no low-frequency content, so every
    noise image hashes to roughly the same thing and the corpus would
    quietly stop testing anything. These are built from a few large smooth
    gradients plus blocks -- the kind of broad light-and-dark layout a
    perceptual hash is meant to summarise.
    """
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:size, 0:size].astype(np.float64)

    canvas = np.zeros((size, size, 3), dtype=np.float64)
    for channel in range(3):
        angle = rng.uniform(0, 2 * np.pi)
        frequency = rng.uniform(0.5, 2.5)
        plane = (
            np.cos(angle) * x / size + np.sin(angle) * y / size
        ) * frequency
        canvas[..., channel] = 128 + 110 * np.sin(2 * np.pi * plane)

    # A few solid blocks, so each base has a distinct coarse layout rather
    # than only a smooth gradient.
    for _ in range(rng.integers(3, 6)):
        x0, y0 = rng.integers(0, size - size // 4, size=2)
        w, h = rng.integers(size // 8, size // 3, size=2)
        colour = rng.integers(0, 256, size=3)
        canvas[y0:y0 + h, x0:x0 + w] = colour

    return Image.fromarray(canvas.clip(0, 255).astype(np.uint8), mode='RGB')


def _reencode(image: Image.Image, fmt: str, **options) -> Image.Image:
    """Round-trip through an encoder so the result carries real artifacts.

    Saving to a buffer and reading back matters: setting a quality value
    without re-encoding would change nothing, and the test would be
    measuring an operation that never happened.
    """
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **options)
    buffer.seek(0)
    return Image.open(buffer).convert('RGB')


def jpeg(image: Image.Image, quality: int = 60) -> Image.Image:
    return _reencode(image, 'JPEG', quality=quality)


def resized(image: Image.Image, factor: float = 0.5) -> Image.Image:
    width = max(8, int(image.width * factor))
    height = max(8, int(image.height * factor))
    return image.resize((width, height), Image.Resampling.LANCZOS)


def webp(image: Image.Image, quality: int = 75) -> Image.Image:
    return _reencode(image, 'WEBP', quality=quality)


def brightened(image: Image.Image, factor: float = 1.15) -> Image.Image:
    return ImageEnhance.Brightness(image).enhance(factor)


def build_corpus(groups: int = 12, seed: int = 7) -> Corpus:
    """A corpus of `groups` originals, each with four near-duplicates."""
    corpus = Corpus()
    for group in range(groups):
        original = base_image(seed * 1000 + group)
        corpus.variants.append(Variant(original, group, 'original'))
        corpus.variants.append(Variant(jpeg(original, 60), group, 'jpeg60'))
        corpus.variants.append(Variant(resized(original, 0.5), group, 'half'))
        corpus.variants.append(Variant(webp(original, 75), group, 'webp75'))
        corpus.variants.append(
            Variant(brightened(original, 1.15), group, 'bright115')
        )
    return corpus
