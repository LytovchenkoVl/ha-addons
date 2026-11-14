#!/bin/sh
# Швидке оновлення без збірки: якщо в папці аддона (/config/app) лежить новіша версія коду
# (файл VERSION новіший за версію образу) — запускаємо її. Оновлення з GitHub тієї ж або новішої
# версії автоматично повертає код з образу.
O=/config/app
if [ -f "$O/bot.py" ] && [ -f "$O/VERSION" ]; then
  V=$(tr -d ' \r\n' < "$O/VERSION")
  NEWEST=$(printf '%s\n%s\n' "${ADDON_VERSION:-0}" "$V" | sort -V | tail -1)
  if [ "$V" != "${ADDON_VERSION:-0}" ] && [ "$NEWEST" = "$V" ]; then
    echo "Запускаю код з $O (версія $V, образ ${ADDON_VERSION:-?})"
    exec python -u "$O/bot.py"
  fi
fi
exec python -u /app/bot.py
