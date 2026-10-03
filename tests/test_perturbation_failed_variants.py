"""Perturbation must not report PASS/100 when its variant backtests failed (#98).

A variant whose backtest returns nothing is kept with `error: True`. With every
variant of a parameter failed, the deviation step fell through to
`pct_change = 0.0`, which classified the parameter LOW; with every parameter
failed the run came out PASS with stability 100 and claimed 13 backtests. On
the report card that looked exactly like a strategy whose parameters were all
tested and all held up, and the 100 fed the combined robustness score.

Contract pinned here:
  - a parameter with no successful variant is UNMEASURED and does not count
    toward the verdict or the average;
  - when no parameter was measured the result is SKIP with no score, the same
    as having nothing to perturb, so the combined score falls back to MC alone;
  - successful/attempted variant counts are recorded per parameter and overall,
    and the report card shows them whenever any variant failed.
"""

import importlib
from pathlib import Path

import pytest

FT_DIR = Path(__file__).parent.parent / "ft_userdata"
BASE_PARAMS = {"stoploss": -0.1, "minimal_roi": {"0": 0.05, "60": 0.02}}  # 3 numeric params
BASE_METRICS = {"total_profit": 100.0, "profit_factor": 1.5, "max_drawdown_pct": 5.0}
TRADES = [
    {"profit_abs": 5.0 if i % 3 else -3.0, "close_date": f"2026-01-{i + 1:02d}"}
    for i in range(30)
]


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    """Redirect HOME before importing: the engine modules resolve paths off it."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOME", str(tmp_path_factory.mktemp("home")))
        mp.syspath_prepend(str(FT_DIR))
        yield importlib.import_module("backtest_engine")


@pytest.fixture
def mc(engine, tmp_path, monkeypatch):
    module = importlib.import_module("engine.monte_carlo")
    monkeypatch.setattr(module, "RESULTS_DIR", tmp_path / "engine_results")
    monkeypatch.setattr(
        module, "build_backtest_config",
        lambda strategy_name, pairs, param_overrides=None: dict(param_overrides or {}),
    )
    return module


def _runner(variant_metrics):
    """Base run (params == BASE_PARAMS) succeeds; variants get variant_metrics(params)."""
    def run(strategy_name, config, timerange):
        if config == BASE_PARAMS:
            return dict(BASE_METRICS)
        return variant_metrics(config)
    return run


def _perturb(mc, monkeypatch, variant_metrics):
    monkeypatch.setattr(mc, "_run_docker_backtest", _runner(variant_metrics))
    return mc.run_parameter_perturbation(
        strategy_name="KeltnerBounceV1",
        base_params=BASE_PARAMS,
        pairs=["BTC/USDT"],
        timerange="20260101-20260201",
        perturb_pcts=[10, 20],
    )


def test_every_variant_failing_is_not_pass_100(mc, monkeypatch):
    result = _perturb(mc, monkeypatch, lambda params: None)

    assert result["overall"] == "SKIP"
    assert result["stability_score"] is None
    assert result["variants_attempted"] == 12
    assert result["variants_succeeded"] == 0
    assert result["total_backtests_run"] == 13  # attempts, base included
    assert "0 of 12" in result["reason"]
    for param in result["per_param"].values():
        assert param["sensitivity"] == "UNMEASURED"
        assert param["pct_change"] is None
        assert (param["variants_succeeded"], param["variants_attempted"]) == (0, 4)


def test_all_variants_succeeding_unchanged(mc, monkeypatch):
    """Control from the issue's table: a genuinely flat strategy still reads PASS/100."""
    result = _perturb(mc, monkeypatch, lambda params: dict(BASE_METRICS))

    assert result["overall"] == "PASS"
    assert result["stability_score"] == 100
    assert (result["variants_succeeded"], result["variants_attempted"]) == (12, 12)
    assert all(p["sensitivity"] == "LOW" for p in result["per_param"].values())


