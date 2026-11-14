# Lampac

Lampac NextGen (<https://github.com/lampac-nextgen/lampac>) на основі офіційного образу `ghcr.io/lampac-nextgen/lampac`.

- Адреса для Лампи: `http://<IP Home Assistant>:9118`.
- Налаштування — `init.conf` у папці аддона (`/addon_configs/<id>_lampac/`), перечитується на льоту.
  Під час першого запуску туди копіюється стандартний `init.conf` з образу.
- `passwd` — пароль root Lampac (створюється випадковий під час першого запуску).
- `cache/` і `database/` теж лежать у папці аддона й переживають оновлення.
