#!/usr/bin/env python3
"""
Pure matching logic for auto-resolving VendSoft's "Products to review" screen --
kept separate from sync.py's browser-driving code so the scoring itself is easy to reason
about and unit-test without Playwright.

Confirmed live (2026-09-14) against VendSoft's actual "Search VendSoft products" box: it's a
plain case-insensitive substring filter over the full product catalog, evaluated client-side
(no network round-trip per keystroke) -- e.g. searching "gator" returned both "Gatorade Zero
Fruit Punch" and "Gatorade Zero Glacier Freeze". That means VendSoft's own search gives us
candidates, not confidence -- picking the *right* one, and deciding whether to trust it at
all, is on us.

Three outcomes, deliberately conservative -- when unsure, this asks a person rather than
guessing, since these actions (mapping a product, or creating a new one) touch real inventory
records, even though they're described as safe to correct by hand afterward:

- CONFIDENT_MATCH: an existing product is clearly the same item under a different name.
- CONFIDENT_NEW: no existing product is even a plausible match -- genuinely new.
- AMBIGUOUS: something plausible exists but isn't a clear enough winner to trust blindly.
"""
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# Tuned against real occurrences, not guessed in the abstract -- see the worked examples
# below. Deliberately conservative: it's cheap to ask a person once in a while, expensive to
# silently mis-map a product.
CONFIDENT_MATCH_SCORE = 0.55
CONFIDENT_MATCH_MARGIN = 0.20  # top score must clear the runner-up by at least this much
NO_MATCH_SCORE = 0.15  # below this, a candidate isn't worth surfacing as "maybe"

_STOPWORDS = {"the", "and", "of", "a", "an", "with", "in", "for", "to"}
_CODE_SUFFIX = re.compile(r"\s*\(([^()]+)\)\s*$")


def _tokens(name: str) -> set:
    words = re.findall(r"[a-z0-9]+", name.lower())
    return {w for w in words if w not in _STOPWORDS}


def similarity(a: str, b: str) -> float:
    """Jaccard overlap of significant words -- handles VendSoft/MicroMart naming the same
    product with reordered or extra/missing words (e.g. "Hippeas Puffs, White Cheddar" vs
    "Hippeas Vegan White Cheddar Chickpea Puffs" -> 4 shared / 6 total = 0.667), while still
    clearly separating a true near-miss from a same-brand decoy (e.g. against "Gatorade Zero
    Sugar, Glacier Freeze": "Gatorade Zero Glacier Freeze" scores 0.8, "Gatorade Zero Fruit
    Punch" scores 0.29 -- the shared brand/line words alone don't fool it)."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def search_terms(product_name: str) -> List[str]:
    """Words worth trying against VendSoft's substring search, longest first -- short/common
    words return too many irrelevant candidates to be useful as a search seed (they're still
    used in scoring via similarity(), just not as the search query itself)."""
    words = [w for w in re.findall(r"[a-zA-Z0-9]+", product_name) if len(w) >= 4]
    return sorted(set(words), key=len, reverse=True)


def parse_option_text(option_text: str) -> Tuple[str, Optional[str]]:
    """"Gatorade Zero Glacier Freeze (VS0ZQJ)" -> ("Gatorade Zero Glacier Freeze", "VS0ZQJ")"""
    m = _CODE_SUFFIX.search(option_text)
    if m:
        return option_text[:m.start()].strip(), m.group(1).strip()
    return option_text.strip(), None


@dataclass
class MatchResult:
    outcome: str  # "confident_match", "confident_new", or "ambiguous"
    product_name: str
    best_candidate: Optional[str] = None
    best_code: Optional[str] = None
    best_score: float = 0.0
    candidates_considered: List[str] = field(default_factory=list)


def decide(product_name: str, candidate_option_texts: List[str]) -> MatchResult:
    """candidate_option_texts: every distinct "Name (CODE)" string seen across all search-term
    probes for this product (see search_terms()) -- the union, not just the last query's
    results, since different words can surface different (or overlapping) candidates."""
    seen = {}
    for text in candidate_option_texts:
        name, code = parse_option_text(text)
        seen[text] = (name, code, similarity(product_name, name))

    if not seen:
        return MatchResult(outcome="confident_new", product_name=product_name)

    ranked = sorted(seen.values(), key=lambda t: t[2], reverse=True)
    best_name, best_code, best_score = ranked[0]
    second_score = ranked[1][2] if len(ranked) > 1 else 0.0
    considered = [f"{n} ({c}): {s:.2f}" for n, c, s in ranked[:5]]

    if best_score < NO_MATCH_SCORE:
        return MatchResult(outcome="confident_new", product_name=product_name,
                            candidates_considered=considered)

    if best_score >= CONFIDENT_MATCH_SCORE and (best_score - second_score) >= CONFIDENT_MATCH_MARGIN:
        return MatchResult(outcome="confident_match", product_name=product_name,
                            best_candidate=best_name, best_code=best_code, best_score=best_score,
                            candidates_considered=considered)

    return MatchResult(outcome="ambiguous", product_name=product_name,
                        best_candidate=best_name, best_code=best_code, best_score=best_score,
                        candidates_considered=considered)
