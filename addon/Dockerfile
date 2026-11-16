ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache python3 py3-pip py3-aiohttp py3-pillow

WORKDIR /app
COPY oeplhub /app/oeplhub
COPY tagtypes /app/tagtypes
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
