ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache python3 py3-pip py3-aiohttp py3-pillow font-dejavu
RUN pip3 install --no-cache-dir --break-system-packages segno==1.6.6

WORKDIR /app
COPY oeplhub /app/oeplhub
COPY tagtypes /app/tagtypes
COPY www /app/www
COPY config.yaml /app/config.yaml
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
