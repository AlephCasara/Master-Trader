"""download_funding_rates.py must never freeze a pair's funding file (#113).

The old check rejected a pair's whole batch when any rate exceeded ±0.0075.
`--incremental` rewinds 24h before the last saved event, so the rejected row
sat in every later window and the file never advanced again. FundingFadeV1's
per-pair staleness guard then blocked that pair's entries for good.

Contract pinned here:
  - a real settlement beyond ±0.75% (ZEC printed -1.64% on 2025-04-07; Binance
    caps ZEC at ±2%) is saved like any other row;
  - a value no Binance cap allows (beyond ±3%), a NaN or a malformed string is
    quarantined for that row only, logged as QUARANTINE, and the rest of the
    batch is saved;
  - across repeated incremental runs the file keeps advancing.

Feather I/O needs pyarrow, which the CI requirements do not install, so the
tests swap feather for pickle. Everything else in save_pair runs as written.
"""

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
EVENT_MS = 8 * 3600 * 1000
T0_MS = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp() * 1000)


@pytest.fixture
def dl(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "download_funding_rates_under_test", ROOT / "ft_userdata" / "download_funding_rates.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "FUNDING_DIR", tmp_path)
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)
    monkeypatch.setattr(pd.DataFrame, "to_feather", lambda self, path: self.to_pickle(path))
    monkeypatch.setattr(module.pd, "read_feather", pd.read_pickle)
    module.logged = []
    monkeypatch.setattr(module, "log", module.logged.append)
    return module


def _record(i, rate):
    return {"symbol": "ZECUSDT", "fundingTime": T0_MS + i * EVENT_MS, "fundingRate": rate}


def _feather(dl, pair="ZEC/USDT"):
    return pd.read_pickle(dl.FUNDING_DIR / f"{pair.replace('/', '_')}-funding.feather")


def _seed(dl, n=6, pair="ZEC/USDT"):
    assert dl.save_pair(pair, [_record(i, "0.00010000") for i in range(n)]) == n


def test_genuine_rate_beyond_old_cap_is_saved(dl):
    _seed(dl)
    batch = [_record(5, "0.00010000"), _record(6, "-0.01639700"), _record(7, "0.00005000")]

    assert dl.save_pair("ZEC/USDT", batch) == 8

    df = _feather(dl)
    assert df["date"].iloc[-1] == pd.Timestamp(T0_MS + 7 * EVENT_MS, unit="ms", tz="UTC")
    assert df["funding_rate"].iloc[6] == pytest.approx(-0.016397)
    assert not any("QUARANTINE" in line or "REJECT" in line for line in dl.logged)


@pytest.mark.parametrize("bad", ["0.25000000", "-0.03000100", "NaN", "", "abc", None])
def test_bad_row_is_quarantined_and_rest_of_batch_saved(dl, bad):
    _seed(dl)
    batch = [_record(5, "0.00010000"), _record(6, bad), _record(7, "0.00020000")]

    assert dl.save_pair("ZEC/USDT", batch) == 7

    df = _feather(dl)
    assert pd.Timestamp(T0_MS + 6 * EVENT_MS, unit="ms", tz="UTC") not in set(df["date"])
    assert df["date"].iloc[-1] == pd.Timestamp(T0_MS + 7 * EVENT_MS, unit="ms", tz="UTC")
    assert df["funding_rate"].iloc[-1] == pytest.approx(0.0002)
    assert df["funding_rate"].abs().max() <= dl.FUNDING_RATE_SANITY_BOUND
    quarantined = [line for line in dl.logged if "QUARANTINE ZEC/USDT" in line]
    assert len(quarantined) == 1
    assert "dropped 1 of 3" in quarantined[0]
    assert "2026-09-03T00:00Z" in quarantined[0]


def test_bound_is_inclusive(dl):
    batch = [_record(0, "0.03000000"), _record(1, "-0.03000000"), _record(2, "0.03000001")]

    assert dl.save_pair("ZEC/USDT", batch) == 2
    assert list(_feather(dl)["funding_rate"]) == [0.03, -0.03]


def test_all_rows_bad_leaves_existing_file_untouched(dl):
    _seed(dl)
    before = _feather(dl)

    assert dl.save_pair("ZEC/USDT", [_record(6, "0.5"), _record(7, "NaN")]) == 0

    pd.testing.assert_frame_equal(_feather(dl), before)
    assert any("dropped 2 of 2" in line for line in dl.logged)


def test_incremental_runs_keep_advancing_past_bad_rows(dl, monkeypatch):
    """The production loop: hourly --incremental runs, each rewinding 24h.

    Event 9 is a genuine ZEC-scale settlement, event 10 a value no cap allows.
    Under the old check both kept the file pinned at event 8 forever.
    """
    rates = {i: "0.00010000" for i in range(30)}
    rates[9] = "-0.01639700"
    rates[10] = "0.25000000"

    def fake_fetch(symbol, start_ms, end_ms):
        return [_record(i, r) for i, r in rates.items() if start_ms <= T0_MS + i * EVENT_MS < end_ms]

    monkeypatch.setattr(dl, "fetch_funding_history", fake_fetch)

    def run(end_day):
        monkeypatch.setattr(sys, "argv", [
            "download_funding_rates.py", "--incremental", "--pairs", "ZEC/USDT",
            "--start", "20260901", "--end", end_day,
        ])
        dl.main()
        return _feather(dl)["date"].iloc[-1]

    assert run("20260904") == pd.Timestamp("2026-09-03T16:00Z")  # events 0-8
    assert run("20260905") == pd.Timestamp("2026-09-04T16:00Z")  # 9 and 10 in window
    assert run("20260908") == pd.Timestamp("2026-09-07T16:00Z")  # 10 still re-fetched

    df = _feather(dl)
    by_date = dict(zip(df["date"], df["funding_rate"]))
    assert by_date[pd.Timestamp("2026-09-04T00:00Z")] == pytest.approx(-0.016397)
    assert pd.Timestamp("2026-09-04T08:00Z") not in by_date
    assert len(df) == 20  # events 0-20 less the quarantined one
