# similar-files for a NAS: the CLI with every extractor available.
#
#   docker compose -f docker/compose.yaml build
#
# The build context is the repo root (see compose.yaml).

FROM python:3.12-slim

# ffmpeg for video, fpcalc (Chromaprint) for audio.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libchromaprint-tools \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY similar_files ./similar_files
RUN pip install --no-cache-dir ".[image]" && rm -rf /src

COPY docker/entrypoint.sh /usr/local/lib/similar-files-nas.sh

# The cache lives in a mounted folder so it survives the container.
ENV SIMILAR_FILES_CACHE=/cache/ \
    PYTHONUNBUFFERED=1

WORKDIR /
ENTRYPOINT ["sh", "/usr/local/lib/similar-files-nas.sh"]
