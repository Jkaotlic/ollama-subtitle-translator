#!/usr/bin/env python3
"""
FFmpeg/ffprobe wrapper for extracting embedded subtitles from video files.
"""

import subprocess
import json
import shutil
import os
import sys
import platform
import logging
import zipfile
import tarfile
import urllib.request
from pathlib import Path
from typing import List
from dataclasses import dataclass

logger = logging.getLogger("video-utils")

# Directory for auto-downloaded ffmpeg binaries
FFMPEG_BIN_DIR = Path(__file__).parent / "ffmpeg_bin"

# Codecs that can be extracted as text-based SRT
TEXT_CODECS = {"subrip", "ass", "ssa", "mov_text", "webvtt", "text"}

# Image-based codecs that cannot be extracted as text SRT
IMAGE_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "pgssub"}

SUPPORTED_VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".webm", ".ts", ".m2ts"}

# Docker volume mount path (set via VIDEO_DIR env when running in container)
VIDEO_DIR = os.environ.get("VIDEO_DIR", "")


@dataclass
class SubtitleTrack:
    """One subtitle stream found in a video file."""
    index: int           # absolute stream index in the container
    sub_index: int       # relative subtitle stream index (0, 1, 2...)
    codec_name: str      # e.g. "subrip", "ass", "hdmv_pgs_subtitle"
    language: str        # ISO 639 tag, e.g. "eng", "rus", or ""
    title: str           # stream title metadata, or ""
    is_text: bool        # True if codec is text-extractable
    is_image: bool       # True if codec is image-based


def _download_ffmpeg() -> bool:
    """Download static ffmpeg+ffprobe binaries for the current platform."""
    system = platform.system()
    if system == "Windows":
        url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
    elif system == "Linux":
        url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz"
    elif system == "Darwin":
        url = "https://evermeet.cx/ffmpeg/getrelease/zip"
    else:
        logger.warning("Unsupported platform for auto-download: %s", system)
        return False

    FFMPEG_BIN_DIR.mkdir(exist_ok=True)
    archive_path = FFMPEG_BIN_DIR / ("ffmpeg_download.zip" if system != "Linux" else "ffmpeg_download.tar.xz")

    try:
        logger.info("Downloading ffmpeg from %s ...", url)
        urllib.request.urlretrieve(url, str(archive_path))

        if system == "Windows":
            with zipfile.ZipFile(archive_path, "r") as zf:
                for member in zf.namelist():
                    basename = Path(member).name.lower()
                    if basename in ("ffmpeg.exe", "ffprobe.exe"):
                        target = FFMPEG_BIN_DIR / basename
                        with zf.open(member) as src, open(target, "wb") as dst:
                            dst.write(src.read())
        elif system == "Linux":
            with tarfile.open(archive_path, "r:xz") as tf:
                for member in tf.getmembers():
                    basename = Path(member.name).name.lower()
                    if basename in ("ffmpeg", "ffprobe"):
                        member.name = basename
                        tf.extract(member, FFMPEG_BIN_DIR)
                        (FFMPEG_BIN_DIR / basename).chmod(0o755)
        elif system == "Darwin":
            # evermeet provides single-binary zips; need separate downloads
            with zipfile.ZipFile(archive_path, "r") as zf:
                zf.extractall(FFMPEG_BIN_DIR)
            # Also download ffprobe
            probe_url = "https://evermeet.cx/ffmpeg/getrelease/ffprobe/zip"
            probe_archive = FFMPEG_BIN_DIR / "ffprobe_download.zip"
            urllib.request.urlretrieve(probe_url, str(probe_archive))
            with zipfile.ZipFile(probe_archive, "r") as zf:
                zf.extractall(FFMPEG_BIN_DIR)
            probe_archive.unlink(missing_ok=True)

        archive_path.unlink(missing_ok=True)

        # Add to PATH
        bin_str = str(FFMPEG_BIN_DIR)
        if bin_str not in os.environ.get("PATH", ""):
            os.environ["PATH"] = bin_str + os.pathsep + os.environ.get("PATH", "")

        logger.info("ffmpeg downloaded to %s", FFMPEG_BIN_DIR)
        return True
    except Exception as e:
        logger.error("Failed to download ffmpeg: %s", e)
        archive_path.unlink(missing_ok=True)
        return False


