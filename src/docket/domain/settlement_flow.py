"""Exact settlement matching over overlapping document scopes.

A payment can target a whole timesheet or one invoice describing some of the same
work. The source capacities occur once. Matching stays inside each retained scope;
credits and explicit deductions consume capacity separately from received cash.
"""

from collections import deque
from dataclasses import dataclass, field
from fractions import Fraction


@dataclass(frozen=True)
class SettlementPart:
    ref: str
    source_key: str
    start: int
    end: int
    currency: str
    amount: Fraction


@dataclass(frozen=True)
class SettlementScope:
    source_key: str
    start: int
    end: int
    currency: str

    def covers(self, part: SettlementPart) -> bool:
        return (
            self.source_key == part.source_key
            and self.currency == part.currency
            and self.start <= part.start
            and self.end >= part.end
        )


@dataclass(frozen=True)
class SettlementIntent:
    ref: str
    kind: str
    amount: Fraction
    currency: str
    scopes: tuple[SettlementScope, ...]
    payment_ref: str | None = None


@dataclass
class SettlementMatch:
    matches: dict[str, dict[str, Fraction]] = field(default_factory=dict)
    unmatched: dict[str, Fraction] = field(default_factory=dict)
    outstanding: dict[str, Fraction] = field(default_factory=dict)


class ExactNetwork:
    def __init__(self) -> None:
        self.edges: dict[str, dict[str, Fraction]] = {}
        self.original: dict[tuple[str, str], Fraction] = {}

    def add(self, first: str, last: str, capacity: Fraction) -> None:
        if capacity < 0:
            raise ValueError("Settlement capacities cannot be negative")
        self.edges.setdefault(first, {})[last] = capacity
        self.edges.setdefault(last, {}).setdefault(first, Fraction(0))
        self.original[first, last] = capacity

    def flow(self) -> None:
        while True:
            parents: dict[str, str] = {}
            queue = deque(["source"])
            seen = {"source"}
            while queue and "sink" not in seen:
                node = queue.popleft()
                for neighbor, capacity in self.edges[node].items():
                    if capacity > 0 and neighbor not in seen:
                        parents[neighbor] = node
                        seen.add(neighbor)
                        queue.append(neighbor)
            if "sink" not in seen:
                return
            path = []
            node = "sink"
            while node != "source":
                previous = parents[node]
                path.append((previous, node))
                node = previous
            amount = min(self.edges[first][last] for first, last in path)
            for first, last in path:
                self.edges[first][last] -= amount
                self.edges[last][first] += amount

    def used(self, first: str, last: str) -> Fraction:
        return self.original[first, last] - self.edges[first][last]


def settle(
    parts: list[SettlementPart], intents: list[SettlementIntent], receipts: dict[str, Fraction]
) -> SettlementMatch:
    if len({part.ref for part in parts}) != len(parts) or len(
        {intent.ref for intent in intents}
    ) != len(intents):
        raise ValueError("Settlement references must be unique")
    result = SettlementMatch(outstanding={part.ref: part.amount for part in parts})
    for kind in ("credit", "deduction", "cash"):
        selected = sorted(
            (intent for intent in intents if intent.kind == kind), key=lambda i: i.ref
        )
        if not selected:
            continue
        network = ExactNetwork()
        for part in sorted(parts, key=lambda p: p.ref):
            network.add("part:" + part.ref, "sink", result.outstanding[part.ref])
        if kind == "cash":
            for ref, amount in sorted(receipts.items()):
                network.add("source", "payment:" + ref, amount)
        for intent in selected:
            if intent.amount < 0 or not intent.scopes:
                raise ValueError("Settlement intents require nonnegative money and retained scopes")
            node = "intent:" + intent.ref
            if kind == "cash":
                if intent.payment_ref not in receipts:
                    raise ValueError("Cash settlement requires its owned receipt")
                network.add("payment:" + str(intent.payment_ref), node, intent.amount)
            else:
                network.add("source", node, intent.amount)
            for part in sorted(parts, key=lambda p: p.ref):
                if any(scope.covers(part) for scope in intent.scopes):
                    network.add(node, "part:" + part.ref, intent.amount)
        network.flow()
        for intent in selected:
            matched = {}
            for part in parts:
                edge = ("intent:" + intent.ref, "part:" + part.ref)
                if edge in network.original:
                    amount = network.used(*edge)
                    if amount:
                        matched[part.ref] = amount
                        result.outstanding[part.ref] -= amount
            result.matches[intent.ref] = matched
            result.unmatched[intent.ref] = intent.amount - sum(matched.values(), Fraction(0))
    if any(intent.kind not in {"credit", "deduction", "cash"} for intent in intents):
        raise ValueError("Unknown settlement kind")
    return result
