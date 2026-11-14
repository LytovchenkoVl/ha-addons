"""Читання станів сутностей Home Assistant через Supervisor (для перемикача «Технічні роботи»)."""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("ha")

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
API = "http://supervisor/core/api"


async def get_state(entity_id: str) -> str | None:
    """Стан сутності ('on', текст input_text тощо) або None, якщо HA недоступний чи сутності немає."""
    if not SUPERVISOR_TOKEN or not entity_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{API}/states/{entity_id}",
                                 headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("state")
    except Exception as e:  # noqa: BLE001
        log.warning("не вдалося прочитати %s з Home Assistant: %s", entity_id, e)
        return None