def ensure_ffmpeg() -> bool:
    """Ensure ffmpeg/ffprobe are available. Auto-downloads if missing."""
    if check_ffmpeg_available():
        return True
    logger.info("ffmpeg not found on PATH, attempting auto-download...")
    if _download_ffmpeg():
        return check_ffmpeg_available()
    return False


def check_ffmpeg_available() -> bool:
    """Check if ffmpeg and ffprobe are on PATH (includes ffmpeg_bin dir)."""
    # Always include local ffmpeg_bin in PATH check
    bin_str = str(FFMPEG_BIN_DIR)
    if FFMPEG_BIN_DIR.exists() and bin_str not in os.environ.get("PATH", ""):
        os.environ["PATH"] = bin_str + os.pathsep + os.environ.get("PATH", "")
    return shutil.which("ffprobe") is not None and shutil.which("ffmpeg") is not None


def resolve_video_path(user_path: str) -> str:
    """Resolve user-provided video path, accounting for Docker volume mounts."""
    if VIDEO_DIR:
        resolved = Path(VIDEO_DIR) / user_path.lstrip("/")
        return str(resolved)
    return user_path


def probe_subtitle_tracks(video_path: str) -> List[SubtitleTrack]:
    """Run ffprobe on video_path, return list of SubtitleTrack.

    Raises:
        FileNotFoundError: if video_path does not exist
        RuntimeError: if ffprobe is not installed or fails
        ValueError: if the file has unsupported extension
    """
    path = Path(video_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {video_path}")

    if path.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
        raise ValueError(f"Unsupported file extension: {path.suffix}")

    if not check_ffmpeg_available():
        raise RuntimeError(
            "ffprobe not found. Install ffmpeg: "
            "brew install ffmpeg (macOS), apt install ffmpeg (Linux), "
            "or download from https://ffmpeg.org/download.html"
        )

    cmd = [
        "ffprobe", "-v", "error",
        "-of", "json",
        "-show_entries", "stream=index,codec_name,codec_type:stream_tags=language,title",
        str(path),
    ]

    logger.info("probe cmd: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()}")

    data = json.loads(result.stdout)
    streams = data.get("streams", [])

    tracks: List[SubtitleTrack] = []
    sub_counter = 0

    for stream in streams:
        if stream.get("codec_type") != "subtitle":
            continue

        codec = stream.get("codec_name", "unknown")
        tags = stream.get("tags", {})
        language = tags.get("language", "")
        title = tags.get("title", "")

        tracks.append(SubtitleTrack(
            index=stream.get("index", 0),
            sub_index=sub_counter,
            codec_name=codec,
            language=language,
            title=title,
            is_text=codec in TEXT_CODECS,
            is_image=codec in IMAGE_CODECS,
        ))
        sub_counter += 1

    return tracks


def extract_subtitle_track(video_path: str, sub_index: int, output_path: str,
                           timeout: int = 120) -> Path:
    """Extract subtitle track at sub_index as SRT file.

    Args:
        video_path: path to the video file
        sub_index: relative subtitle stream index (0-based)
        output_path: where to write the extracted .srt
        timeout: subprocess timeout in seconds

    Returns:
        Path to the output SRT file

    Raises:
        RuntimeError: if ffmpeg fails or is not installed
    """
    if not check_ffmpeg_available():
        raise RuntimeError("ffmpeg not found")

    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-map", f"0:s:{sub_index}",
        "-c:s", "srt",
        str(output_path),
    ]

    logger.info("extract cmd: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg extraction failed: {result.stderr.strip()}")

    out = Path(output_path)
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError("ffmpeg produced empty output file")

    return out


def format_track_label(track: SubtitleTrack) -> str:
    """Human-readable label for a subtitle track."""
    parts = []
    if track.language:
        parts.append(track.language.upper())
    if track.title:
        parts.append(track.title)
    parts.append(f"[{track.codec_name}]")
    if track.is_image:
        parts.append("(image-based, cannot extract)")
    return " - ".join(parts) if parts else f"Track {track.sub_index}"
