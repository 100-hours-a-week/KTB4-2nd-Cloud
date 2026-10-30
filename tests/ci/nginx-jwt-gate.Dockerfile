# Same Ubuntu release and Nginx/njs package versions observed on the V1 App.
FROM ubuntu@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    nginx=1.24.0-2ubuntu7.19 nginx-common=1.24.0-2ubuntu7.19 \
    libnginx-mod-http-js=0.8.2-1ubuntu1 python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
