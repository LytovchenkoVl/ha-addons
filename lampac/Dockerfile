FROM ghcr.io/lampac-nextgen/lampac:1.90.0

USER root
COPY init.conf /defaults/init.conf
COPY dlna-playlist.js /defaults/dlna-playlist.js
COPY run.sh /run.sh

# DLNA-плагін для Лампи запускає один файл (playlist([video])). Підміняємо на всі файли папки,
# щоб наступна серія вмикалася сама.
RUN chmod +x /run.sh \
    && f=$(find / -path '*DLNA/plugin.js' -not -path '/proc/*' | head -1) \
    && test -n "$f" \
    && awk 'BEGIN{getline r < "/defaults/dlna-playlist.js"} {t="Lampa.Player.playlist([video]);"; i=index($0,t); if(i){$0=substr($0,1,i-1) r substr($0,i+length(t))} print}' "$f" > /tmp/p.js \
    && grep -q 'media_formats.indexOf(x)' /tmp/p.js \
    && cat /tmp/p.js > "$f" && rm /tmp/p.js \
    && echo "patched $f"

WORKDIR /lampac
ENTRYPOINT ["/run.sh"]
