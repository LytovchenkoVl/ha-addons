# ha-addons — нотатки для Claude

Репозиторій доповнень Home Assistant. Встановлюється на домашньому сервері (Asus K55D, HAOS, amd64)
через Магазин доповнень → Репозиторії → `https://github.com/LytovchenkoVl/ha-addons`.
Спілкуємося українською.

## dtek_power_bot — Telegram-бот графіків відключень ДТЕК

- `app/bot.py` — Telegram (python-telegram-bot 21–22, job-queue): `/start` (регіон → адреса або черга), `/now`, `/stop`,
  опитування сайту кожні `poll_minutes`, підписки в `/data/subscriptions.json` (по одній на чат).
- `app/dtek.py` — Playwright Chromium відкриває `https://www.dtek-<krem|kem|dnem|oem>.com.ua/ua/shutdowns`
  (прямі HTTP-запити блокує антибот) і робить ті самі POST-запити на `/ua/ajax`, що й сторінка,
  з заголовком `X-CSRF-Token` з `<meta name="csrf-token">`:
  - `method=checkDisconUpdate&update=01.01.2000 00:00` → `{fact, preset}` для всіх черг;
  - `method=getHomeNum` + `data[i][name|value]` для `city`, `street`, `updateFact` → будинки
    `{номер: {sub_type_reason: ["GPV5.1"], sub_type, start_date, end_date}}`.
- `app/schedule.py` — розбір `fact`: `data[<unix ts півночі за Києвом>][GPVx.y][1..24]` зі станами
  `yes/no/first/second/maybe/mfirst/msecond`; `fact.update` — коли ДТЕК змінив графік.
  `updateTimestamp`, який сайт показує біля адреси, оновлюється постійно — для визначення змін не використовувати.
- Сповіщення лише коли змінився графік черг користувача або з'явився новий день; минулі дні ігноруються (без повідомлень опівночі).
- Доступ: `allow_all`, `allowed_users` (ID або @username), `deny_message` у налаштуваннях add-on.
  Якщо Telegram повертає Forbidden (бота заблокували / змінили токен) — підписка `paused`, відновлюється, коли людина напише боту.
- `/stats` лише для `admin_users` (ID або @username; адміни завжди мають доступ). Лічильники в `/data/stats.json`:
  `users` {id: first/last дата}, `sent` {дата: к-сть сповіщень, 60 днів}, `denied` (останні 20 запитів доступу).
  Також `polls` (стан перевірок сайту за регіонами), `since`, `restarts`. На старті `stats.started()` додає людей
  з підписок, яких немає в `users` (користувались до появи статистики; `first`/`last` порожні).
  Час роботи без перезапуску (`STARTED_AT`) за задумом скидається при кожному запуску.

## Перед випуском

- Збільшити `version` у `dtek_power_bot/config.yaml`, інакше HA не покаже оновлення.
- Структуру даних перевірено на сайті ДТЕК КРЕМ (23.10.2026); КЕМ, ДнЕМ, ОЕМ мають той самий рушій, але не перевірялися.
- Не перевірено вживу: чи пропускає антибот ДТЕК headless Chromium з домашнього IP — дивитися журнал add-on.
