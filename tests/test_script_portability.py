"""Portability contracts for operator/research shell scripts.

These checks are intentionally static: PR CI must not start Docker, install
crontabs, or touch production. They catch workstation-specific assumptions that
previously survived Ubuntu CI because the scripts were never invoked there.
"""

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]

PORTABLE_BASH_SCRIPTS = (
    ROOT / "ft_userdata" / "automation_scheduler.sh",
    ROOT / "ft_userdata" / "test_4h_migration.sh",
    ROOT / "ft_userdata" / "backtest.sh",
)


def _text(path: Path) -> str:
    return path.read_text()


def test_portable_bash_scripts_parse_on_ubuntu_ci():
    for script in PORTABLE_BASH_SCRIPTS:
        result = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"{script}: {result.stderr}"


def test_portable_scripts_do_not_use_bsd_date_v():
    scheduler = _text(ROOT / "ft_userdata" / "automation_scheduler.sh")
    migration = _text(ROOT / "ft_userdata" / "test_4h_migration.sh")

    assert "date -u -v" not in scheduler
    assert "date -u -v" not in migration
    assert "timedelta(days=90)" in scheduler
    assert "timedelta(days=days)" in migration


def test_generic_backtest_wrapper_has_no_personal_mac_mount():
    script = _text(ROOT / "ft_userdata" / "backtest.sh")

    assert "/Users/" not in script
    assert 'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"' in script
    assert '-v "$SCRIPT_DIR/user_data:/freqtrade/user_data"' in script


def test_migration_script_resolves_checkout_instead_of_home_layout():
    script = _text(ROOT / "ft_userdata" / "test_4h_migration.sh")

    assert "cd ~/ft_userdata" not in script
    assert "-v ~/ft_userdata" not in script
    assert 'cd "$SCRIPT_DIR"' in script
    assert '-v "$SCRIPT_DIR/user_data:/freqtrade/user_data"' in script


def test_scheduler_cron_uses_installing_checkout():
    script = _text(ROOT / "ft_userdata" / "automation_scheduler.sh")

    assert "cd ~/ft_userdata" not in script
    assert 'MT_FT_DIR=\"$FT_DIR\"' in script
    assert 'cd "$MT_FT_DIR"' in script