def test_unmeasured_param_does_not_count_toward_verdict(mc, monkeypatch):
    """stoploss variants all fail; the ROI params are measured and decide the verdict."""
    def variants(params):
        if params["stoploss"] != BASE_PARAMS["stoploss"]:
            return None
        return dict(BASE_METRICS, total_profit=150.0)  # +50% on every ROI variant -> HIGH

    result = _perturb(mc, monkeypatch, variants)

    assert result["per_param"]["stoploss"]["sensitivity"] == "UNMEASURED"
    assert result["params_unmeasured"] == ["stoploss"]
    assert [p["sensitivity"] for k, p in result["per_param"].items() if k != "stoploss"] == [
        "HIGH", "HIGH",
    ]
    assert result["overall"] == "FAIL"
    assert result["avg_sensitivity_pct"] == 50.0  # mean of the two measured params only
    assert (result["variants_succeeded"], result["variants_attempted"]) == (8, 12)


def test_partial_failure_is_recorded_per_param(mc, monkeypatch):
    """One failed variant per param: still measured, but the shortfall is on record."""
    def variants(params):
        if params["stoploss"] == round(-0.1 * 0.8, 8) or 0.04 in params["minimal_roi"].values() \
                or 0.016 in params["minimal_roi"].values():
            return None
        return dict(BASE_METRICS)

    result = _perturb(mc, monkeypatch, variants)

    assert result["overall"] == "PASS"
    assert (result["variants_succeeded"], result["variants_attempted"]) == (9, 12)
    assert all(
        (p["variants_succeeded"], p["variants_attempted"]) == (3, 4)
        for p in result["per_param"].values()
    )


def test_robustness_stage_does_not_carry_failed_perturbation(mc, monkeypatch):
    """The combined score must come from MC alone, not MC blended with a phantom 100."""
    monkeypatch.setattr(mc, "_run_docker_backtest", _runner(lambda params: None))
    result = mc.run_robustness_stage(
        strategy_name="KeltnerBounceV1",
        trades=TRADES,
        base_params=BASE_PARAMS,
        pairs=["BTC/USDT"],
        timerange="20260101-20260201",
        mode_config={"mc_iterations": 50, "perturb_pcts": [10, 20]},
    )

    assert result["perturbation"]["overall"] == "SKIP"
    assert result["combined_score"] == result["monte_carlo"]["mc_score"]
    assert "0 of 12" in result["perturbation_skip_reason"]


def _card_lines(engine, perturbation):
    reporting = importlib.import_module("engine.reporting")
    card = reporting.build_report_card(
        "KeltnerBounceV1", {"robustness": {"monte_carlo": None, "perturbation": perturbation}},
    )
    lines = card.splitlines()
    i = next(n for n, line in enumerate(lines) if "Perturbation:" in line)
    return lines[i], lines[i + 1]


def test_report_card_all_failed_is_skip_with_counts(engine, mc, monkeypatch):
    perturbation = _perturb(mc, monkeypatch, lambda params: None)

    line, note = _card_lines(engine, perturbation)

    assert "SKIP" in line and "PASS" not in line and "100" not in line
    assert "0/12 variant backtests ran" in note


def test_report_card_partial_run_shows_counts(engine, mc, monkeypatch):
    perturbation = _perturb(
        mc, monkeypatch,
        lambda params: None if params["stoploss"] != BASE_PARAMS["stoploss"] else dict(BASE_METRICS),
    )

    line, note = _card_lines(engine, perturbation)

    assert "PASS (stability 100/100)" in line
    assert "8/12 variant backtests ran" in note
    assert len(note) == len(line), "the note must stay inside the box"


def test_report_card_complete_run_has_no_note(engine, mc, monkeypatch):
    perturbation = _perturb(mc, monkeypatch, lambda params: dict(BASE_METRICS))

    line, after = _card_lines(engine, perturbation)

    assert "PASS (stability 100/100)" in line
    assert "variant backtests ran" not in after
