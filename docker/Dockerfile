FROM alpine:3.19

RUN apk add --no-cache \
    python3 \
    py3-pip \
    samba \
    samba-common-tools

RUN mkdir -p /app /config /merged /sources

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --break-system-packages -r /app/requirements.txt

COPY app/ /app/
COPY smb.conf /etc/samba/smb.conf
COPY entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh

EXPOSE 445
EXPOSE 8080

VOLUME ["/config", "/sources"]

ENTRYPOINT ["/entrypoint.sh"]
