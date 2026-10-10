#!/usr/bin/env node
// Thin wrapper over @vot.js/node (Yandex video translation) for the Python bot.
//
// Usage:
//   node vot_worker.mjs --url URL --lang en --reslang ru --mode audio|subs --out FILE [--audio-file PATH]
//
// --audio-file: when Yandex asks for the audio, upload this file instead of fetching it with yt-dlp again
// (the bot downloads the original audio anyway; the path appears once that download is complete).
//
// Prints one JSON object per line to stdout:
//   {"type":"video","id":"..."}
//   {"type":"waiting","seconds":120}
//   {"type":"audio_upload","chunks":3}
//   {"type":"done","path":"/abs/out.mp3","url":"https://..."}
//   {"type":"error","message":"..."}            (exit code 1)
//
// Env: YTDLP_BIN (default "yt-dlp"), YTDLP_ARGS (JSON array of extra yt-dlp args: proxy, cookies...),
//      YANDEX_OAUTH_TOKEN (optional, lively voices en->ru),
//      VOT_PROXY (optional proxy URL), VOT_POLL_MAX (seconds, default 30)
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { createReadStream, createWriteStream } from "node:fs";
import fs from "node:fs/promises";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import { setTimeout as delay } from "node:timers/promises";
import { parseArgs } from "node:util";

import VOTClient from "@vot.js/node";
import { getVideoData } from "@vot.js/node/utils/videoData";
import { VOTAgent, VOTProxyAgent } from "@vot.js/node/utils/fetchAgent";
import VOTConfig from "@vot.js/shared/config";
import { LoggerLevel } from "@vot.js/shared/types/logger";
import { convertSubs } from "@vot.js/shared/utils/subs";
import { AudioDownloadType, VideoTranslationStatus } from "@vot.js/core/types/providers/yandex";

VOTConfig.loggerLevel = LoggerLevel.SILENCE;

const emit = (obj) => process.stdout.write(JSON.stringify(obj) + "\n");

const { values: args } = parseArgs({
  options: {
    url: { type: "string" },
    lang: { type: "string", default: "auto" },
    reslang: { type: "string", default: "ru" },
    mode: { type: "string", default: "audio" },
    out: { type: "string" },
    "subs-format": { type: "string", default: "srt" },
    "audio-file": { type: "string" },
    "audio-wait": { type: "string", default: "900" },
  },
});

const YTDLP_BIN = process.env.YTDLP_BIN || "yt-dlp";
const YTDLP_ARGS = JSON.parse(process.env.YTDLP_ARGS || "[]");
const POLL_MAX = Number(process.env.VOT_POLL_MAX || 30);
const apiToken = process.env.YANDEX_OAUTH_TOKEN || undefined;
const fetchOpts = {
  dispatcher: process.env.VOT_PROXY ? new VOTProxyAgent(process.env.VOT_PROXY) : new VOTAgent(),
};
const client = new VOTClient({ apiToken, fetchOpts });

// Yandex sometimes asks the client to supply the audio itself (AUDIO_REQUESTED),
// e.g. for YouTube. Stream it from yt-dlp and upload in chunks, like the browser extension.
async function* ytDlpAudio(url, lang) {
  const format = lang && lang !== "auto" ? `ba[language^=${lang}]/ba` : "ba";
  const child = spawn(
    YTDLP_BIN,
    [...YTDLP_ARGS, "-f", format, "-S", "lang,+size,+br", "-o", "-", "--no-playlist", "--no-part", "--quiet", "--no-warnings", "--", url],
    { stdio: ["ignore", "pipe", "pipe"] },
  );
  let stderr = "";
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (d) => (stderr = (stderr + d).slice(-2000)));
  const exit = new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("close", resolve);
  });
  exit.catch(() => {});
  try {
    yield* child.stdout;
    const code = await exit;
    if (code !== 0) throw new Error(`yt-dlp exited with ${code}: ${stderr.trim().split("\n").pop()}`);
  } finally {
    if (child.exitCode === null) child.kill();
  }
}

// The bot's downloaded file: its size is known, so chunks are read exactly (no chunk held back in memory)
async function* fileChunks(path, size) {
  const deadline = Date.now() + Number(args["audio-wait"]) * 1000;
  while (!(await fs.stat(path).catch(() => null))) {
    if (Date.now() > deadline) throw new Error("the original audio was not downloaded in time");
    await delay(1000);
  }
  const fh = await fs.open(path);
  try {
    const { size: total } = await fh.stat();
    if (!total) throw new Error("Audio is empty");
    const count = Math.ceil(total / size);
    for (let i = 0; i < count; i++) {
      const audioFile = Buffer.allocUnsafe(Math.min(size, total - i * size));
      await fh.read(audioFile, 0, audioFile.length, i * size);
      yield { audioFile, isLast: i === count - 1 };
    }
  } finally {
    await fh.close();
  }
}

