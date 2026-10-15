"""Perceptual hashing for near-duplicate image detection.

sha256 answers "are these the same bytes". Most duplicates in an asset
library are not the same bytes: the same photograph re-encoded at a
different JPEG quality, resized for a thumbnail, or converted to WebP is a
different file that a person would call identical. A perceptual hash is
built so that those versions land on almost the same value, and the
distance between two hashes stands in for how different the pictures look.

The construction here is the standard DCT-based pHash:

    grayscale -> resize to 32x32 -> 2-D DCT -> keep the top-left 8x8
    -> threshold each coefficient against the median -> 64 bits

Each step exists for a reason worth stating:

* Grayscale, because a recolored copy is still the same picture and colour
  would make the hash sensitive to something people ignore.
* 32x32, because the hash should describe composition, not detail. At that
  size a resize or a re-encode changes almost nothing.
* The DCT, because it sorts the image into frequency bands. The low
  frequencies carry the layout -- light here, dark there -- and the high
  ones carry exactly the detail that compression throws away. Keeping only
  the top-left 8x8 keeps the part that survives.
* The median rather than the mean, because the mean is dragged by a single
  extreme coefficient, and a hash that moves when one value moves is a
  hash that reports false differences.
* The DC term (coefficient 0,0) is dropped. It encodes overall brightness,
  so leaving it in would make a brightened copy look like a different
  image -- the opposite of what this is for.

No project imports: this module is pure numeric work on an image, which
keeps it independently testable and keeps the test suite able to load it
by file path.
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np
from PIL import Image

HASH_BITS = 64
_SAMPLE_SIZE = 32
_LOW_FREQ = 8


@lru_cache(maxsize=4)
def _dct_matrix(n: int) -> np.ndarray:
    """Orthonormal DCT-II basis, so that M @ x is the 1-D transform.

    Built rather than imported from scipy: it is four lines, and it keeps
    the dependency list to numpy and Pillow.
    """
    k = np.arange(n)
    basis = np.cos(np.pi * (2 * k[None, :] + 1) * k[:, None] / (2 * n))
    basis[0] *= 1.0 / np.sqrt(2.0)
    return basis * np.sqrt(2.0 / n)


def _dct2(block: np.ndarray) -> np.ndarray:
    """2-D DCT as a pair of 1-D transforms, rows then columns."""
    m = _dct_matrix(block.shape[0])
    return m @ block @ m.T


def phash(image: Image.Image) -> int:
    """64-bit perceptual hash of a PIL image.

    Returns an int rather than bytes or a hex string so that distance is a
    single XOR and popcount, and so that it stores in a BIGINT column.
    """
    grey = image.convert('L').resize(
        (_SAMPLE_SIZE, _SAMPLE_SIZE), Image.Resampling.LANCZOS
    )
    pixels = np.asarray(grey, dtype=np.float64)

    coefficients = _dct2(pixels)[:_LOW_FREQ, :_LOW_FREQ]

    # Flatten, drop the DC term for the median, but keep its bit position so
    # the hash stays a fixed 64 bits. Comparing the DC term against a median
    # computed without it is harmless -- it lands on whichever side overall
    # brightness puts it, consistently for both copies of an image.
    flat = coefficients.flatten()
    median = float(np.median(flat[1:]))

    bits = 0
    for index, value in enumerate(flat):
        if value > median:
            bits |= 1 << index
    return bits


def phash_file(path) -> int:
    """Perceptual hash of an image on disk."""
    with Image.open(path) as handle:
        return phash(handle)


def hamming(a: int, b: int) -> int:
    """Number of differing bits -- the distance these hashes are compared by.

    Hamming distance is a true metric, which matters more than it looks:
    it satisfies the triangle inequality, and that is the property a
    BK-tree needs in order to prune. An ad-hoc similarity score would make
    the index in search.py impossible.
    """
    return (a ^ b).bit_count()


def similarity(a: int, b: int) -> float:
    """Distance expressed as a 0..1 score, for reporting rather than logic."""
    return 1.0 - (hamming(a, b) / HASH_BITS)
