"""Forward test arms for prereg ff-forward-v2-vs-no-protections-2026-10-01 (#109).

Both subclass the live FundingFadeV1 pinned by md5 in run_forward.sh. The only
difference is component C of the 2026-08-23 V2 change (CooldownPeriod +
StoplossGuard protections).
"""
from FundingFadeV1 import FundingFadeV1


class FFF_V2(FundingFadeV1):
    """Control: identical to live FundingFadeV1."""


class FFF_V2_noC(FundingFadeV1):
    """Challenger: live FundingFadeV1 without its protections."""

    @property
    def protections(self):
        return []
