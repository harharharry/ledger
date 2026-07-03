"""Tests for the risk manager (veto layer).

Every number here is hand-computable against the real config.toml (60/40
split, 20% per-trade cap, £50 floor, 1/day, 5/week, ±10pts drift threshold).
Adversarial cases are the point: oversize proposals, boundary values, engaged
kill switch, sub-floor arrivals, drift edges.
"""

from decimal import Decimal

import pytest

from ledger import kill_switch
from ledger.proposals import Proposal
from ledger.risk import PortfolioSnapshot, check_drift, evaluate, sleeve_value_gbp

D = Decimal


def make_proposal(sleeve="crypto", notional="60", rationale="uptrend: scheduled accumulation"):
    venue = "kraken" if sleeve == "crypto" else "alpaca"
    asset = "BTC" if sleeve == "crypto" else "QQQ"
    return Proposal(
        sleeve=sleeve,
        asset=asset,
        venue=venue,
        side="buy",
        notional_gbp=D(notional),
        rationale=rationale,
    )


def day_one_snapshot(cash="500"):
    """All cash, no holdings — the portfolio as it exists on day one."""
    return PortfolioSnapshot(cash_gbp=D(cash), holdings_value_gbp={})


def absent_switch(tmp_path):
    """A kill-switch path that exists nowhere — the switch is disengaged."""
    return tmp_path / "KILL_SWITCH"


def engaged_switch(tmp_path):
    path = tmp_path / "KILL_SWITCH"
    kill_switch.engage(path, "test")
    return path


# --- sleeve_value_gbp -------------------------------------------------------


def test_day_one_sleeve_values_use_target_share_of_cash(config):
    snapshot = day_one_snapshot("500")
    assert sleeve_value_gbp(snapshot, "crypto", config) == D("300.0")
    assert sleeve_value_gbp(snapshot, "stocks", config) == D("200.0")


def test_sleeve_value_adds_holdings_to_cash_share(config):
    snapshot = PortfolioSnapshot(cash_gbp=D("100"), holdings_value_gbp={"crypto": D("50")})
    # 50 holdings + 60% of 100 cash = 110
    assert sleeve_value_gbp(snapshot, "crypto", config) == D("110.0")
    # no stocks holdings + 40% of 100 cash = 40
    assert sleeve_value_gbp(snapshot, "stocks", config) == D("40.0")


# --- kill switch ------------------------------------------------------------


def test_kill_switch_blocks_otherwise_valid_proposal(config, tmp_path):
    decision = evaluate(
        make_proposal(notional="60"),
        day_one_snapshot(),
        config,
        proposals_today=0,
        proposals_this_week=0,
        kill_switch_path=engaged_switch(tmp_path),
    )
    assert decision.verdict == "blocked"
    assert decision.proposal is None
    assert len(decision.reasons) == 1
    assert "kill switch" in decision.reasons[0]


def test_kill_switch_reported_even_when_other_rules_would_fire(config, tmp_path):
    # Over-cap proposal AND exhausted cadence: the kill switch is still the
    # reason reported, because it is checked first, unconditionally.
    decision = evaluate(
        make_proposal(notional="100"),
        day_one_snapshot(),
        config,
        proposals_today=99,
        proposals_this_week=99,
        kill_switch_path=engaged_switch(tmp_path),
    )
    assert decision.verdict == "blocked"
    assert decision.proposal is None
    assert "kill switch" in decision.reasons[0]


def test_absent_switch_file_means_disengaged(config, tmp_path):
    decision = evaluate(
        make_proposal(notional="60"),
        day_one_snapshot(),
        config,
        proposals_today=0,
        proposals_this_week=0,
        kill_switch_path=absent_switch(tmp_path),
    )
    assert decision.verdict == "approved"
    assert decision.proposal is not None


# --- cadence caps -----------------------------------------------------------


def test_day_cap_boundary(config, tmp_path):
    common = dict(
        snapshot=day_one_snapshot(),
        config=config,
        proposals_this_week=0,
        kill_switch_path=absent_switch(tmp_path),
    )
    # 0 so far today: under the 1/day cap, passes.
    ok = evaluate(make_proposal(notional="60"), proposals_today=0, **common)
    assert ok.verdict == "approved"
    # 1 already today: at the 1/day cap, blocked.
    blocked = evaluate(make_proposal(notional="60"), proposals_today=1, **common)
    assert blocked.verdict == "blocked"
    assert blocked.proposal is None
    assert "daily proposal cap" in blocked.reasons[0]


