#!/usr/bin/env python3
"""Move USDC between the fleet's own Hyperliquid accounts (#123). Operator-run.

Master keys live only in this Mac's Keychain, service
``master-trader.hyperliquid.<label>.master``; the item's account attribute is
the account address, so no address is hard-coded here. Every write shows the
balances and asks the terminal for a literal YES before reading a key.

    hl-funds.sh status
    hl-funds.sh to-perps killers          # spot USDC -> perps margin
    hl-funds.sh send insiders killers     # all withdrawable perps USDC, minus the send fee

Before moving money into or out of an account the circuit breaker watches
(binance-spot, hyperliquid-killers): stop ft-metrics-exporter, move, add the
rows to ft_userdata/account_transfers.json, deploy, restart the exporter.
"""
import subprocess
import sys
from decimal import ROUND_FLOOR, Decimal

LABELS = ("killers", "insiders", "short_keltner")
SEND_FEE = Decimal("1")  # observed on every Hyperliquid Send so far (2026-08-23, 2026-10-02)


def service(label):
    if label not in LABELS:
        sys.exit(f"unknown account label {label!r}; expected one of {', '.join(LABELS)}")
    return f"master-trader.hyperliquid.{label}.master"


def address(label):
    """Account address from the Keychain item's account attribute (no secret read)."""
    out = subprocess.run(["security", "find-generic-password", "-s", service(label)],
                         capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        if '"acct"' in line:
            return line.split("=", 1)[1].strip().strip('"')
    sys.exit(f"no account attribute on Keychain item {service(label)}")


def send_amount(withdrawable, fee=SEND_FEE):
    """Largest whole-cent amount that leaves room for the send fee; 0 if none."""
    amount = (Decimal(str(withdrawable)) - fee).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
    return max(amount, Decimal(0))


def confirm(prompt):
    print(f"{prompt} Type YES: ", end="", flush=True)
    with open("/dev/tty") as tty:  # stdin may be a heredoc/pipe; ask the terminal
        return tty.readline().strip() == "YES"


def _sdk():
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    return Info(constants.MAINNET_API_URL, skip_ws=True), constants


def balances(info, addr):
    spot = next((b for b in info.spot_user_state(addr)["balances"] if b["coin"] == "USDC"), None)
    state = info.user_state(addr)
    return {"spot_usdc": Decimal(spot["total"]) if spot else Decimal(0),
            "perps_value": Decimal(state["marginSummary"]["accountValue"]),
            "withdrawable": Decimal(state["withdrawable"]),
            "positions": len(state["assetPositions"])}


def exchange(label, addr, constants):
    from eth_account import Account
    from hyperliquid.exchange import Exchange
    key = subprocess.run(["security", "find-generic-password", "-s", service(label), "-w"],
                         capture_output=True, text=True, check=True).stdout.strip()
    wallet = Account.from_key(key)
    del key
    if wallet.address.lower() != addr.lower():
        sys.exit(f"Keychain key for {label} does not match {addr}; aborting.")
    return Exchange(wallet, constants.MAINNET_API_URL, account_address=addr)


def main(argv):
    if not argv or argv[0] not in ("status", "to-perps", "send"):
        sys.exit(__doc__)
    info, constants = _sdk()
    if argv[0] == "status":
        for label in LABELS:
            addr = address(label)
            print(label, addr, {k: str(v) for k, v in balances(info, addr).items()})
        return
    if argv[0] == "to-perps":
        label = argv[1]
        addr = address(label)
        amount = balances(info, addr)["spot_usdc"]
        if amount <= 0:
            sys.exit("Nothing in spot to move.")
        if not confirm(f"Move {amount} USDC spot -> perps on {label} ({addr})?"):
            sys.exit("Cancelled, nothing sent.")
        print("Result:", exchange(label, addr, constants).usd_class_transfer(float(amount), True))
        print("Now:", {k: str(v) for k, v in balances(info, addr).items()})
        return
    source, target = argv[1], argv[2]
    if source == target:
        sys.exit("source and target are the same account")
    src, dst = address(source), address(target)
    before = balances(info, src)
    if before["positions"]:
        sys.exit(f"{source} has open positions; close them before draining the account.")
    amount = send_amount(before["withdrawable"])
    if amount <= 0:
        sys.exit(f"{source} has nothing to send after the {SEND_FEE} USDC fee.")
    if not confirm(f"Send {amount} USDC from {source} ({src}) perps to {target} ({dst})?"):
        sys.exit("Cancelled, nothing sent.")
    print("Result:", exchange(source, src, constants).usd_transfer(float(amount), dst))
    print(source, "now:", {k: str(v) for k, v in balances(info, src).items()})
    print(target, "now:", {k: str(v) for k, v in balances(info, dst).items()})


if __name__ == "__main__":
    main(sys.argv[1:])
