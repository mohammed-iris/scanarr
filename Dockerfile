FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 CONFIG_DIR=/config SCANARR_PORT=8787
WORKDIR /opt/scanarr
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
# Run as the same user as qBittorrent so imported files have the right owner
ARG PUID=1000
ARG PGID=1000
RUN (getent group ${PGID} || groupadd -g ${PGID} scanarr) \
 && useradd -o -u ${PUID} -g ${PGID} -M scanarr \
 && mkdir -p /config && chown ${PUID}:${PGID} /config
USER scanarr
VOLUME ["/config"]
EXPOSE 8787
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('SCANARR_PORT','8787')+'/login')"
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${SCANARR_PORT}"]
