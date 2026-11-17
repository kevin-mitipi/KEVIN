"""Tell the user when a newer release of this integration is published.

The Marketplace (Home Assistant 2026.11+) and HACS (2026.10 and older) show updates once
they re-read the repository on their own schedule. This module only checks the latest
GitHub release and, while it is newer than the installed version, keeps a Repairs issue
that links to the release. It never touches the stores or their entities.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any

from aiohttp import ClientError, ClientTimeout
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .const import DOMAIN, INTEGRATION_VERSION
from .environment import GITHUB_REPOSITORY

_LOGGER = logging.getLogger(__name__)

LATEST_RELEASE_URL = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
RELEASES_URL = f"https://github.com/{GITHUB_REPOSITORY}/releases"
CHECK_INTERVAL = timedelta(hours=6)
FIRST_CHECK_DELAY = 120  # seconds; keep the GitHub call out of Home Assistant's start
ISSUE_ID = "update_available"
_DATA_KEY = f"{DOMAIN}_update_check"

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def parse_version(value: str | None) -> tuple[int, int, int] | None:
    """`v0.4.1` / `0.4.1` → (0, 4, 1); anything else (pre-releases, hashes) → None."""
    match = _VERSION_RE.match((value or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None  # type: ignore[return-value]


def is_newer(latest: str | None, installed: str = INTEGRATION_VERSION) -> bool:
    """True only when both versions parse and `latest` is strictly higher."""
    a, b = parse_version(latest), parse_version(installed)
    return a is not None and b is not None and a > b


async def async_fetch_latest_tag(hass: HomeAssistant) -> str | None:
    """Tag of the latest published GitHub release, or None when GitHub is unreachable."""
    session = async_get_clientsession(hass)
    try:
        async with session.get(
            LATEST_RELEASE_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"mitipi_kevin/{INTEGRATION_VERSION}",
            },
            timeout=ClientTimeout(total=20),
        ) as resp:
            if resp.status != 200:
                _LOGGER.debug("Latest release check returned HTTP %s", resp.status)
                return None
            data: dict[str, Any] = await resp.json()
    except (ClientError, TimeoutError, ValueError) as err:
        _LOGGER.debug("Latest release check failed: %s", err)
        return None
    if data.get("draft") or data.get("prerelease"):
        return None
    tag = data.get("tag_name")
    return tag if isinstance(tag, str) else None


async def async_check_for_update(hass: HomeAssistant) -> str:
    """Run one check.

    Returns `current`, `repair` (a newer release is out) or `unknown` (GitHub unreachable).
    """
    latest = await async_fetch_latest_tag(hass)
    if latest is None:
        return "unknown"
    if not is_newer(latest):
        ir.async_delete_issue(hass, DOMAIN, ISSUE_ID)
        return "current"
    _LOGGER.info("KEVIN %s is available (installed %s)", latest, INTEGRATION_VERSION)
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_ID,
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        learn_more_url=f"{RELEASES_URL}/tag/{latest}",
        translation_key=ISSUE_ID,
        translation_placeholders={"latest": latest, "installed": INTEGRATION_VERSION},
    )
    return "repair"


@callback
def async_setup_update_check(hass: HomeAssistant) -> CALLBACK_TYPE:
    """Start the periodic check once per Home Assistant, shared by all config entries.

    Returns a release callback; the timers stop when the last entry releases it.
    """
    state = hass.data.get(_DATA_KEY)
    if state is None:

        async def _run(_now: Any = None) -> None:
            await async_check_for_update(hass)

        state = {
            "users": 0,
            "unsubs": [
                async_call_later(hass, FIRST_CHECK_DELAY, _run),
                async_track_time_interval(hass, _run, CHECK_INTERVAL),
            ],
        }
        hass.data[_DATA_KEY] = state
    state["users"] += 1

    @callback
    def _release() -> None:
        current = hass.data.get(_DATA_KEY)
        if current is None:
            return
        current["users"] -= 1
        if current["users"] <= 0:
            for unsub in current["unsubs"]:
                unsub()
            hass.data.pop(_DATA_KEY, None)

    return _release
