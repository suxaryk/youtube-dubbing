import asyncio
import logging
import shutil
import subprocess
import threading
import time
import traceback
import uuid
from pathlib import Path
from urllib.parse import urlparse

from deep_translator import GoogleTranslator
from faster_whisper import WhisperModel
from pydub import AudioSegment
import edge_tts

log = logging.getLogger(__name__)

VOICES = {
    "Polina (жіночий)": "uk-UA-PolinaNeural",
    "Ostap (чоловічий)": "uk-UA-OstapNeural",
}
DEFAULT_VOICE = "uk-UA-PolinaNeural"

# Thread-safe Whisper singleton
_whisper_model: WhisperModel | None = None
_whisper_lock = threading.Lock()


def get_whisper_model(device: str = "cpu", compute_type: str = "int8") -> WhisperModel:
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            _whisper_model = WhisperModel("large-v3", device=device, compute_type=compute_type)
    return _whisper_model


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------

def download_video(url: str, out_dir: Path) -> tuple[Path, Path]:
    """Downloads video and extracts mono 22050 Hz WAV audio."""
    if not url or not url.strip():
        raise ValueError("URL не може бути порожнім")

    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Непідтримуване посилання: {url}")

    video_path = out_dir / "input_video.mp4"
    audio_path = out_dir / "input_audio.wav"

    try:
        subprocess.run(
            [
                "yt-dlp",
                "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "--merge-output-format", "mp4",
                "-o", str(video_path),
                "--no-playlist",
                url.strip(),
            ],
            check=True,
            capture_output=True,
            timeout=600,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"yt-dlp помилка (код {e.returncode}): {e.stderr.decode(errors='replace')}"
        ) from e

    if not video_path.exists():
        raise FileNotFoundError(f"yt-dlp не створив очікуваний файл: {video_path}")

    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(video_path), "-ar", "22050", "-ac", "1", str(audio_path)],
            check=True,
            capture_output=True,
            timeout=300,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffmpeg помилка: {e.stderr.decode(errors='replace')}") from e

    return video_path, audio_path


def transcribe(audio_path: Path, model: WhisperModel) -> list[dict]:
    """Transcribes audio with Whisper; returns list of timed chunks."""
    if not audio_path.exists():
        raise FileNotFoundError(f"Аудіофайл не знайдено: {audio_path}")

    segments, _ = model.transcribe(
        str(audio_path),
        language="en",
        beam_size=5,
        vad_filter=True,
    )
    return [
        {"start": s.start, "end": s.end, "text": s.text.strip()}
        for s in segments
        if s.text.strip()
    ]


def translate_chunks(chunks: list[dict]) -> list[dict]:
    """Translates EN text to UK with 3-attempt retry; falls back to original on error."""
    if not chunks:
        return chunks

    translator = GoogleTranslator(source="en", target="uk")
    failed = 0
    for i, chunk in enumerate(chunks):
        for attempt in range(3):
            try:
                chunk["ua_text"] = translator.translate(chunk["text"]) or chunk["text"]
                break
            except Exception as exc:
                if attempt == 2:
                    log.warning("Помилка перекладу фрагменту %d: %s", i, exc)
                    chunk["ua_text"] = chunk["text"]
                    failed += 1
                else:
                    time.sleep(2 ** attempt)

    if failed > len(chunks) * 0.1:
        log.warning("УВАГА: %d/%d фрагментів не перекладено!", failed, len(chunks))
    return chunks


async def _synthesize_chunk(text: str, out_file: Path, voice: str) -> None:
    """Synthesises one chunk via edge-tts and saves as WAV."""
    mp3_file = out_file.with_suffix(".mp3")
    target_sample_rate = 22050
    try:
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(str(mp3_file))
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(mp3_file),
                "-ar", str(target_sample_rate), "-ac", "1",
                str(out_file)
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
    finally:
        mp3_file.unlink(missing_ok=True)


