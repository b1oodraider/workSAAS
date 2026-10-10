"""Meaning similarity on top of the keyword prefilter.

Keywords miss a Russian resume against an English vacancy, synonyms ("бэкенд-разработчик" vs
"Python Developer") and related roles; embeddings catch them. The keyword score stays the
guard for hard filters and auto-apply; the blend only decides which vacancies the LLM sees.
"""

from __future__ import annotations

from app.ranking.prefilter import RankResult


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of unit-length vectors (as the gateway returns them)."""
    return sum(x * y for x, y in zip(a, b))


def similarity_score(cos: float, low: float, high: float) -> float:
    """Cosine -> 0..100: ``low`` and below is 0, ``high`` and above is 100, linear in between."""
    if high <= low:
        return 100.0 if cos >= high else 0.0
    return round(max(0.0, min(1.0, (cos - low) / (high - low))) * 100, 1)


def blended_score(keyword: float, semantic: float | None, weight: float) -> float:
    """Keyword score mixed with the meaning similarity (the keyword score alone without one)."""
    if semantic is None:
        return keyword
    return round((1 - weight) * keyword + weight * semantic, 1)


def blend(rank: RankResult, similarity: float | None, weight: float) -> RankResult:
    """Keyword score mixed with the meaning similarity; excluded vacancies stay excluded."""
    if rank.excluded or similarity is None:
        return rank
    return RankResult(blended_score(rank.score, similarity, weight), [*rank.reasons, f"по смыслу: {similarity:.0f}"])
