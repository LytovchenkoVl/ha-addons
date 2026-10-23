"""Клієнт сайтів ДТЕК через справжній браузер (Playwright Chromium).

Сайти ДТЕК стоять за антибот-захистом, тому прямі HTTP-запити блокуються.
Ми відкриваємо сторінку /ua/shutdowns у headless Chromium і робимо ті самі
AJAX-запити (/ua/ajax), що й сама сторінка: з її cookies та CSRF-токеном.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

log = logging.getLogger("dtek")

REGIONS = {
    # Сайт КЕМ (місто Київ) не має населених пунктів: лише список вулиць, і будинки шукаються за вулицею
    "kem": {"name": "Київ", "full": "ДТЕК Київські електромережі", "url": "https://www.dtek-kem.com.ua",
            "city": "м. Київ"},
    "krem": {"name": "Київська область", "full": "ДТЕК Київські регіональні електромережі", "url": "https://www.dtek-krem.com.ua",
             "example": "Бровари"},
    "dnem": {"name": "Дніпропетровська область", "full": "ДТЕК Дніпровські електромережі", "url": "https://www.dtek-dnem.com.ua",
             "example": "Павлоград"},
    "oem": {"name": "Одеська область", "full": "ДТЕК Одеські електромережі", "url": "https://www.dtek-oem.com.ua",
            "example": "Чорноморськ"},
}

# Повний перезапуск сторінки раз на стільки секунд (оновлює cookies і CSRF)
PAGE_MAX_AGE = 30 * 60
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

# Той самий запит, що робить jQuery на сторінці ($.post('/ua/ajax', ...))
AJAX_JS = """
async (params) => {
  const meta = document.querySelector('meta[name="csrf-token"]');
  const body = new URLSearchParams();
  for (const [k, v] of params) body.append(k, v);
  const r = await fetch('/ua/ajax', {
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
      'X-Requested-With': 'XMLHttpRequest',
      'X-CSRF-Token': meta ? meta.content : ''
    },
    body
  });
  return { status: r.status, text: await r.text() };
}
"""


class DtekError(Exception):
    pass


class DtekSite:
    def __init__(self, key: str, ctx: BrowserContext):
        self.key = key
        self.base = REGIONS[key]["url"]
        self.ctx = ctx
        self.page: Page | None = None
        self.loaded_at = 0.0
        self.lock = asyncio.Lock()
        self.streets: dict[str, list[str]] = {}
        self.has_cities = True
        self.names: dict[str, str] = {}

    async def _load(self):
        if self.page:
            try:
                await self.page.close()
            except Exception:
                pass
        self.page = await self.ctx.new_page()
        url = self.base + "/ua/shutdowns"
        log.info("[%s] відкриваю %s", self.key, url)
        await self.page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        # антибот-перевірка може кілька секунд перенаправляти — чекаємо, поки з'явиться розклад
        await self.page.wait_for_function(
            "() => typeof DisconSchedule !== 'undefined' && DisconSchedule.fact && DisconSchedule.streets",
            timeout=90_000,
        )
        info = await self.page.evaluate(
            "() => ({streets: DisconSchedule.streets, names: (DisconSchedule.preset||{}).sch_names || {}})"
        )
        streets = info.get("streets") or {}
        if isinstance(streets, list):
            # Київ: замість {населений пункт: [вулиці]} сайт віддає просто [вулиці]
            self.has_cities = False
            self.streets = {REGIONS[self.key].get("city", "—"): streets}
        else:
            self.has_cities = True
            self.streets = streets
        self.names = info.get("names") or {}
        self.loaded_at = time.monotonic()
        log.info("[%s] сторінка готова: %d населених пунктів", self.key, len(self.streets))

    async def _ensure(self):
        if not self.page or self.page.is_closed() or time.monotonic() - self.loaded_at > PAGE_MAX_AGE:
            await self._load()

    async def _ajax(self, params: list[tuple[str, str]]) -> dict:
        async with self.lock:
            for attempt in (1, 2):
                try:
                    await self._ensure()
                    res = await self.page.evaluate(AJAX_JS, params)
                    if res["status"] == 200:
                        data = json.loads(res["text"])
                        if data.get("result"):
                            return data
                        raise DtekError("result=false")
                    raise DtekError(f"HTTP {res['status']}")
                except Exception as e:  # noqa: BLE001
                    log.warning("[%s] запит не вдався (спроба %d): %s", self.key, attempt, e)
                    self.loaded_at = 0  # наступна спроба перезавантажить сторінку
                    if attempt == 2:
                        raise DtekError(str(e)) from e
                    await asyncio.sleep(3)

    async def get_fact(self) -> dict:
        """Актуальний графік (fact) для всіх черг. Стара дата змушує сайт віддати повні дані."""
        data = await self._ajax([("method", "checkDisconUpdate"), ("update", "01.01.2000 00:00")])
        fact = data.get("fact") or {}
        preset = data.get("preset") or {}
        if preset.get("sch_names"):
            self.names = preset["sch_names"]
        return fact

    async def get_streets(self) -> dict[str, list[str]]:
        async with self.lock:
            await self._ensure()
        return self.streets

    async def get_houses(self, city: str, street: str) -> dict:
        """Будинки вулиці: {номер: {sub_type_reason: [черги], sub_type, start_date, end_date, ...}}."""
        await self.get_streets()  # сторінка має бути завантажена, щоб знати, чи є на сайті поле «населений пункт»
        fields = ([("city", city)] if self.has_cities else []) + [("street", street), ("updateFact", "01.01.2000 00:00")]
        params = [("method", "getHomeNum")]
        for i, (n, v) in enumerate(fields):
            params += [(f"data[{i}][name]", n), (f"data[{i}][value]", v)]
        data = await self._ajax(params)
        houses = data.get("data") or {}
        return houses if isinstance(houses, dict) else {}


class Dtek:
    """Один браузер на всі регіони, по сторінці на регіон (відкривається, лише коли потрібна)."""

    def __init__(self):
        self._pw = None
        self.browser: Browser | None = None
        self.ctx: BrowserContext | None = None
        self.sites: dict[str, DtekSite] = {}

    async def start(self):
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        )
        self.ctx = await self.browser.new_context(
            user_agent=UA, locale="uk-UA", timezone_id="Europe/Kyiv",
            viewport={"width": 1280, "height": 900},
        )
        # картинки, шрифти й відео не потрібні — менше навантаження на сервер
        await self.ctx.route(
            "**/*",
            lambda route: route.abort() if route.request.resource_type in ("image", "font", "media")
            else route.continue_(),
        )

    def site(self, key: str) -> DtekSite:
        if key not in self.sites:
            self.sites[key] = DtekSite(key, self.ctx)
        return self.sites[key]

    async def stop(self):
        try:
            if self.browser:
                await self.browser.close()
        finally:
            if self._pw:
                await self._pw.stop()