def test_week_cap_blocks_at_five(config, tmp_path):
    decision = evaluate(
        make_proposal(notional="60"),
        day_one_snapshot(),
        config,
        proposals_today=0,
        proposals_this_week=5,
        kill_switch_path=absent_switch(tmp_path),
    )
    assert decision.verdict == "blocked"
    assert decision.proposal is None
    assert "weekly proposal cap" in decision.reasons[0]


# --- per-trade cap ----------------------------------------------------------


def test_day_one_oversize_crypto_proposal_resized_to_cap(config, tmp_path):
    # £500 cash, no holdings: crypto sleeve £300, 20% cap £60. £100 -> £60.
    decision = evaluate(
        make_proposal(notional="100"),
        day_one_snapshot("500"),
        config,
        proposals_today=0,
        proposals_this_week=0,
        kill_switch_path=absent_switch(tmp_path),
    )
    assert decision.verdict == "resized"
    assert decision.proposal is not None
    assert decision.proposal.notional_gbp == D("60.00")
    assert decision.proposal.rationale.endswith(
        " [risk: resized from £100.00 to £60.00 — 20% sleeve cap]"
    )
    # Everything except size and rationale survives untouched.
    assert decision.proposal.sleeve == "crypto"
    assert decision.proposal.asset == "BTC"
    assert decision.proposal.side == "buy"
    assert len(decision.reasons) == 1
    assert "per-trade cap" in decision.reasons[0]


def test_at_cap_proposal_passes_byte_identical(config, tmp_path):
    proposal = make_proposal(notional="60")
    decision = evaluate(
        proposal,
        day_one_snapshot("500"),
        config,
        proposals_today=0,
        proposals_this_week=0,
        kill_switch_path=absent_switch(tmp_path),
    )
    assert decision.verdict == "approved"
    assert decision.proposal is proposal  # the same object, not a copy
    assert decision.reasons == ()


def test_stocks_sleeve_cap_uses_stocks_allocation(config, tmp_path):
    # Stocks holdings £250 + 40% of £500 cash = £450 sleeve; 20% cap = £90.
    # (Were the crypto 60% split used by mistake, the sleeve would be £550
    # and the cap £110 — the resize target below would not be £90.)
    snapshot = PortfolioSnapshot(
        cash_gbp=D("500"), holdings_value_gbp={"stocks": D("250"), "crypto": D("300")}
    )
    decision = evaluate(
        make_proposal(sleeve="stocks", notional="120"),
        snapshot,
        config,
        proposals_today=0,
        proposals_this_week=0,
        kill_switch_path=absent_switch(tmp_path),
    )
    assert decision.verdict == "resized"
    assert decision.proposal.notional_gbp == D("90.00")


# --- cap/floor conflict (floor wins — Harry's decision, 2026-07-03) ---------


def test_floor_wins_cap_floor_conflict_small_sleeve(config, tmp_path):
    # £200 cash, no holdings: crypto sleeve £120, percentage cap £24 — under
    # the £50 floor. The floor wins the conflict: the effective cap is £50,
    # so a £60 proposal is resized to £50.00 (not resized-then-blocked, the
    # old deadlock). The audit note must honestly credit the floor, not
    # claim a percentage that wasn't applied.
    decision = evaluate(
        make_proposal(notional="60"),
        day_one_snapshot("200"),
        config,
        proposals_today=0,
        proposals_this_week=0,
        kill_switch_path=absent_switch(tmp_path),
    )
    assert decision.verdict == "resized"
    assert decision.proposal is not None
    assert decision.proposal.notional_gbp == D("50.00")
    assert decision.proposal.rationale.endswith(
        " [risk: resized from £60.00 to £50.00 — trade floor is "
        "the effective cap for this sleeve size]"
    )
    assert len(decision.reasons) == 1
    assert "floor is the binding cap" in decision.reasons[0]
    # The percentage-cap wording must NOT appear in the audit note.
    assert "% sleeve cap" not in decision.proposal.rationale


def test_percentage_cap_exactly_at_floor_uses_percentage_note(config, tmp_path):
    # Boundary: crypto holdings £250, no cash → sleeve £250 → 20% cap is
    # exactly £50, equal to the floor. The percentage cap was NOT below the
    # floor, so a £60 proposal resizes to £50 with the ordinary percentage
    # audit note — the floor-cap wording is reserved for genuine conflicts.
    snapshot = PortfolioSnapshot(cash_gbp=D("0"), holdings_value_gbp={"crypto": D("250")})
    decision = evaluate(
        make_proposal(notional="60"),
        snapshot,
        config,
        proposals_today=0,
        proposals_this_week=0,
        kill_switch_path=absent_switch(tmp_path),
    )
    assert decision.verdict == "resized"
    assert decision.proposal.notional_gbp == D("50.00")
    assert decision.proposal.rationale.endswith(
        " [risk: resized from £60.00 to £50.00 — 20% sleeve cap]"
    )
    assert "trade floor is the effective cap" not in decision.proposal.rationale


