FROM python:3.12-slim
WORKDIR /opt/yeodam
RUN install -d -m 0700 /run/yeodam-cutover
COPY deploy/request-cutover/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY scripts/request_cutover /opt/yeodam/request_cutover
STOPSIGNAL SIGINT
ENTRYPOINT ["python", "-m", "request_cutover.gate"]
