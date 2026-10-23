# tg-polyglot

A Telegram bot that translates videos from YouTube, VK, Rutube, Vimeo, Twitch, direct `.mp4` links and
[hundreds of other sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md).
Send a link, then pick one of three outputs:

- 🎬 **Video**: the translated voice over the ducked original soundtrack.
- 🎧 **Audio** only, podcast-style.
- 📝 **Text**: a translated transcript (`.txt` + `.srt`).

It is built for a **Raspberry Pi 3B with 1 GB RAM**, running in Docker, and is tested on one:

- Translation and speech run on remote services, and video is never re-encoded.
- One job runs at a time. Jobs survive restarts and power cuts.
- Each result is stored as a Telegram file id, so asking for the same video again is answered instantly and costs nothing.

## Engines

Voice engines are tried in order (`VOICE_ENGINES`). If one fails, the next one is used:

| Engine | What it does | Languages | Cost on the Pi |
|---|---|---|---|
| `ytdub` | YouTube's own AI-dubbed audio track, when the video has one | whatever YouTube dubbed | download + remux |
| `yandex` | Yandex voice-over (Yandex Browser's API) via a small Node worker on [`@vot.js/node`](https://github.com/FOSWLY/vot.js) | from en, zh, ko, ar, fr, it, es, de, ja, ru, auto; into **ru, en, kk** | waiting; uploads the audio the bot already downloaded when Yandex asks for it |
| `tts` | captions → machine translation → speech, placed on the original timeline | every target in `/settings` | small: speech clips are joined by stream copy |

Building blocks of the `tts` engine:

- **Captions**: YouTube subtitles or its original auto-captions (`xx-orig`).
  Otherwise Groq Whisper speech recognition (free key, `GROQ_API_KEY`).
- **Translation** (`MT_ENGINES`): any OpenAI-compatible LLM (free Groq/Gemini keys), DeepL Free, plus the
  keyless Yandex and Google endpoints as the last resort. An engine that keeps failing is skipped for the
  rest of the job.
- **Speech** (`TTS_ENGINES`): Microsoft Edge neural voices (`edge-tts`), falling back to Google TTS
  (`gTTS`). One engine is used per job, so the voice doesn't change halfway through.

Text mode uses Yandex's subtitles if they exist, and captions → translation otherwise.

## Run

```bash
cp .env.example .env      # set TELEGRAM_TOKEN, ADMIN_IDS, and ALLOWED_USERS (it runs on your home network!)
./run.sh                  # docker compose up -d --build; everything persistent lives in ./data
docker compose logs -f    # watch it
```

Test one translation without Telegram:

```bash
docker compose run --rm bot python -m polyglot.cli https://youtu.be/... --mode video --dst ru --out /data/out
```

## Raspberry Pi 3B setup

1. **Use 64-bit Raspberry Pi OS (Lite).** Docker no longer ships packages for 32-bit Pi OS. The image
   still supports `linux/arm/v7` if you're stuck on 32-bit.
2. **Add a little swap** as a safety net: `sudo apt install zram-tools`, or a 512 MB swap file.
   `memswap_limit` in `docker-compose.yml` lets the container use it before anything gets OOM-killed.
3. **Install Docker**: `curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker $USER`.
4. `./run.sh`. The first build compiles a small ffmpeg (about 15–30 min on a Pi 3, by estimate). Later
   rebuilds reuse it. To skip building on the Pi, cross-build on a PC and pull the image on the Pi:
   ```bash
   docker buildx build --platform linux/arm64 -t you/tg-polyglot --push .
   ```

### Measured footprint

Measured with the real image in a container limited to 700 MB RAM and 4 CPUs (like a Pi 3B). These are
amd64 numbers; arm64 is similar and 32-bit ARM is a bit smaller.

| Stage | Process | Peak RSS |
|---|---|---|
| idle | bot (Python + aiogram) | ~45 MB |
| reading video info | yt-dlp | ~65–85 MB |
| ↳ sometimes: YouTube's JS challenge | Node solver, heap-capped by `bin/node-lowmem` | ~215 MB (260 uncapped) |
| download (all formats, one process) | yt-dlp | ~65 MB |
| waiting for / uploading to Yandex | Node worker | ~80–130 MB |
| audio mix + mux (video copied) | ffmpeg | ~20 MB |
| **whole job, worst moment** | | **~200–250 MB** (~350 MB if the JS challenge runs) |

The image is ~490 MB. Debian's `ffmpeg` package alone would add ~460 MB of GPU and video-encoder
libraries; the custom build is a 6 MB binary.

### Where the CPU goes

- **Video is never re-encoded.** The picture is chosen as H.264 ≤720p that fits Telegram's 50 MB limit,
  before downloading, and stream-copied. On long videos the audio bitrate drops a little if that buys a
  sharper picture (e.g. 480p instead of 360p).
- **The only real encoding is audio**: the translated voice mixed over the original sound. With
  `AUDIO_QUALITY=speech` (default: mono 24 kHz AAC) it's ~3.4x cheaper than stereo 44.1 kHz. A 34-minute
  video took 9 s on one desktop core, which means roughly 1.5–3 min on a Pi 3 (estimated).
  `MIX_ORIGINAL_VOLUME=0` skips the mix entirely.
- Subprocesses run with `nice 10`, and the bot itself stays responsive.

## Reliability

- **Restarts and power cuts:** jobs are stored in SQLite. After a restart, interrupted and waiting jobs go
  back in the queue and their users are told. A job interrupted twice is dropped, in case it is what
  crashes the bot.
- **Out of memory:** child processes (ffmpeg, yt-dlp, node) get a high OOM score, so the kernel kills them
  rather than the bot. The job then fails with a clear message.
- **Fallbacks everywhere:** voice engines, translation engines and TTS engines all fall back to the next
  one. A YouTube download rejected with 401/403 is retried with fresh metadata.
- **Telegram:** uploads are retried on network errors and flood limits. Status edits are throttled. At
  startup the bot waits for the network instead of crash-looping.
- **Housekeeping:**
  - Temp files are removed after every job and on start.
  - A job won't start below `MIN_FREE_DISK_MB`.
  - Logs are size-capped.
  - yt-dlp updates itself daily, only while no job is running.
  - Docker `HEALTHCHECK` watches a heartbeat file.

## Network notes

- A home connection has a residential IP, which YouTube trusts far more than cloud IPs. If YouTube still
  asks to "confirm you're not a bot":
  - Export cookies from a throwaway account to `data/cookies.txt`.
  - Set `YTDLP_COOKIES=/data/cookies.txt`.
- If YouTube or Telegram is blocked where the Pi is, set `YTDLP_PROXY`, `VOT_PROXY` and `TELEGRAM_PROXY`
  (e.g. `socks5://…`).
- Bigger files: enable the local Bot API server in `docker-compose.yml` to raise the 50 MB upload limit
  to 2 GB.

## Commands

| Command | What it does |
|---|---|
| `/start`, `/help` | How to use the bot |
| `/settings` (`/lang`) | Source and target languages |
| `/cancel` | Cancel your current job |
| `/stats` | Your usage; admins also see global stats and the queue |
| `/grant <id>`, `/revoke <id>` | Admins only: premium (no daily limit) |

All settings are in [`.env.example`](.env.example).

## Development

Needs Python 3.11+, Node 22+ and ffmpeg.

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
(cd vot && npm ci --omit=dev --omit=peer)
.venv/bin/python -m pytest tests                      # unit + resilience tests (offline)
RUN_NETWORK_TESTS=1 .venv/bin/python -m pytest tests  # + end-to-end bot flow (real YouTube/Yandex, fake Telegram)
```

Code map:

- `polyglot/pipeline.py`: one job, engines and fallbacks.
- `polyglot/media.py`: yt-dlp, ffmpeg, format choice.
- `polyglot/bot.py`: Telegram UI, the queue runner, delivery.
- `vot/vot_worker.mjs`: the Yandex client.
