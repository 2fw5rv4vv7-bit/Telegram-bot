import os
import subprocess
import sys

import yt_dlp

from ig_core import INCLUDE


def ytdlp(url, outdir, cookies):
    opts = {
        "outtmpl": os.path.join(outdir, "%(autonumber)s_%(id)s.%(ext)s"),
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "socket_timeout": 30,
    }
    if cookies:
        opts["cookiefile"] = cookies
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=True)


def gallery_post(url, outdir, cookies):
    cmd = [sys.executable, "-m", "gallery_dl", "-q", "--write-metadata", "-D", outdir]
    if cookies:
        cmd += ["--cookies", cookies]
    cmd.append(url)
    try:
        r = subprocess.run(cmd, timeout=600, check=False,
                           capture_output=True, text=True)
        return (r.stderr or "")[-300:]
    except subprocess.TimeoutExpired:
        return "timeout"


def gallery_profile(username, outdir, cookies, limit, mode):
    cmd = [
        sys.executable, "-m", "gallery_dl", "-q", "--write-metadata",
        "-D", outdir,
        "-o", f"include={INCLUDE[mode]}",
        "--range", f"1-{limit}",
        "--sleep", "2-4", "--sleep-request", "1-2",
    ]
    if cookies:
        cmd += ["--cookies", cookies]
    cmd.append(f"https://www.instagram.com/{username}/")
    try:
        r = subprocess.run(cmd, timeout=1500, check=False,
                           capture_output=True, text=True)
        return (r.stderr or "")[-300:]
    except subprocess.TimeoutExpired:
        return "timeout"
