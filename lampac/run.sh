#!/bin/sh
# Налаштування, кеш і БД Lampac живуть у /config (папка аддона), щоб переживати оновлення.
# Локальні серії/фільми — у /share/lampac (видно в Лампі через модуль DLNA).
set -e
C=/config

mkdir -p "$C/cache" "$C/database" /share/lampac
[ -f "$C/init.conf" ] || cp /defaults/init.conf "$C/init.conf"
if [ ! -f "$C/passwd" ]; then
  head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$C/passwd"
  echo "Створено пароль root для Lampac: $C/passwd"
fi

for f in init.conf passwd cache database; do
  rm -rf "/lampac/$f"
  ln -s "$C/$f" "/lampac/$f"
done
chown -R 1000:1000 "$C" /share/lampac

exec setpriv --reuid=1000 --regid=1000 --init-groups /usr/share/dotnet/dotnet Core.dll
