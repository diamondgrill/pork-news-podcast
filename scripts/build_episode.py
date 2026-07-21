#!/usr/bin/env python3
"""台本テキストを音声化し、RSS フィードを更新する。

GitHub Actions 上での実行を想定:
  - 環境変数 GOOGLE_TTS_API_KEY : Google Cloud Text-to-Speech の API キー
  - 環境変数 GH_TOKEN / GITHUB_REPOSITORY : gh CLI でのリリース作成に使用

ローカルで RSS だけ再生成する場合:
  python3 scripts/build_episode.py --feed-only
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
JST = timezone(timedelta(hours=9))

# Google TTS synthesize は 1 リクエスト 5,000 バイト上限。
# 日本語 (UTF-8 で 3 バイト/字) の安全マージンとして 1,200 字で分割する。
MAX_CHUNK_CHARS = 1200


def log(msg):
    print(msg, flush=True)


def chunk_text(text, max_chars=MAX_CHUNK_CHARS):
    sentences = []
    for para in re.split(r"\n+", text):
        para = para.strip()
        if not para:
            continue
        parts = re.split(r"(?<=[。！？])", para)
        sentences.extend(p for p in parts if p.strip())
    chunks, cur = [], ""
    for s in sentences:
        if cur and len(cur) + len(s) > max_chars:
            chunks.append(cur)
            cur = s
        else:
            cur += s
    if cur:
        chunks.append(cur)
    return chunks


def synthesize(chunk, voice, api_key):
    body = json.dumps(
        {
            "input": {"text": chunk},
            "voice": {"languageCode": CONFIG["language"], "name": voice},
            "audioConfig": {"audioEncoding": "LINEAR16"},
        }
    ).encode()
    req = urllib.request.Request(
        "https://texttospeech.googleapis.com/v1/text:synthesize?key=" + api_key,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        payload = json.load(r)
    return base64.b64decode(payload["audioContent"])


def synthesize_with_retry(chunk, voice, api_key, attempts=3):
    for i in range(attempts):
        try:
            return synthesize(chunk, voice, api_key)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503) and i < attempts - 1:
                time.sleep(10 * (i + 1))
                continue
            raise


def build_audio(script_path, out_mp3, api_key):
    text = script_path.read_text(encoding="utf-8")
    chunks = chunk_text(text)
    if not chunks:
        raise SystemExit(f"台本が空: {script_path}")
    voices = CONFIG["voices"]
    voice = None
    with tempfile.TemporaryDirectory() as td:
        wavs = []
        for n, chunk in enumerate(chunks):
            if voice is None:
                last_err = None
                for cand in voices:
                    try:
                        data = synthesize_with_retry(chunk, cand, api_key)
                        voice = cand
                        break
                    except urllib.error.HTTPError as e:
                        last_err = e
                        log(f"voice {cand} 失敗: HTTP {e.code}")
                if voice is None:
                    raise SystemExit(f"全ての voice で合成に失敗: {last_err}")
            else:
                data = synthesize_with_retry(chunk, voice, api_key)
            p = Path(td) / f"c{n:03d}.wav"
            p.write_bytes(data)
            wavs.append(p)
        listfile = Path(td) / "list.txt"
        listfile.write_text("".join(f"file '{w}'\n" for w in wavs))
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", str(listfile), "-ar", "44100", "-ac", "1", "-b:a", "64k",
             str(out_mp3)],
            check=True,
        )
    dur = float(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(out_mp3)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    )
    log(f"voice={voice} chunks={len(chunks)} duration={dur:.0f}s")
    return dur


def publish_release(date, mp3_path):
    repo = os.environ.get("GITHUB_REPOSITORY", CONFIG["repo"])
    tag = f"ep-{date}"
    exists = (
        subprocess.run(
            ["gh", "release", "view", tag, "-R", repo],
            capture_output=True,
        ).returncode
        == 0
    )
    if exists:
        subprocess.run(
            ["gh", "release", "upload", tag, str(mp3_path), "-R", repo, "--clobber"],
            check=True,
        )
    else:
        subprocess.run(
            ["gh", "release", "create", tag, str(mp3_path), "-R", repo,
             "--title", f"{date} エピソード", "--notes", ""],
            check=True,
        )
    return f"https://github.com/{repo}/releases/download/{tag}/{mp3_path.name}"


def pending_episodes():
    eps_dir = ROOT / "episodes"
    if not eps_dir.exists():
        return []
    out = []
    for d in sorted(eps_dir.iterdir()):
        if not d.is_dir():
            continue
        if (d / "script.txt").exists() and (d / "meta.json").exists() \
                and not (d / "episode.json").exists():
            out.append(d)
    return out


def process_episode(ep_dir, api_key):
    meta = json.loads((ep_dir / "meta.json").read_text(encoding="utf-8"))
    date = meta["date"]
    log(f"--- エピソード生成: {date} ---")
    with tempfile.TemporaryDirectory() as td:
        mp3 = Path(td) / f"porkcast-{date}.mp3"
        dur = build_audio(ep_dir / "script.txt", mp3, api_key)
        size = mp3.stat().st_size
        url = publish_release(date, mp3)
    (ep_dir / "episode.json").write_text(
        json.dumps(
            {"audio_url": url, "bytes": size, "duration": round(dur)},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    log(f"公開: {url} ({size/1e6:.1f}MB)")


def build_feed():
    base = CONFIG["base_url"].rstrip("/")
    token = CONFIG["feed_token"]
    feed_dir = ROOT / "docs" / token
    feed_dir.mkdir(parents=True, exist_ok=True)

    eps = []
    eps_dir = ROOT / "episodes"
    if eps_dir.exists():
        for d in sorted(eps_dir.iterdir(), reverse=True):
            mj, ej = d / "meta.json", d / "episode.json"
            if mj.exists() and ej.exists():
                m = json.loads(mj.read_text(encoding="utf-8"))
                m.update(json.loads(ej.read_text(encoding="utf-8")))
                eps.append(m)
    eps = eps[: CONFIG.get("max_feed_items", 60)]

    img_url = None
    if (feed_dir / "cover.jpg").exists():
        img_url = f"{base}/{token}/cover.jpg"

    items = []
    for m in eps:
        dt = datetime.strptime(m["date"], "%Y-%m-%d").replace(hour=9, tzinfo=JST)
        pub = dt.strftime("%a, %d %b %Y %H:%M:%S %z")
        dur = int(m.get("duration", 0))
        durs = f"{dur // 3600}:{dur % 3600 // 60:02d}:{dur % 60:02d}"
        items.append(
            f"""    <item>
      <title>{escape(m["title"])}</title>
      <description>{escape(m.get("description", ""))}</description>
      <enclosure url="{escape(m["audio_url"])}" length="{m.get("bytes", 0)}" type="audio/mpeg"/>
      <guid isPermaLink="false">porkcast-{m["date"]}</guid>
      <pubDate>{pub}</pubDate>
      <itunes:duration>{durs}</itunes:duration>
    </item>"""
        )

    image_xml = ""
    if img_url:
        image_xml = f'\n    <itunes:image href="{escape(img_url)}"/>'
    body = "\n".join(items)
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>{escape(CONFIG["podcast_title"])}</title>
    <description>{escape(CONFIG["podcast_description"])}</description>
    <link>{escape(base)}</link>
    <language>ja</language>
    <itunes:author>{escape(CONFIG["author"])}</itunes:author>
    <itunes:block>Yes</itunes:block>{image_xml}
{body}
  </channel>
</rss>
"""
    (feed_dir / "feed.xml").write_text(xml, encoding="utf-8")
    log(f"feed: {len(eps)} items -> docs/{token}/feed.xml")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--feed-only", action="store_true",
                    help="音声合成をスキップして RSS だけ再生成する")
    args = ap.parse_args()

    if not args.feed_only:
        pend = pending_episodes()
        if pend:
            api_key = os.environ.get("GOOGLE_TTS_API_KEY")
            if not api_key:
                sys.exit("環境変数 GOOGLE_TTS_API_KEY が未設定")
            for ep_dir in pend:
                process_episode(ep_dir, api_key)
        else:
            log("未処理のエピソードなし")
    build_feed()


if __name__ == "__main__":
    main()