// Holds one finished chunk back so the last one can be flagged. Buffer.concat makes the only copy.
async function* chunked(source, size) {
  let parts = [];
  let len = 0;
  let ready;
  for await (const piece of source) {
    parts.push(piece);
    len += piece.length;
    if (len < size) continue;
    if (ready) yield { audioFile: ready, isLast: false };
    ready = Buffer.concat(parts, len);
    parts = [];
    len = 0;
  }
  if (len > 0) {
    if (ready) yield { audioFile: ready, isLast: false };
    ready = Buffer.concat(parts, len);
  }
  if (!ready) throw new Error("Audio is empty");
  yield { audioFile: ready, isLast: true };
}

async function uploadAudio(videoData, translationId) {
  const fileId = `random-${AudioDownloadType.WEB_ABR}-${randomUUID()}`;
  let chunkId = 0;
  const chunks = args["audio-file"]
    ? fileChunks(args["audio-file"], VOTConfig.minChunkSize)
    : chunked(ytDlpAudio(videoData.url, args.lang), VOTConfig.minChunkSize);
  for await (const { audioFile, isLast } of chunks) {
    for (let attempt = 1; ; attempt++) {
      try {
        await client.provider.requestVtransAudio(
          videoData.url,
          translationId,
          { audioFile, chunkId },
          { audioPartsLength: isLast ? chunkId + 1 : 0, fileId, version: 1 },
          undefined,
          fetchOpts,
        );
        break;
      } catch (err) {
        if (attempt >= 3) throw err;
        await delay(1500);
      }
    }
    chunkId++;
    emit({ type: "audio_upload", chunks: chunkId });
    globalThis.gc?.(); // drop the request copies of this chunk now, not when V8 feels like it (--expose-gc)
  }
}

async function translate(videoData) {
  const useLivelyVoice = Boolean(apiToken) && args.lang !== "auto" && args.reslang === "ru";
  let audioProvided = false;
  for (;;) {
    const res = await client.translateVideo({
      videoData,
      requestLang: args.lang,
      responseLang: args.reslang,
      shouldSendFailedAudio: false,
      extraOpts: { useLivelyVoice },
    });
    if (res.translated && res.remainingTime < 1) return res.url;
    if (res.status === VideoTranslationStatus.AUDIO_REQUESTED && !audioProvided) {
      audioProvided = true;
      await uploadAudio(videoData, res.translationId);
      continue;
    }
    const seconds = res.remainingTime > 0 ? res.remainingTime : null;
    emit({ type: "waiting", seconds, message: res.message ?? null });
    await delay(1000 * Math.min(POLL_MAX, Math.max(10, seconds ?? 20)));
  }
}

async function subtitles(videoData) {
  const res = await client.getSubtitles({ videoData, requestLang: args.lang });
  const sub = res.subtitles.find((s) => s.translatedLanguage === args.reslang)
    ?? res.subtitles.find((s) => s.language === args.reslang);
  if (!sub) throw new Error("Yandex has no subtitles for this video in the requested language");
  return sub.translatedLanguage === args.reslang ? sub.translatedUrl : sub.url;
}

async function download(url, out, isSubs) {
  const res = await fetch(url, { headers: { "User-Agent": VOTConfig.userAgent }, ...fetchOpts });
  if (!res.ok || !res.body) throw new Error(`Download failed: HTTP ${res.status}`);
  if (isSubs) {
    const data = await res.json();
    const text = args["subs-format"] === "json" ? JSON.stringify(data) : convertSubs(data, args["subs-format"]);
    await fs.writeFile(out, text);
  } else {
    await pipeline(Readable.fromWeb(res.body), createWriteStream(out));
  }
}

try {
  if (!args.url || !args.out) throw new Error("--url and --out are required");
  const videoData = await getVideoData(args.url);
  emit({ type: "video", id: videoData.videoId });
  const isSubs = args.mode === "subs";
  const url = isSubs ? await subtitles(videoData) : await translate(videoData);
  await download(url, args.out, isSubs);
  emit({ type: "done", path: args.out, url });
} catch (err) {
  emit({ type: "error", message: err?.message ?? String(err) });
  process.exitCode = 1;
}
