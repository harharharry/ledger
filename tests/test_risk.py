"""Tests for the risk manager (veto layer), v1.2 per-asset rules.

Every number is hand-computable against the real config.toml (weights BTC 40 /
ETH 25 / SOL 15 / SUI 10 / HYPE 10, 20% per-trade cap, £50 floor, 1/day,
5/week, ±10pts drift threshold). Adversarial cases are the point.
"""

from decimal import Decimal

import pytest

from ledger import kill_switch
from ledger.proposals import Proposal
from ledger.risk import PortfolioSnapshot, RiskDecision, asset_value_gbp, check_drift, evaluate

D = Decimal


def proposal(asset="BTC", notional="60.00", venue="kraken"):
    return Proposal(
        asset=asset, venue=venue, side="buy",
        notional_gbp=D(notional), rationale="test rationale",
    )


def day_one_snapshot(cash="500.00"):
    return PortfolioSnapshot(cash_gbp=D(cash), holdings_value_gbp={})


def ev(config, tmp_path, prop, snapshot, today=0, week=0):
    return evaluate(
        prop, snapshot, config,
        proposals_today=today, proposals_this_week=week,
        kill_switch_path=tmp_path / "KILL_SWITCH",
    )


# -- kill switch -----------------------------------------------------------------


def test_kill_switch_blocks_everything(config, tmp_path):
    kill_switch.engage(tmp_path / "KILL_SWITCH")
    decision = ev(config, tmp_path, proposal(), day_one_snapshot())
    assert decision.verdict == "blocked" and decision.proposal is None
    assert "kill switch" in decision.reasons[0]


def test_kill_switch_reported_alone_even_when_other_rules_would_fire(config, tmp_path):
    kill_switch.engage(tmp_path / "KILL_SWITCH")
    # oversize proposal + exhausted cadence: kill switch is still the only reason
    decision = ev(config, tmp_path, proposal(notional="400"), day_one_snapshot(), today=5, week=9)
    assert len(decision.reasons) == 1
    assert "kill switch" in decision.reasons[0]


def test_absent_switch_file_passes(config, tmp_path):
    decision = ev(config, tmp_path, proposal(notional="50.00"), day_one_snapshot())
    assert decision.verdict == "approved"


# -- cadence ---------------------------------------------------------------------


def test_day_cap_boundary(config, tmp_path):
    ok = ev(config, tmp_path, proposal(notional="50.00"), day_one_snapshot(), today=0)
    assert ok.verdict == "approved"
    blocked = ev(config, tmp_path, proposal(notional="50.00"), day_one_snapshot(), today=1)
    assert blocked.verdict == "blocked"
    assert "daily proposal cap" in blocked.reasons[0]


def test_week_cap(config, tmp_path):
    blocked = ev(config, tmp_path, proposal(notional="50.00"), day_one_snapshot(), week=5)
    assert blocked.verdict == "blocked"
    assert "weekly proposal cap" in blocked.reasons[0]


# -- per-asset cap ------------------------------------------------------------------


def test_asset_value_counts_target_share_of_cash(config):
    # day one: BTC (40% weight) of £500 cash -> £200
    assert asset_value_gbp(day_one_snapshot(), "BTC", config) == D("200.00")
    # with holdings: £300 of BTC + 40% of £100 cash
    snapshot = PortfolioSnapshot(cash_gbp=D("100"), holdings_value_gbp={"BTC": D("300")})
    assert asset_value_gbp(snapshot, "BTC", config) == D("340.00")


def test_day_one_floor_is_the_effective_cap(config, tmp_path):
    """Day one, BTC: pct cap = 20% of £200 = £40 < £50 floor, so the floor
    binds — the £60 proposal is resized to £50, not blocked (floor-wins rule,
    Harry 2026-07-03). At v1.2 weights this is the normal day-one path."""
    decision = ev(config, tmp_path, proposal(notional="60.00"), day_one_snapshot())
    assert decision.verdict == "resized"
    assert decision.proposal.notional_gbp == D("50.00")
    assert "trade floor is the effective cap" in decision.proposal.rationale
    assert "below the £50.00 trade floor" in decision.reasons[0]


