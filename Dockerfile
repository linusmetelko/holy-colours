FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOLY_COLOURS_HOST=0.0.0.0 \
    HOLY_COLOURS_PORT=8000 \
    HOLY_COLOURS_PRESETS_PATH=/data/presets.json

RUN sed -i 's/Components: main/Components: main contrib non-free/g' /etc/apt/sources.list.d/debian.sources \
    && echo "ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true" | debconf-set-selections \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        cabextract \
        fontconfig \
        fonts-crosextra-caladea \
        fonts-crosextra-carlito \
        fonts-dejavu \
        fonts-liberation2 \
        fonts-noto-core \
        libreoffice-writer \
        ttf-mscorefonts-installer \
    && fc-cache -f \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser
RUN mkdir /data && chown appuser:appuser /data

COPY --chown=appuser:appuser web_app.py highlight_names.py colors.example.json ./
COPY --chown=appuser:appuser static ./static

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).read()"

CMD ["python", "web_app.py"]
