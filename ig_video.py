import asyncio
import json
import os
import subprocess

from ig_core import MAX_UPLOAD, NULL, SEND_AS, SHRINK, is_video


def probe(path):
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_streams", "-show_format", path], timeout=60)
        data = json.loads(out)
    except Exception:
        return {}
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    return {
        "w": v.get("width"),
        "h": v.get("height"),
        "dur": int(float(data.get("format", {}).get("duration") or 0)),
        "audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def add_silent_audio(path):
    # Videos with no audio track are shown by Telegram as GIFs.
    out = path + ".a.mp4"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", path, "-f", "lavfi",
             "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
             "-c:v", "copy", "-c:a", "aac", "-shortest",
             "-movflags", "+faststart", out],
            stdout=NULL, stderr=NULL, timeout=300, check=True)
        if os.path.exists(out) and os.path.getsize(out) > 0:
            return out
    except Exception:
        pass
    return path


def shrink(path):
    try:
        dur = float(subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path], timeout=60).strip())
        vbr = int(46 * 1024 * 1024 * 8 / dur) - 96_000
        if vbr < 150_000:
            return None
        out = path + ".small.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-i", path, "-c:v", "libx264", "-preset", "veryfast",
             "-b:v", str(vbr), "-maxrate", str(int(vbr * 1.2)),
             "-bufsize", str(vbr * 2), "-c:a", "aac", "-b:a", "96k",
             "-movflags", "+faststart", out],
            stdout=NULL, stderr=NULL, timeout=900, check=True)
        if os.path.exists(out) and os.path.getsize(out) <= MAX_UPLOAD:
            return out
    except Exception:
        pass
    return None


async def prep(path):
    # Returns (ready_path or None, status)
    if is_video(path) and SEND_AS != "document":
        m = await asyncio.to_thread(probe, path)
        if m and not m.get("audio"):
            path = await asyncio.to_thread(add_silent_audio, path)
    if os.path.getsize(path) > MAX_UPLOAD:
        if SHRINK and is_video(path):
            small = await asyncio.to_thread(shrink, path)
            if small:
                return small, "shrunk"
        return None, "skipped"
    return path, "ok"
