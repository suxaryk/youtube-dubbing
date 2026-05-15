"""
Tests for pipeline.py — all external I/O is mocked.
Run with: pytest tests/ -v
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call
import pytest
from pydub import AudioSegment

import pipeline as p


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _silent_wav(tmp_path: Path, name: str = "audio.wav", duration_ms: int = 500) -> Path:
    path = tmp_path / name
    AudioSegment.silent(duration=duration_ms).export(str(path), format="wav")
    return path


def _make_chunk(start=0.0, end=1.0, text="Hello", ua_text="Привіт"):
    return {"start": start, "end": end, "text": text, "ua_text": ua_text}


# ---------------------------------------------------------------------------
# stretch_audio
# ---------------------------------------------------------------------------

class TestStretchAudio:
    def test_same_duration_no_stretch(self):
        audio = AudioSegment.silent(duration=1000)
        result = p.stretch_audio(audio, 1000)
        assert len(result) == pytest.approx(1000, abs=50)

    def test_within_tolerance_not_stretched(self):
        audio = AudioSegment.silent(duration=1000)
        result = p.stretch_audio(audio, 1030)  # 3% diff < 5% threshold
        assert result is audio

    def test_empty_audio_returns_silence(self):
        audio = AudioSegment.silent(duration=0)
        result = p.stretch_audio(audio, 1000)
        assert len(result) == pytest.approx(1000, abs=50)

    def test_zero_target_returns_empty(self):
        audio = AudioSegment.silent(duration=500)
        result = p.stretch_audio(audio, 0)
        assert len(result) == 0

    def test_negative_target_returns_empty(self):
        audio = AudioSegment.silent(duration=500)
        result = p.stretch_audio(audio, -100)
        assert len(result) == 0

    def test_ratio_clamped_at_min(self):
        # audio 2000ms, target 5000ms → ratio=0.4 → clamped to 0.5
        audio = AudioSegment.silent(duration=2000)
        result = p.stretch_audio(audio, 5000)
        assert len(result) > 0

    def test_ratio_clamped_at_max(self):
        # audio 5000ms, target 1000ms → ratio=5.0 → clamped to 2.0
        audio = AudioSegment.silent(duration=5000)
        result = p.stretch_audio(audio, 1000)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# _fmt_time
# ---------------------------------------------------------------------------

class TestFmtTime:
    def test_zero(self):
        assert p._fmt_time(0) == "00:00:00,000"

    def test_one_hour(self):
        assert p._fmt_time(3600) == "01:00:00,000"

    def test_fractional_seconds(self):
        assert p._fmt_time(1.5) == "00:00:01,500"

    def test_complex(self):
        # 1h 2m 3.456s
        t = 3600 + 2 * 60 + 3.456
        assert p._fmt_time(t) == "01:02:03,456"

    def test_59_minutes_59_seconds(self):
        t = 59 * 60 + 59.999
        result = p._fmt_time(t)
        assert result.startswith("00:59:59,")


# ---------------------------------------------------------------------------
# save_transcript
# ---------------------------------------------------------------------------

class TestSaveTranscript:
    def test_creates_both_files(self, tmp_path):
        chunks = [_make_chunk(0.0, 2.0, "Hello", "Привіт")]
        srt, txt = p.save_transcript(chunks, tmp_path)
        assert srt.exists()
        assert txt.exists()

    def test_srt_format(self, tmp_path):
        chunks = [_make_chunk(0.0, 2.0, "Hello", "Привіт")]
        srt, _ = p.save_transcript(chunks, tmp_path)
        content = srt.read_text(encoding="utf-8")
        assert "1\n" in content
        assert "00:00:00,000 --> 00:00:02,000" in content
        assert "Привіт" in content

    def test_srt_multiple_chunks(self, tmp_path):
        chunks = [
            _make_chunk(0.0, 1.0, "One", "Один"),
            _make_chunk(1.5, 3.0, "Two", "Два"),
        ]
        srt, _ = p.save_transcript(chunks, tmp_path)
        content = srt.read_text(encoding="utf-8")
        assert "1\n" in content
        assert "2\n" in content
        assert "Один" in content
        assert "Два" in content

    def test_txt_format(self, tmp_path):
        chunks = [_make_chunk(1.0, 2.0, "Hello", "Привіт")]
        _, txt = p.save_transcript(chunks, tmp_path)
        content = txt.read_text(encoding="utf-8")
        assert "[1.0s] EN: Hello" in content
        assert "[1.0s] UA: Привіт" in content

    def test_fallback_to_original_when_no_ua_text(self, tmp_path):
        chunk = {"start": 0.0, "end": 1.0, "text": "Hello"}  # no ua_text key
        srt, _ = p.save_transcript([chunk], tmp_path)
        assert "Hello" in srt.read_text(encoding="utf-8")

    def test_empty_chunks(self, tmp_path):
        srt, txt = p.save_transcript([], tmp_path)
        assert srt.read_text(encoding="utf-8") == ""
        assert txt.read_text(encoding="utf-8") == ""


# ---------------------------------------------------------------------------
# assemble_audio
# ---------------------------------------------------------------------------

class TestAssembleAudio:
    def test_output_file_created(self, tmp_path):
        wav = _silent_wav(tmp_path, "chunk.wav", 500)
        chunks = [_make_chunk(0.0, 0.5)]
        result = p.assemble_audio(chunks, [wav], tmp_path)
        assert result.exists()

    def test_raises_on_empty_chunks(self, tmp_path):
        with pytest.raises(ValueError, match="порожній"):
            p.assemble_audio([], [], tmp_path)

    def test_raises_on_mismatched_lengths(self, tmp_path):
        wav = _silent_wav(tmp_path, "c.wav", 500)
        with pytest.raises(ValueError, match="не збігається"):
            p.assemble_audio([_make_chunk()], [wav, wav], tmp_path)

    def test_output_duration_covers_last_chunk(self, tmp_path):
        wav = _silent_wav(tmp_path, "c.wav", 500)
        chunks = [_make_chunk(0.0, 2.0)]
        result = p.assemble_audio(chunks, [wav], tmp_path)
        audio = AudioSegment.from_wav(str(result))
        # total_ms = 2000 + 2000 buffer
        assert len(audio) >= 3000


# ---------------------------------------------------------------------------
# translate_chunks
# ---------------------------------------------------------------------------

class TestTranslateChunks:
    def test_translates_text(self):
        chunks = [_make_chunk(text="Hello")]
        with patch("pipeline.GoogleTranslator") as MockTranslator:
            MockTranslator.return_value.translate.return_value = "Привіт"
            result = p.translate_chunks(chunks)
        assert result[0]["ua_text"] == "Привіт"

    def test_fallback_on_error(self):
        chunks = [_make_chunk(text="Hello")]
        with patch("pipeline.GoogleTranslator") as MockTranslator:
            MockTranslator.return_value.translate.side_effect = Exception("API error")
            result = p.translate_chunks(chunks)
        assert result[0]["ua_text"] == "Hello"

    def test_empty_chunks_passthrough(self):
        assert p.translate_chunks([]) == []

    def test_none_translation_falls_back(self):
        chunks = [_make_chunk(text="Hello")]
        with patch("pipeline.GoogleTranslator") as MockTranslator:
            MockTranslator.return_value.translate.return_value = None
            result = p.translate_chunks(chunks)
        assert result[0]["ua_text"] == "Hello"


# ---------------------------------------------------------------------------
# transcribe
# ---------------------------------------------------------------------------

class TestTranscribe:
    def test_returns_chunks(self, tmp_path):
        audio = _silent_wav(tmp_path)
        seg = MagicMock()
        seg.start, seg.end, seg.text = 0.0, 1.0, " Hello "
        mock_model = MagicMock()
        mock_model.transcribe.return_value = ([seg], MagicMock())
        result = p.transcribe(audio, mock_model)
        assert result == [{"start": 0.0, "end": 1.0, "text": "Hello"}]

    def test_filters_empty_segments(self, tmp_path):
        audio = _silent_wav(tmp_path)
        seg = MagicMock()
        seg.start, seg.end, seg.text = 0.0, 1.0, "   "
        mock_model = MagicMock()
        mock_model.transcribe.return_value = ([seg], MagicMock())
        result = p.transcribe(audio, mock_model)
        assert result == []

    def test_raises_if_file_missing(self, tmp_path):
        mock_model = MagicMock()
        with pytest.raises(FileNotFoundError):
            p.transcribe(tmp_path / "nonexistent.wav", mock_model)


# ---------------------------------------------------------------------------
# download_video
# ---------------------------------------------------------------------------

class TestDownloadVideo:
    def test_calls_yt_dlp_and_ffmpeg(self, tmp_path):
        fake_video = tmp_path / "input_video.mp4"
        fake_video.touch()  # simulate yt-dlp creating the file
        with patch("pipeline.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            p.download_video("https://youtube.com/watch?v=test", tmp_path)
        assert mock_run.call_count == 2
        first_cmd = mock_run.call_args_list[0][0][0]
        assert "yt-dlp" in first_cmd
        second_cmd = mock_run.call_args_list[1][0][0]
        assert "ffmpeg" in second_cmd

    def test_raises_on_empty_url(self, tmp_path):
        with pytest.raises(ValueError, match="порожнім"):
            p.download_video("", tmp_path)

    def test_raises_on_whitespace_url(self, tmp_path):
        with pytest.raises(ValueError, match="порожнім"):
            p.download_video("   ", tmp_path)


# ---------------------------------------------------------------------------
# synthesize_speech
# ---------------------------------------------------------------------------

class TestSynthesizeSpeech:
    def test_returns_one_file_per_chunk(self, tmp_path):
        chunks = [_make_chunk(), _make_chunk(1.0, 2.0, "World", "Світ")]
        chunks_dir = tmp_path / "chunks"
        chunks_dir.mkdir()
        for i in range(len(chunks)):
            _silent_wav(chunks_dir, f"{i:04d}.wav")

        # asyncio.run(_synthesize_all(...)) returns [None, None] — no errors
        with patch("pipeline.asyncio.run", return_value=[None, None]):
            result = p.synthesize_speech(chunks, tmp_path)
        assert len(result) == len(chunks)

    def test_empty_chunks_returns_empty(self, tmp_path):
        result = p.synthesize_speech([], tmp_path)
        assert result == []

    def test_silence_inserted_on_error(self, tmp_path):
        chunks = [_make_chunk(0.0, 1.0)]
        # asyncio.run returns list with one Exception — chunk synthesis failed
        with patch("pipeline.asyncio.run", return_value=[Exception("TTS error")]):
            result = p.synthesize_speech(chunks, tmp_path)
        assert len(result) == 1
        assert result[0].exists()
        audio = AudioSegment.from_wav(str(result[0]))
        assert len(audio) == pytest.approx(1000, abs=50)


# ---------------------------------------------------------------------------
# merge_video_audio
# ---------------------------------------------------------------------------

class TestMergeVideoAudio:
    def test_calls_ffmpeg(self, tmp_path):
        video = tmp_path / "v.mp4"
        audio = tmp_path / "a.wav"
        video.touch()
        audio.touch()
        with patch("pipeline.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = p.merge_video_audio(video, audio, tmp_path)
        cmd = mock_run.call_args[0][0]
        assert "ffmpeg" in cmd
        assert str(video) in cmd
        assert str(audio) in cmd
        assert result == tmp_path / "output_ukrainian.mp4"


# ---------------------------------------------------------------------------
# run_pipeline (orchestrator)
# ---------------------------------------------------------------------------

class TestRunPipeline:
    def _mock_pipeline(self, tmp_path):
        """Returns a dict of patches for the full pipeline."""
        fake_video = tmp_path / "v.mp4"
        fake_audio = tmp_path / "a.wav"
        fake_final_video = tmp_path / "out.mp4"
        fake_final_audio = tmp_path / "ua.wav"
        fake_srt = tmp_path / "sub.srt"
        fake_txt = tmp_path / "tr.txt"
        for f in [fake_video, fake_audio, fake_final_video, fake_final_audio, fake_srt, fake_txt]:
            f.touch()

        chunks = [_make_chunk()]
        return {
            "pipeline.download_video": MagicMock(return_value=(fake_video, fake_audio)),
            "pipeline.transcribe": MagicMock(return_value=chunks),
            "pipeline.translate_chunks": MagicMock(return_value=chunks),
            "pipeline.synthesize_speech": MagicMock(return_value=[fake_audio]),
            "pipeline.assemble_audio": MagicMock(return_value=fake_final_audio),
            "pipeline.merge_video_audio": MagicMock(return_value=fake_final_video),
            "pipeline.save_transcript": MagicMock(return_value=(fake_srt, fake_txt)),
        }

    def test_empty_url_returns_error(self, tmp_path):
        v, s, t, msg = p.run_pipeline("", tmp_path)
        assert v is None
        assert "Введіть" in msg

    def test_whitespace_url_returns_error(self, tmp_path):
        v, s, t, msg = p.run_pipeline("   ", tmp_path)
        assert v is None

    def test_full_pipeline_success(self, tmp_path):
        mocks = self._mock_pipeline(tmp_path)
        with patch.multiple("pipeline", **{k.split(".")[-1]: v for k, v in mocks.items()}):
            mock_model = MagicMock()
            video, srt, txt, msg = p.run_pipeline(
                "https://youtube.com/watch?v=abc", tmp_path, model=mock_model
            )
        assert video is not None
        assert srt is not None
        assert txt is not None
        assert "✅" in msg

    def test_no_speech_detected_returns_error(self, tmp_path):
        mocks = self._mock_pipeline(tmp_path)
        mocks["pipeline.transcribe"] = MagicMock(return_value=[])
        with patch.multiple("pipeline", **{k.split(".")[-1]: v for k, v in mocks.items()}):
            mock_model = MagicMock()
            v, s, t, msg = p.run_pipeline(
                "https://youtube.com/watch?v=abc", tmp_path, model=mock_model
            )
        assert v is None
        assert "розпізнати" in msg

    def test_exception_returns_error_string(self, tmp_path):
        with patch("pipeline.download_video", side_effect=RuntimeError("Network error")):
            v, s, t, msg = p.run_pipeline(
                "https://youtube.com/watch?v=abc", tmp_path, model=MagicMock()
            )
        assert v is None
        assert "Network error" in msg

    def test_voice_selection_polina(self, tmp_path):
        mocks = self._mock_pipeline(tmp_path)
        with patch.multiple("pipeline", **{k.split(".")[-1]: v for k, v in mocks.items()}):
            p.run_pipeline(
                "https://youtube.com/watch?v=abc", tmp_path,
                voice_choice="Polina (жіночий)", model=MagicMock()
            )
        synthesize_mock = mocks["pipeline.synthesize_speech"]
        _, _, voice_arg = synthesize_mock.call_args[0]
        assert voice_arg == "uk-UA-PolinaNeural"

    def test_voice_selection_ostap(self, tmp_path):
        mocks = self._mock_pipeline(tmp_path)
        with patch.multiple("pipeline", **{k.split(".")[-1]: v for k, v in mocks.items()}):
            p.run_pipeline(
                "https://youtube.com/watch?v=abc", tmp_path,
                voice_choice="Ostap (чоловічий)", model=MagicMock()
            )
        synthesize_mock = mocks["pipeline.synthesize_speech"]
        _, _, voice_arg = synthesize_mock.call_args[0]
        assert voice_arg == "uk-UA-OstapNeural"

    def test_unknown_voice_falls_back_to_default(self, tmp_path):
        mocks = self._mock_pipeline(tmp_path)
        with patch.multiple("pipeline", **{k.split(".")[-1]: v for k, v in mocks.items()}):
            p.run_pipeline(
                "https://youtube.com/watch?v=abc", tmp_path,
                voice_choice="Unknown Voice", model=MagicMock()
            )
        synthesize_mock = mocks["pipeline.synthesize_speech"]
        _, _, voice_arg = synthesize_mock.call_args[0]
        assert voice_arg == p.DEFAULT_VOICE

    def test_progress_callback_called(self, tmp_path):
        mocks = self._mock_pipeline(tmp_path)
        progress_calls = []
        with patch.multiple("pipeline", **{k.split(".")[-1]: v for k, v in mocks.items()}):
            p.run_pipeline(
                "https://youtube.com/watch?v=abc", tmp_path,
                model=MagicMock(),
                progress_cb=lambda v, desc="": progress_calls.append(v),
            )
        assert len(progress_calls) > 0
        assert progress_calls[-1] == 1.0
