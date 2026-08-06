# warp-pool on fly.io
# 镜像: python3 + wireproxy(静态) + wg 工具, 部署逻辑全在 entrypoint.sh
# 构建: 在仓库根执行 `fly deploy`(fly.toml 已指向本 Dockerfile)
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl openssl wireguard-tools \
 && rm -rf /var/lib/apt/lists/*

ARG TARGETARCH
RUN set -eux; \
    VER=v1.0.9; \
    ARCH="${TARGETARCH:-amd64}"; \
    curl -fsSL -m 180 -o /tmp/wireproxy.tgz \
      "https://github.com/octeep/wireproxy/releases/download/${VER}/wireproxy_linux_${ARCH}.tar.gz"; \
    mkdir -p /app/bin; \
    tar xzf /tmp/wireproxy.tgz -C /app/bin; \
    chmod +x /app/bin/wireproxy; \
    rm -f /tmp/wireproxy.tgz

WORKDIR /app

COPY gateway /app/gateway
COPY pool /app/pool
COPY etc /app/etc

COPY deploy/fly/entrypoint.sh /app/entrypoint.sh
COPY deploy/fly/health-loop.sh /app/health-loop.sh

RUN chmod +x /app/entrypoint.sh /app/health-loop.sh /app/pool/*.sh

ENV WARP_POOL_ROOT=/opt/warp-pool \
    NODE_COUNT=5 \
    BASE_PORT=10800 \
    HEALTH_EVERY=300

CMD ["/app/entrypoint.sh"]