async def _synthesize_all(chunks: list[dict], chunks_dir: Path, voice: str) -> list[Exception | None]:
    """Runs all chunk synthesis concurrently; returns list of errors (None = success)."""
    tasks = [
        _synthesize_chunk(
            chunk.get("ua_text") or chunk.get("text", ""),
            chunks_dir / f"{i:04d}.wav",
            voice,
        )
        for i, chunk in enumerate(chunks)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return results


def synthesize_speech(
    chunks: list[dict], out_dir: Path, voice: str = DEFAULT_VOICE
) -> list[Path]:
    """Synthesises Ukrainian speech for every chunk; inserts silence on failure."""
    if not chunks:
        return []

    chunks_dir = out_dir / "chunks"
    chunks_dir.mkdir(exist_ok=True)

    # Run all chunks in a single event loop — compatible with Jupyter/Gradio
    results = asyncio.run(_synthesize_all(chunks, chunks_dir, voice))

    audio_files: list[Path] = []
    for i, (chunk, result) in enumerate(zip(chunks, results)):
        out_file = chunks_dir / f"{i:04d}.wav"
        if isinstance(result, Exception):
            log.warning("Помилка синтезу фрагменту %d: %s", i, result)
            duration_ms = max(0, int((chunk["end"] - chunk["start"]) * 1000))
            AudioSegment.silent(duration=duration_ms).export(out_file, format="wav")
        audio_files.append(out_file)

        if (i + 1) % 10 == 0:
            log.info("%d/%d фрагментів синтезовано...", i + 1, len(chunks))

    return audio_files


def stretch_audio(audio: AudioSegment, target_duration_ms: int) -> AudioSegment:
    """Time-stretches audio to fit target duration via frame-rate trick."""
    if target_duration_ms <= 0:
        return AudioSegment.silent(duration=0)
    current_duration = len(audio)
    if current_duration == 0:
        return AudioSegment.silent(duration=target_duration_ms)
    ratio = current_duration / target_duration_ms
    ratio = max(0.5, min(2.0, ratio))
    if abs(ratio - 1.0) < 0.05:
        return audio
    new_frame_rate = max(1000, int(audio.frame_rate * ratio))
    return audio._spawn(
        audio.raw_data, overrides={"frame_rate": new_frame_rate}
    ).set_frame_rate(audio.frame_rate)


def assemble_audio(
    chunks: list[dict], audio_files: list[Path], out_dir: Path
) -> Path:
    """Overlays all chunk WAVs at their original timestamps."""
    if not chunks:
        raise ValueError("Список чанків порожній")
    if len(chunks) != len(audio_files):
        raise ValueError(
            f"Кількість чанків ({len(chunks)}) не збігається з кількістю файлів ({len(audio_files)})"
        )

    total_ms = int(chunks[-1]["end"] * 1000) + 2000
    final_audio = AudioSegment.silent(duration=total_ms)

    for chunk, audio_file in zip(chunks, audio_files):
        part = AudioSegment.from_wav(str(audio_file))
        position_ms = int(chunk["start"] * 1000)
        final_audio = final_audio.overlay(part, position=position_ms)

    out_path = out_dir / "ukrainian_audio.wav"
    final_audio.export(str(out_path), format="wav")
    return out_path


def merge_video_audio(video_path: Path, audio_path: Path, out_dir: Path) -> Path:
    """Replaces video audio track with the Ukrainian dub."""
    out_path = out_dir / "output_ukrainian.mp4"
    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(video_path),
                "-i", str(audio_path),
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c:v", "copy",
                "-shortest",
                str(out_path),
            ],
            check=True,
            capture_output=True,
            timeout=300,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffmpeg помилка при злитті: {e.stderr.decode(errors='replace')}") from e
    return out_path


def _fmt_time(t: float) -> str:
    """Formats seconds as SRT timestamp: HH:MM:SS,mmm."""
    t = max(0.0, t)
    h, remainder = divmod(t, 3600)
    m, s = divmod(remainder, 60)
    ms = int(round((s % 1) * 1000))
    return f"{int(h):02}:{int(m):02}:{int(s):02},{ms:03}"


def save_transcript(chunks: list[dict], out_dir: Path) -> tuple[Path, Path]:
    """Saves bilingual SRT subtitles and plain-text transcript."""
    srt_path = out_dir / "subtitles_ua.srt"
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, chunk in enumerate(chunks, 1):
            f.write(f"{i}\n")
            f.write(f"{_fmt_time(chunk['start'])} --> {_fmt_time(chunk['end'])}\n")
            f.write(f"{chunk.get('ua_text', chunk['text'])}\n\n")

    txt_path = out_dir / "transcript.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        for chunk in chunks:
            f.write(f"[{chunk['start']:.1f}s] EN: {chunk['text']}\n")
            f.write(f"[{chunk['start']:.1f}s] UA: {chunk.get('ua_text', '')}\n\n")

    return srt_path, txt_path


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_pipeline(
    youtube_url: str,
    work_dir: Path,
    voice_choice: str = "Polina (жіночий)",
    model: WhisperModel | None = None,
    progress_cb=None,
) -> tuple[Path | None, Path | None, Path | None, str]:
    """Full dubbing pipeline. Returns (video, srt, txt, status_message)."""
    if not youtube_url or not youtube_url.strip():
        return None, None, None, "❌ Введіть YouTube посилання"

    voice = VOICES.get(voice_choice, DEFAULT_VOICE)

    # Unique job dir per run — safe for parallel requests
    job_dir = work_dir / f"job_{uuid.uuid4().hex[:8]}"
    job_dir.mkdir(parents=True)

    def _progress(value: float, desc: str = "") -> None:
        if progress_cb:
            progress_cb(value, desc=desc)
        log.info("[%.0f%%] %s", value * 100, desc)

    try:
        _progress(0.10, "⬇️ Завантажую відео...")
        video_path, audio_path = download_video(youtube_url.strip(), job_dir)

        _progress(0.25, "📝 Транскрибую (Whisper)...")
        _model = model or get_whisper_model()
        chunks = transcribe(audio_path, _model)
        if not chunks:
            return None, None, None, "❌ Не вдалося розпізнати мовлення"

        _progress(0.45, "🌐 Перекладаю на українську...")
        chunks = translate_chunks(chunks)

        _progress(0.60, "🎙️ Синтезую мовлення...")
        audio_files = synthesize_speech(chunks, job_dir, voice)

        _progress(0.80, "🔧 Збираю аудіо...")
        final_audio = assemble_audio(chunks, audio_files, job_dir)

        _progress(0.90, "🎬 Збираю відео...")
        final_video = merge_video_audio(video_path, final_audio, job_dir)

        _progress(0.95, "📄 Зберігаю субтитри...")
        srt_path, txt_path = save_transcript(chunks, job_dir)

        _progress(1.0, "✅ Готово!")
        report = (
            f"✅ Дублювання завершено!\n\n"
            f"📊 Фрагментів: {len(chunks)}\n"
            f"⏱️ Тривалість: {chunks[-1]['end']:.1f} сек\n"
            f"🎤 Голос: {voice}\n"
        )
        return final_video, srt_path, txt_path, report

    except Exception as exc:
        msg = f"❌ {exc}\n\n{traceback.format_exc()}"
        log.error(msg)
        return None, None, None, msg
