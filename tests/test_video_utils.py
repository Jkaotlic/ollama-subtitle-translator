"""Tests for video_utils module."""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock

import video_utils as vu


# --- check_ffmpeg_available ---

class TestCheckFfmpeg:
    def test_available(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")
        assert vu.check_ffmpeg_available() is True

    def test_not_available(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda cmd: None)
        assert vu.check_ffmpeg_available() is False

    def test_only_ffprobe(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/ffprobe" if cmd == "ffprobe" else None)
        assert vu.check_ffmpeg_available() is False


# --- probe_subtitle_tracks ---

MOCK_FFPROBE_OUTPUT = json.dumps({
    "streams": [
        {"index": 0, "codec_name": "h264", "codec_type": "video"},
        {"index": 1, "codec_name": "aac", "codec_type": "audio"},
        {"index": 2, "codec_name": "subrip", "codec_type": "subtitle",
         "tags": {"language": "eng", "title": "English"}},
        {"index": 3, "codec_name": "hdmv_pgs_subtitle", "codec_type": "subtitle",
         "tags": {"language": "rus", "title": "Russian PGS"}},
        {"index": 4, "codec_name": "ass", "codec_type": "subtitle",
         "tags": {"language": "jpn"}},
    ]
})


class TestProbeSubtitleTracks:
    def test_parses_tracks(self, tmp_path, monkeypatch):
        video = tmp_path / "test.mkv"
        video.touch()

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = MOCK_FFPROBE_OUTPUT

        monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: mock_result)

        tracks = vu.probe_subtitle_tracks(str(video))
        assert len(tracks) == 3

        # First track: English subrip (text)
        assert tracks[0].codec_name == "subrip"
        assert tracks[0].language == "eng"
        assert tracks[0].title == "English"
        assert tracks[0].is_text is True
        assert tracks[0].is_image is False
        assert tracks[0].sub_index == 0

        # Second track: Russian PGS (image)
        assert tracks[1].codec_name == "hdmv_pgs_subtitle"
        assert tracks[1].language == "rus"
        assert tracks[1].is_text is False
        assert tracks[1].is_image is True
        assert tracks[1].sub_index == 1

        # Third track: Japanese ASS (text)
        assert tracks[2].codec_name == "ass"
        assert tracks[2].language == "jpn"
        assert tracks[2].is_text is True
        assert tracks[2].sub_index == 2

    def test_no_subtitle_tracks(self, tmp_path, monkeypatch):
        video = tmp_path / "test.mp4"
        video.touch()

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"streams": [
            {"index": 0, "codec_name": "h264", "codec_type": "video"},
        ]})

        monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: mock_result)

        tracks = vu.probe_subtitle_tracks(str(video))
        assert tracks == []

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            vu.probe_subtitle_tracks("/nonexistent/video.mkv")

    def test_unsupported_extension(self, tmp_path):
        txt = tmp_path / "file.txt"
        txt.touch()
        with pytest.raises(ValueError, match="Unsupported"):
            vu.probe_subtitle_tracks(str(txt))

    def test_ffmpeg_not_installed(self, tmp_path, monkeypatch):
        video = tmp_path / "test.mkv"
        video.touch()
        monkeypatch.setattr("shutil.which", lambda cmd: None)
        with pytest.raises(RuntimeError, match="ffprobe not found"):
            vu.probe_subtitle_tracks(str(video))

    def test_ffprobe_failure(self, tmp_path, monkeypatch):
        video = tmp_path / "test.mkv"
        video.touch()

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "Invalid data found"

        monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: mock_result)

        with pytest.raises(RuntimeError, match="ffprobe failed"):
            vu.probe_subtitle_tracks(str(video))


# --- extract_subtitle_track ---

class TestExtractSubtitleTrack:
    def test_success(self, tmp_path, monkeypatch):
        output = tmp_path / "out.srt"

        def mock_run(*args, **kwargs):
            output.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n\n")
            result = MagicMock()
            result.returncode = 0
            return result

        monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")
        monkeypatch.setattr("subprocess.run", mock_run)

        result = vu.extract_subtitle_track("/fake/video.mkv", 0, str(output))
        assert result.exists()
        assert result.stat().st_size > 0

    def test_ffmpeg_failure(self, tmp_path, monkeypatch):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "Stream not found"

        monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: mock_result)

        with pytest.raises(RuntimeError, match="ffmpeg extraction failed"):
            vu.extract_subtitle_track("/fake/video.mkv", 0, str(tmp_path / "out.srt"))

    def test_ffmpeg_not_installed(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda cmd: None)
        with pytest.raises(RuntimeError, match="ffmpeg not found"):
            vu.extract_subtitle_track("/fake/video.mkv", 0, "/tmp/out.srt")

    def test_empty_output(self, tmp_path, monkeypatch):
        output = tmp_path / "out.srt"

        def mock_run(*args, **kwargs):
            # ffmpeg succeeds but produces empty file
            output.touch()
            result = MagicMock()
            result.returncode = 0
            return result

        monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")
        monkeypatch.setattr("subprocess.run", mock_run)

        with pytest.raises(RuntimeError, match="empty output"):
            vu.extract_subtitle_track("/fake/video.mkv", 0, str(output))


# --- format_track_label ---

class TestFormatTrackLabel:
    def test_full_label(self):
        track = vu.SubtitleTrack(
            index=2, sub_index=0, codec_name="subrip",
            language="eng", title="English", is_text=True, is_image=False,
        )
        label = vu.format_track_label(track)
        assert "ENG" in label
        assert "English" in label
        assert "subrip" in label

    def test_image_warning(self):
        track = vu.SubtitleTrack(
            index=3, sub_index=1, codec_name="hdmv_pgs_subtitle",
            language="rus", title="", is_text=False, is_image=True,
        )
        label = vu.format_track_label(track)
        assert "cannot extract" in label

    def test_no_language(self):
        track = vu.SubtitleTrack(
            index=2, sub_index=0, codec_name="subrip",
            language="", title="", is_text=True, is_image=False,
        )
        label = vu.format_track_label(track)
        assert "subrip" in label


# --- resolve_video_path ---

class TestResolveVideoPath:
    def test_no_video_dir(self, monkeypatch):
        monkeypatch.setattr(vu, "VIDEO_DIR", "")
        assert vu.resolve_video_path("/home/user/movie.mkv") == "/home/user/movie.mkv"

    def test_with_video_dir(self, monkeypatch):
        monkeypatch.setattr(vu, "VIDEO_DIR", "/videos")
        result = vu.resolve_video_path("/home/user/movie.mkv")
        assert Path(result) == Path("/videos/home/user/movie.mkv")
