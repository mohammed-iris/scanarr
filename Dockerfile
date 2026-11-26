# One image for everything: the Scanarr app, the setup wizard, the one-time init step.
#   docker run ... ghcr.io/mohammed-iris/scanarr            -> Scanarr (port 9797)
#   docker run ... ghcr.io/mohammed-iris/scanarr setup      -> setup wizard (port 9700)
ARG DOCKER_CLI_IMAGE=docker:29-cli
FROM ${DOCKER_CLI_IMAGE} AS dockercli

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 CONFIG_DIR=/config SCANARR_PORT=9797 HOME=/tmp
WORKDIR /opt/scanarr
# Docker CLI + compose plugin: only used by the setup wizard (and only if you give it the Docker socket)
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && groupadd -g 1000 scanarr && useradd -u 1000 -g 1000 -M -d /tmp scanarr \
 && mkdir -p /config && chown 1000:1000 /config
COPY app ./app
# Default user; the generated compose file sets your own user ID (PUID:PGID) so renamed files are yours.
USER 1000:1000
VOLUME ["/config"]
EXPOSE 9797 9700
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s CMD ["python", "-m", "app", "health"]
ENTRYPOINT ["python", "-m", "app"]
CMD ["serve"]
