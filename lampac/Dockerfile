FROM ghcr.io/lampac-nextgen/lampac:1.90.0

USER root
COPY run.sh /run.sh
RUN chmod +x /run.sh

WORKDIR /lampac
ENTRYPOINT ["/run.sh"]