def test_percentage_cap_binds_when_allocation_is_large(config, tmp_path):
    # BTC worth £600 held + 40% of £500 cash -> £800; pct cap £160
    snapshot = PortfolioSnapshot(cash_gbp=D("500"), holdings_value_gbp={"BTC": D("600")})
    decision = ev(config, tmp_path, proposal(notional="200.00"), snapshot)
    assert decision.verdict == "resized"
    assert decision.proposal.notional_gbp == D("160.00")
    assert "20% asset cap" in decision.proposal.rationale
    assert "trade floor is the effective cap" not in decision.proposal.rationale


def test_pct_cap_exactly_at_floor_uses_percentage_note(config, tmp_path):
    # BTC allocation exactly £250 -> pct cap exactly £50 = floor: not a
    # conflict, so the percentage wording applies
    snapshot = PortfolioSnapshot(cash_gbp=D("0"), holdings_value_gbp={"BTC": D("250")})
    decision = ev(config, tmp_path, proposal(notional="60.00"), snapshot)
    assert decision.proposal.notional_gbp == D("50.00")
    assert "20% asset cap" in decision.proposal.rationale


def test_at_cap_passes_byte_identical(config, tmp_path):
    prop = proposal(notional="50.00")
    decision = ev(config, tmp_path, prop, day_one_snapshot())
    assert decision.verdict == "approved"
    assert decision.proposal is prop
    assert decision.reasons == ()


def test_sub_floor_arrival_blocked_never_raised(config, tmp_path):
    decision = ev(config, tmp_path, proposal(notional="10.00"), day_one_snapshot())
    assert decision.verdict == "blocked"
    assert decision.proposal is None
    assert "below" in decision.reasons[0] and "floor" in decision.reasons[0]


def test_satellite_cap_uses_its_own_weight(config, tmp_path):
    # HYPE (10% weight): day one allocation £50 -> pct cap £10 -> floor binds
    decision = ev(config, tmp_path, proposal(asset="HYPE", notional="60.00"), day_one_snapshot())
    assert decision.verdict == "resized"
    assert decision.proposal.notional_gbp == D("50.00")
    assert "HYPE" in decision.reasons[0]


# -- drift ----------------------------------------------------------------------------


def test_all_in_one_asset_flags_the_big_deviations_only(config):
    snapshot = PortfolioSnapshot(cash_gbp=D("400"), holdings_value_gbp={"BTC": D("100")})
    flags = check_drift(snapshot, config)
    # BTC 100% vs 40% (60pts), ETH 0% vs 25% (25pts), SOL 0% vs 15% (15pts)
    # are flagged; SUI/HYPE 0% vs 10% is exactly at the ±10pt threshold,
    # which is tolerated (strictly-greater convention)
    flagged = {f.split(" ")[0] for f in flags}
    assert flagged == {"BTC", "ETH", "SOL"}


def test_holdings_at_target_no_flags(config):
    snapshot = PortfolioSnapshot(
        cash_gbp=D("0"),
        holdings_value_gbp={
            "BTC": D("200"), "ETH": D("125"), "SOL": D("75"),
            "SUI": D("50"), "HYPE": D("50"),
        },
    )
    assert check_drift(snapshot, config) == ()


def test_zero_holdings_no_flags(config):
    assert check_drift(day_one_snapshot(), config) == ()


def test_drift_boundary_strictly_greater(config):
    # BTC 50.0% vs 40% target is exactly 10.0pts: tolerated; 50.5% is not
    at_threshold = PortfolioSnapshot(
        cash_gbp=D("0"),
        holdings_value_gbp={"BTC": D("50"), "ETH": D("25"), "SOL": D("15"),
                            "SUI": D("5"), "HYPE": D("5")},
    )
    assert not any(f.startswith("BTC") for f in check_drift(at_threshold, config))
    beyond = PortfolioSnapshot(
        cash_gbp=D("0"),
        holdings_value_gbp={"BTC": D("50.5"), "ETH": D("24.75"), "SOL": D("14.85"),
                            "SUI": D("4.95"), "HYPE": D("4.95")},
    )
    assert any(f.startswith("BTC") for f in check_drift(beyond, config))


def test_drift_flags_attached_to_blocked_decisions(config, tmp_path):
    snapshot = PortfolioSnapshot(cash_gbp=D("400"), holdings_value_gbp={"BTC": D("100")})
    decision = ev(config, tmp_path, proposal(notional="10.00"), snapshot)
    assert decision.verdict == "blocked"
    assert decision.drift_flags != ()


def test_decision_shape(config, tmp_path):
    decision = ev(config, tmp_path, proposal(notional="50.00"), day_one_snapshot())
    assert isinstance(decision, RiskDecision)
