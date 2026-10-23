# syntax=docker/dockerfile:1
# Multi-arch: linux/arm64 (Pi 3/4/5 on 64-bit OS, Oracle Ampere), linux/arm/v7 (32-bit Pi OS), linux/amd64.
#   docker buildx build --platform linux/arm64,linux/arm/v7 -t you/tg-polyglot --push .

# Node 22: yt-dlp needs >= 22 as its JavaScript runtime for YouTube (Debian's apt nodejs is 20), and
# node:22-bookworm-slim is the last Node image with 32-bit ARM builds.
FROM node:22-bookworm-slim AS node

# The worker's npm packages are plain JavaScript: install them once on the build machine, for every platform
FROM --platform=$BUILDPLATFORM node:22-bookworm-slim AS vot
WORKDIR /vot
COPY vot/package.json vot/package-lock.json ./
RUN npm ci --omit=dev --omit=peer --no-audit --no-fund

# A minimal ffmpeg: stream copy + AAC/MP3 audio only. Debian's ffmpeg package is ~460 MB of libraries
# (LLVM and Mesa for GPU filters, a dozen video encoders) that this bot never uses; this one is ~5 MB.
# Compiling takes ~1 min on a PC, roughly 15-30 min on a Pi 3 (parallel jobs are capped by RAM: ~400 MB each).
FROM python:3.13-slim-trixie AS ffmpeg
ARG FFMPEG_VERSION=8.1.3
ARG FFMPEG_SHA256=7138d28c96d9d3e3af4ee3d8cad72741f8ffb40da90c1112235dea3ecd3178a3
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential nasm libmp3lame-dev curl ca-certificates xz-utils \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN curl -fsSL -o ffmpeg.tar.xz "https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz" \
    && echo "${FFMPEG_SHA256}  ffmpeg.tar.xz" | sha256sum -c - \
    && tar xf ffmpeg.tar.xz --strip-components=1 && rm ffmpeg.tar.xz
RUN ./configure --prefix=/opt/ffmpeg --disable-everything --disable-autodetect --disable-network \
        --disable-doc --disable-debug --disable-ffplay --disable-ffprobe --disable-avdevice --disable-swscale \
        --enable-libmp3lame \
        --enable-protocol=file,pipe \
        --enable-demuxer=mov,matroska,mpegts,flv,avi,mp3,aac,ogg,wav,concat \
        --enable-muxer=mp4,ipod,mov,mp3,null \
        --enable-decoder=aac,aac_latm,mp3,mp3float,opus,vorbis,flac,alac,ac3,eac3,pcm_s16le,pcm_f32le,vp9 \
        --enable-encoder=aac,libmp3lame \
        --enable-parser=aac,aac_latm,ac3,mpegaudio,opus,vorbis,flac,h264,hevc,vp8,vp9,av1 \
        --enable-bsf=aac_adtstoasc,extract_extradata,vp9_superframe,av1_frame_merge,null \
        --enable-filter=abuffer,abuffersink,anull,anullsrc,aformat,aresample,volume,amix,atempo \
    && jobs=$(awk '/MemTotal/ {print int($2 / 400000)}' /proc/meminfo) \
    && jobs=$(( jobs < 1 ? 1 : (jobs > $(nproc) ? $(nproc) : jobs) )) \
    && make -j"$jobs" && make install && /opt/ffmpeg/bin/ffmpeg -hide_banner -version | head -1

FROM python:3.13-slim-trixie AS build
# Compilers stay in this stage. They're only used on 32-bit ARM, where brotli and pycryptodomex have no wheels.
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
COPY requirements.txt /tmp/
RUN /opt/venv/bin/pip install --no-cache-dir --prefer-binary -r /tmp/requirements.txt

FROM python:3.13-slim-trixie
LABEL org.opencontainers.image.title="tg-polyglot" \
      org.opencontainers.image.description="Telegram bot that translates videos (voice-over, audio, transcript)"
# util-linux provides choom (child processes get a high OOM score, so the kernel spares the bot itself)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates tini libstdc++6 libmp3lame0 util-linux \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=ffmpeg /opt/ffmpeg/bin/ffmpeg /usr/local/bin/ffmpeg
RUN useradd --create-home --uid 1000 bot && mkdir /data && chown bot /data
# The venv belongs to the bot user so that yt-dlp can update itself at runtime
COPY --from=build --chown=bot /opt/venv /opt/venv
WORKDIR /app
COPY --from=vot /vot/node_modules /app/vot/node_modules
COPY vot/package.json vot/vot_worker.mjs /app/vot/
COPY bin /app/bin
COPY polyglot /app/polyglot
# yt-dlp: Node with a capped heap as the JS runtime; its cache (player JS, solved challenges) lives in /data
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data \
    YTDLP_JS_RUNTIMES=node:/app/bin/node-lowmem XDG_CACHE_HOME=/data/cache
USER bot
VOLUME /data
# The bot touches /data/heartbeat every minute; stale = the event loop is stuck
HEALTHCHECK --interval=2m --timeout=10s --start-period=5m \
    CMD test $(( $(date +%s) - $(stat -c %Y /data/heartbeat) )) -lt 300 || exit 1
ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "polyglot"]
