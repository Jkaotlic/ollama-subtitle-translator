#!/usr/bin/env python3
"""
FFmpeg/ffprobe wrapper for extracting embedded subtitles from video files.
"""

import subprocess
import json
import shutil
import os
import logging
from pathlib import Path
from typing import List
from dataclasses import dataclass

logger = logging.getLogger("video-utils")

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


def check_ffmpeg_available() -> bool:
    """Check if ffmpeg and ffprobe are on PATH."""
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