# --- fee floor --------------------------------------------------------------


def test_sub_floor_arrival_is_blocked_never_raised(config, tmp_path):
    # The veto layer never increases a trade: a £10 proposal is blocked,
    # not bumped up to the £50 floor.
    decision = evaluate(
        make_proposal(notional="10"),
        day_one_snapshot("500"),
        config,
        proposals_today=0,
        proposals_this_week=0,
        kill_switch_path=absent_switch(tmp_path),
    )
    assert decision.verdict == "blocked"
    assert decision.proposal is None
    assert "minimum trade floor" in decision.reasons[0]


def test_exactly_at_floor_passes(config, tmp_path):
    decision = evaluate(
        make_proposal(notional="50"),
        day_one_snapshot("500"),
        config,
        proposals_today=0,
        proposals_this_week=0,
        kill_switch_path=absent_switch(tmp_path),
    )
    assert decision.verdict == "approved"
    assert decision.proposal.notional_gbp == D("50")


# --- drift flags ------------------------------------------------------------


def test_all_crypto_holdings_flag_both_sleeves(config):
    snapshot = PortfolioSnapshot(
        cash_gbp=D("0"), holdings_value_gbp={"crypto": D("100"), "stocks": D("0")}
    )
    flags = check_drift(snapshot, config)
    assert len(flags) == 2
    crypto_flag, stocks_flag = flags
    assert "crypto sleeve is 100.0% of invested value vs 60.0% target" in crypto_flag
    assert "40.0pts" in crypto_flag
    assert "stocks sleeve is 0.0% of invested value vs 40.0% target" in stocks_flag
    assert "40.0pts" in stocks_flag


def test_holdings_exactly_at_target_no_flags(config):
    # Cash is excluded from drift entirely, so a big cash pile changes nothing.
    snapshot = PortfolioSnapshot(
        cash_gbp=D("500"), holdings_value_gbp={"crypto": D("60"), "stocks": D("40")}
    )
    assert check_drift(snapshot, config) == ()


def test_drift_boundary_is_strictly_greater_than_threshold(config):
    # Exactly 10pts off (70/30 vs 60/40): tolerated, not flagged.
    at_threshold = PortfolioSnapshot(
        cash_gbp=D("0"), holdings_value_gbp={"crypto": D("70"), "stocks": D("30")}
    )
    assert check_drift(at_threshold, config) == ()
    # Just beyond (71/29, 11pts off): flagged, both sleeves.
    beyond = PortfolioSnapshot(
        cash_gbp=D("0"), holdings_value_gbp={"crypto": D("71"), "stocks": D("29")}
    )
    assert len(check_drift(beyond, config)) == 2


def test_zero_holdings_no_flags(config):
    assert check_drift(day_one_snapshot("500"), config) == ()


def test_drift_flags_present_on_blocked_decision(config, tmp_path):
    # 100% crypto book, kill switch engaged: the block must still carry the
    # drift flags — a drifted book is worth knowing about regardless.
    snapshot = PortfolioSnapshot(
        cash_gbp=D("0"), holdings_value_gbp={"crypto": D("100"), "stocks": D("0")}
    )
    decision = evaluate(
        make_proposal(notional="60"),
        snapshot,
        config,
        proposals_today=0,
        proposals_this_week=0,
        kill_switch_path=engaged_switch(tmp_path),
    )
    assert decision.verdict == "blocked"
    assert len(decision.drift_flags) == 2


def test_drift_flags_present_on_approved_decision(config, tmp_path):
    snapshot = PortfolioSnapshot(
        cash_gbp=D("500"), holdings_value_gbp={"crypto": D("100"), "stocks": D("0")}
    )
    decision = evaluate(
        make_proposal(notional="60"),  # crypto sleeve 100 + 300 = 400, cap 80
        snapshot,
        config,
        proposals_today=0,
        proposals_this_week=0,
        kill_switch_path=absent_switch(tmp_path),
    )
    assert decision.verdict == "approved"
    assert len(decision.drift_flags) == 2
