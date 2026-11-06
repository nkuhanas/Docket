"""Shared claim scopes cannot manufacture a second receivable or received cash."""

from fractions import Fraction

from docket.domain.settlement_flow import SettlementIntent, SettlementPart, SettlementScope, settle


def scopes(first=0, last=120):
    return (SettlementScope("work:example:base", first, last, "USD"),)


def parts():
    return [
        SettlementPart("regular", "work:example:base", 0, 60, "USD", Fraction(80)),
        SettlementPart("overtime", "work:example:base", 60, 120, "USD", Fraction(120)),
    ]


def test_whole_timesheet_and_invoice_share_one_capacity():
    intents = [
        SettlementIntent("timesheet", "cash", Fraction(200), "USD", scopes(), "p1"),
        SettlementIntent("invoice", "cash", Fraction(80), "USD", scopes(0, 60), "p2"),
    ]
    result = settle(parts(), intents, {"p1": Fraction(200), "p2": Fraction(80)})
    assert sum(sum(rows.values()) for rows in result.matches.values()) == 200
    assert sum(result.unmatched.values()) == 80
    assert sum(result.outstanding.values()) == 0


def test_broad_payment_does_not_block_new_specific_payment_or_move_outside_scope():
    intents = [
        SettlementIntent("a-broad", "cash", Fraction(100), "USD", scopes(), "p1"),
        SettlementIntent("z-specific", "cash", Fraction(80), "USD", scopes(0, 60), "p2"),
    ]
    result = settle(parts(), intents, {"p1": Fraction(100), "p2": Fraction(80)})
    assert not any(result.unmatched.values())
    assert result.matches["z-specific"] == {"regular": 80}
    assert result.matches["a-broad"] == {"overtime": 100}
    assert result.outstanding["overtime"] == 20


def test_credit_releases_overpayment_and_withholding_never_becomes_received_cash():
    intents = [
        SettlementIntent("credit", "credit", Fraction(120), "USD", scopes(60, 120)),
        SettlementIntent("tax", "deduction", Fraction(20), "USD", scopes(0, 60)),
        SettlementIntent("cash", "cash", Fraction(100), "USD", scopes(), "p1"),
    ]
    result = settle(parts(), intents, {"p1": Fraction(100)})
    assert result.matches["credit"] == {"overtime": 120}
    assert result.matches["tax"] == {"regular": 20}
    assert sum(result.matches["cash"].values()) == 60
    assert result.unmatched["cash"] == 40
    assert sum(result.outstanding.values()) == 0
    # An earmarked payment remains on its original source after that source is credited.
    earmarked = settle(
        parts(),
        [intents[0], SettlementIntent("cash", "cash", Fraction(80), "USD", scopes(60, 120), "p1")],
        {"p1": Fraction(80)},
    )
    assert earmarked.unmatched["cash"] == 80 and earmarked.outstanding["regular"] == 80


def test_fractional_money_and_receipt_capacity_stay_exact():
    targets = [
        SettlementPart("one", "expense:example", 0, 1, "USD", Fraction(1, 3)),
        SettlementPart("two", "expense:example", 1, 2, "USD", Fraction(2, 3)),
    ]
    scope = (SettlementScope("expense:example", 0, 2, "USD"),)
    intents = [
        SettlementIntent("first", "cash", Fraction(1), "USD", scope, "receipt"),
        SettlementIntent("second", "cash", Fraction(1), "USD", scope, "receipt"),
    ]
    result = settle(targets, intents, {"receipt": Fraction(1)})
    assert sum(sum(rows.values()) for rows in result.matches.values()) == 1
    assert sum(result.unmatched.values()) == 1
    assert sum(result.outstanding.values()) == 0
