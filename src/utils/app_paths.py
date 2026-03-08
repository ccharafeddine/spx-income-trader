"""
Application path resolution for SPX Income Trader.

Determines the correct data directory based on runtime mode:
- Source mode (git repo / development): uses project root for backward compat
- Packaged mode (PyInstaller / py2app): uses platformdirs user_data_dir

Exposes: DATA_DIR, LOG_DIR, DB_PATH, TOKENS_DIR, CONFIG_DIR, BUNDLE_DIR
"""
from __future__ import annotations

import sys
import shutil
import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

APP_NAME = "SPXIncomeTrader"

# Messages collected during init (before logging is configured)
init_messages: list[str] = []


def _is_packaged() -> bool:
    """True when running inside a PyInstaller or py2app bundle."""
    if getattr(sys, '_MEIPASS', None) is not None:
        return True
    if getattr(sys, 'frozen', None) == 'macosx_app':
        return True
    return False


def _get_bundle_dir() -> Path:
    """Directory containing bundled resources.

    PyInstaller: sys._MEIPASS (temporary extraction root).
    py2app: <AppBundle>/Contents/Resources (where data files live).
    Source: project root.
    """
    if getattr(sys, '_MEIPASS', None) is not None:
        return Path(sys._MEIPASS)
    if getattr(sys, 'frozen', None) == 'macosx_app':
        # py2app places resources in <bundle>/Contents/Resources
        return Path(sys.executable).resolve().parent.parent / 'Resources'
    return _get_source_root()


def _get_source_root() -> Path:
    """Project root when running from source."""
    # src/utils/app_paths.py -> parent.parent.parent = project root
    return Path(__file__).resolve().parent.parent.parent


def _get_data_dir() -> Path:
    """User-writable data directory for persistent files."""
    if _is_packaged():
        from platformdirs import user_data_dir
        return Path(user_data_dir(APP_NAME))
    return _get_source_root()


def _deep_merge_missing(target: dict, defaults: dict, prefix: str = "") -> list[str]:
    """
    Recursively add keys from *defaults* that are absent in *target*.

    Existing user values are never overwritten.
    Returns a list of dotted key paths that were added.
    """
    added: list[str] = []
    for key, value in defaults.items():
        key_path = f"{prefix}.{key}" if prefix else str(key)
        if key not in target:
            target[key] = value
            added.append(key_path)
        elif isinstance(value, dict) and isinstance(target[key], dict):
            added.extend(_deep_merge_missing(target[key], value, key_path))
    return added


def _merge_config_updates(user_path: Path, default_path: Path) -> None:
    """
    Merge new fields from the bundled default config into the user's config.

    Only adds missing keys; never overwrites existing user values.
    Logs every newly added field.
    """
    with open(default_path, 'r', encoding='utf-8') as f:
        defaults = yaml.safe_load(f) or {}
    with open(user_path, 'r', encoding='utf-8') as f:
        user_config = yaml.safe_load(f) or {}

    added = _deep_merge_missing(user_config, defaults)

    if added:
        with open(user_path, 'w', encoding='utf-8') as f:
            yaml.dump(user_config, f, default_flow_style=False, sort_keys=False)
        for field in added:
            msg = f"Config updated - added new field: {field} (default value applied)"
            logger.info(msg)
            init_messages.append(msg)


def _initialize_packaged_dirs() -> None:
    """
    First-run setup for packaged mode.

    Creates all data directories and seeds / merges the default
    strategy_params.yaml into CONFIG_DIR.  Existing user files are
    never overwritten.
    """
    if not IS_PACKAGED:
        return

    # Create directory tree
    for directory in [LOG_DIR, DB_PATH.parent, TOKENS_DIR, CONFIG_DIR]:
        directory.mkdir(parents=True, exist_ok=True)

    # Seed or merge strategy_params.yaml
    user_config = CONFIG_DIR / 'strategy_params.yaml'
    bundled_config = BUNDLE_DIR / 'config' / 'strategy_params.yaml'

    if not bundled_config.exists():
        return

    if not user_config.exists():
        shutil.copy2(bundled_config, user_config)
        msg = f"Copied default strategy_params.yaml to {user_config}"
        logger.info(msg)
        init_messages.append(msg)
    else:
        _merge_config_updates(user_config, bundled_config)


# ---------------------------------------------------------------------------
# Public path constants
# ---------------------------------------------------------------------------

IS_PACKAGED: bool = _is_packaged()
BUNDLE_DIR: Path = _get_bundle_dir()
DATA_DIR: Path = _get_data_dir()
LOG_DIR: Path = DATA_DIR / 'logs'
DB_PATH: Path = DATA_DIR / 'database' / 'trades.db'
TOKENS_DIR: Path = DATA_DIR / 'tokens'
CONFIG_DIR: Path = DATA_DIR / 'config'

# Run first-time setup in packaged mode
_initialize_packaged_dirs()
