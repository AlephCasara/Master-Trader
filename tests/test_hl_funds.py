"""Pure helpers of the operator fund-moving script (#123); no SDK or Keychain."""
import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "hl_funds", Path(__file__).resolve().parents[1] / "scripts" / "hyperliquid" / "hl_funds.py")
hl_funds = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hl_funds)


@pytest.mark.parametrize("withdrawable, expected", [
    ("50.0", Decimal("49.00")),
    ("40.004", Decimal("39.00")),
    ("209.734477", Decimal("208.73")),
    ("1.0", Decimal("0")),
    ("0.4", Decimal("0")),
])
def test_send_amount_leaves_the_fee_and_never_rounds_up(withdrawable, expected):
    assert hl_funds.send_amount(withdrawable) == expected


def test_unknown_label_is_refused():
    with pytest.raises(SystemExit):
        hl_funds.service("binance")
    assert hl_funds.service("killers") == "master-trader.hyperliquid.killers.master"
