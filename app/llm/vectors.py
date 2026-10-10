"""Embedding vectors: unit length and compact storage (no numpy needed)."""

from __future__ import annotations

import math
import struct


def unit(vec: list[float]) -> list[float]:
    """Scale to length 1, so that cosine similarity is a plain dot product."""
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else list(vec)


def pack(vec: list[float]) -> bytes:
    """Little-endian float16: ~3 significant digits, plenty for comparing unit vectors."""
    return struct.pack(f"<{len(vec)}e", *vec)


def unpack(data: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(data) // 2}e", data))
