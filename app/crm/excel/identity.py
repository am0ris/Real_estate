"""Possible-duplicate detection across customer records (pure).

Customers are separate per channel on purpose: merging two records because
they share an unverified phone number would let one visitor reach another
person's data. This module only *suggests* pairs for staff to review and
decides how the workbook groups them - it never changes a customer record,
and the assistant never sees any of it.

A number is "verified" only when the person demonstrably controls it: the
WhatsApp sender id (or, later, an OTP). A number typed on the website and
read back is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Iterable, Mapping

from app.crm.excel.schema import STRONG_VERIFICATION_METHODS
from app.crm.excel.text import PERSON_PREFIX, code

MAX_GROUP = 8  # more records than this sharing one number is data noise, not a person


@dataclass(frozen=True)
class CustomerFacts:
    pk: int
    name: str
    phone: str | None  # E.164, already normalised
    email: str | None
    channel: str
    phone_verified: bool
    verification_method: str | None


@dataclass(frozen=True)
class Candidate:
    a: CustomerFacts
    b: CustomerFacts
    evidence: str
    auto_linked: bool  # verified on both sides: grouped without waiting for review


def strongly_verified(customer: CustomerFacts) -> bool:
    return bool(customer.phone and customer.phone_verified and customer.verification_method in STRONG_VERIFICATION_METHODS)


def _pair(x: CustomerFacts, y: CustomerFacts) -> tuple[CustomerFacts, CustomerFacts]:
    return (x, y) if x.pk < y.pk else (y, x)


def find_candidates(customers: Iterable[CustomerFacts]) -> list[Candidate]:
    """Every pair of records that share a phone number or an email."""
    by_phone: dict[str, list[CustomerFacts]] = {}
    by_email: dict[str, list[CustomerFacts]] = {}
    for customer in customers:
        if customer.phone:
            by_phone.setdefault(customer.phone, []).append(customer)
        if customer.email:
            by_email.setdefault(customer.email.strip().casefold(), []).append(customer)

    found: dict[tuple[int, int], Candidate] = {}
    for group in by_phone.values():
        if not 2 <= len(group) <= MAX_GROUP:
            continue
        for x, y in combinations(group, 2):
            a, b = _pair(x, y)
            strong = strongly_verified(a) and strongly_verified(b)
            found[(a.pk, b.pk)] = Candidate(a, b, "same_phone_verified_both" if strong else "same_phone", strong)
    for group in by_email.values():
        if not 2 <= len(group) <= MAX_GROUP:
            continue
        for x, y in combinations(group, 2):
            a, b = _pair(x, y)
            found.setdefault((a.pk, b.pk), Candidate(a, b, "same_email", False))
    return sorted(found.values(), key=lambda item: (item.a.pk, item.b.pk))


def linked_pairs(candidates: Iterable[Candidate], decisions: Mapping[tuple[int, int], str]) -> set[tuple[int, int]]:
    """Pairs the workbook treats as the same person.

    Verified-on-both pairs are linked automatically; staff can confirm any other
    pair, and a staff "rejected" always wins over automatic linking.
    """
    linked: set[tuple[int, int]] = set()
    seen = set()
    for candidate in candidates:
        key = (candidate.a.pk, candidate.b.pk)
        seen.add(key)
        decision = decisions.get(key, "")
        if decision == "rejected":
            continue
        if decision == "confirmed" or candidate.auto_linked:
            linked.add(key)
    # A confirmed decision stays valid even if the evidence has since changed.
    linked |= {key for key, decision in decisions.items() if decision == "confirmed"}
    return linked


def person_keys(linked: Iterable[tuple[int, int]]) -> dict[int, str]:
    """``customer pk -> P-000012`` for every record in a linked group of two or more."""
    parent: dict[int, int] = {}

    def find(node: int) -> int:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for a, b in linked:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[max(root_a, root_b)] = min(root_a, root_b)
    return {node: code(PERSON_PREFIX, find(node)) for node in list(parent)}


def pending_candidates(candidates: Iterable[Candidate], decisions: Mapping[tuple[int, int], str]) -> list[Candidate]:
    """Pairs that still need a person to look at them."""
    return [c for c in candidates if not c.auto_linked and not decisions.get((c.a.pk, c.b.pk))]
