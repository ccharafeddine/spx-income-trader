"""
Version information and update checking for SPX Income Trader.

Compares the local APP_VERSION against a remote version manifest
(JSON with a "version" key). The check is designed to run in a
background thread and silently fail if the network is unreachable.
"""

import json
import logging
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

APP_VERSION = "1.0.0"

# Default URL pointing to a raw JSON file in the repo.
# Override via strategy_params.yaml: app.update_check_url
DEFAULT_UPDATE_URL = (
    "https://raw.githubusercontent.com/ccharafeddine/spx-income-trader/main/version.json"
)

# Where users can download new releases
DEFAULT_DOWNLOAD_URL = (
    "https://github.com/ccharafeddine/spx-income-trader/releases/latest"
)


def _parse_semver(version_str):
    """Parse a 'major.minor.patch' string into a comparable tuple.

    Returns (major, minor, patch) as ints, or None on failure.
    """
    try:
        parts = version_str.strip().lstrip("v").split(".")
        return tuple(int(p) for p in parts[:3])
    except (ValueError, AttributeError):
        return None


def check_for_updates(update_url=None, download_url=None, timeout=5):
    """Check if a newer version is available.

    Fetches a JSON manifest from *update_url* and compares its
    ``version`` field against APP_VERSION using semver ordering.

    Args:
        update_url: URL to a JSON file with at least a ``version`` key.
        download_url: URL shown to the user for downloading updates.
        timeout: HTTP request timeout in seconds.

    Returns:
        dict with keys:
            current_version  - the running version string
            latest_version   - the remote version string (or None)
            update_available - bool
            download_url     - where to get the update (or None)
            error            - error message if the check failed (or None)

        The function never raises; network / parse failures are captured
        in the ``error`` field.
    """
    url = update_url or DEFAULT_UPDATE_URL
    dl_url = download_url or DEFAULT_DOWNLOAD_URL
    result = {
        "current_version": APP_VERSION,
        "latest_version": None,
        "update_available": False,
        "download_url": dl_url,
        "error": None,
    }

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SPXIncomeTrader"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        remote_version = data.get("version", "").strip()
        result["latest_version"] = remote_version

        local = _parse_semver(APP_VERSION)
        remote = _parse_semver(remote_version)

        if local and remote and remote > local:
            result["update_available"] = True
            # Allow the manifest to override the download link
            result["download_url"] = data.get("download_url", dl_url)

        logger.info(
            "Update check: current=%s, latest=%s, update_available=%s",
            APP_VERSION, remote_version, result["update_available"],
        )

    except (urllib.error.URLError, OSError) as exc:
        result["error"] = f"Network error: {exc}"
        logger.debug("Update check failed (network): %s", exc)
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        result["error"] = f"Parse error: {exc}"
        logger.debug("Update check failed (parse): %s", exc)

    return result
