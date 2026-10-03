from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .util import UserError, atomic_write, digest, file_hash, read_json, write_json


def run(args: list[str], timeout=120, cwd=None) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except FileNotFoundError:
        raise UserError(f"Required media tool {args[0]} is not installed.") from None
    except subprocess.TimeoutExpired:
        raise UserError("Media processing exceeded its time budget; retry as a separate job.") from None
    if result.returncode:
        # Don't echo full commands/private paths; diagnostic tail is still useful.
        raise UserError(f"Media processing failed: {result.stderr[-700:].strip()}")
    return result.stdout


def probe(path: Path, decode=True) -> dict:
    if not path.is_file() or path.stat().st_size == 0:
        raise UserError("Video file is missing or empty.")
    source_hash = file_hash(path)
    raw = json.loads(run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)], timeout=60))
    videos = [stream for stream in raw.get("streams", []) if stream.get("codec_type") == "video"]
    audio = [stream for stream in raw.get("streams", []) if stream.get("codec_type") == "audio"]
    if not videos:
        raise UserError("The supplied file has no decodable video stream.")
    duration = float(raw.get("format", {}).get("duration", 0))
    if duration <= 0:
        raise UserError("Video duration is invalid.")
    if decode:
        run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"], timeout=max(60, min(600, int(duration * 2))))
    if file_hash(path) != source_hash:
        raise UserError("Video changed during validation; ingest the finished file again.")
    return {"sha256": source_hash, "bytes": path.stat().st_size, "duration": duration,
            "width": videos[0]["width"], "height": videos[0]["height"],
            "video_codec": videos[0].get("codec_name"), "has_audio": bool(audio),
            "audio_codec": audio[0].get("codec_name") if audio else None}


def render(source: Path, workspace: Path, recipe: dict) -> dict:
    metadata = probe(source)
    options = recipe.get("options", {})
    kind = recipe.get("recipe", "passthrough")
    if kind not in {"passthrough", "standard"}:
        raise UserError("Editing recipe must be passthrough or standard.")
    key = digest({"source": metadata["sha256"], "recipe": recipe,
                  "watermark": file_hash(Path(options["watermark"])) if options.get("watermark") else None,
                  "subtitles": file_hash(Path(options["subtitles"])) if options.get("subtitles") else None})
    output_dir = workspace / "media" / "ready"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{key}{source.suffix.lower() if kind == 'passthrough' else '.mp4'}"
    report = output.with_suffix(".qc.json")
    if output.exists() and report.exists():
        cached = read_json(report)
        if cached.get("sha256") == file_hash(output) and cached.get("source_sha256") == metadata["sha256"] and cached.get("recipe_hash") == key:
            return {"path": str(output), "qc": cached, "recipe_hash": key, "cached": True}
    if kind == "passthrough":
        # Keep original encoding/extension; no silent lossy edit.
        output = output_dir / f"{key}{source.suffix.lower()}"
        with tempfile.NamedTemporaryFile(dir=output_dir, delete=False) as handle:
            temporary=Path(handle.name)
            try:
                with source.open("rb") as incoming: shutil.copyfileobj(incoming,handle)
                handle.flush()
                if file_hash(temporary) != metadata["sha256"]: raise UserError("Source changed during rendering.")
                os.replace(temporary,output)
            finally: temporary.unlink(missing_ok=True)
        metadata["source_sha256"] = metadata["sha256"]
        metadata["recipe_hash"] = key
        write_json(output.with_suffix(".qc.json"), metadata)
        return {"path": str(output), "qc": metadata, "recipe_hash": key, "cached": False}
    with tempfile.TemporaryDirectory(prefix="render-", dir=workspace) as tmp:
        tmpdir = Path(tmp)
        args = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source)]
        if options.get("watermark"):
            watermark = Path(options["watermark"]).expanduser().resolve()
            if not watermark.is_file():
                raise UserError("Configured watermark image is missing.")
            args += ["-i", str(watermark)]
        start = float(options.get("trim_start", 0))
        end = float(options.get("trim_end", metadata["duration"]))
        if not 0 <= start < end <= metadata["duration"]:
            raise UserError("Trim range must fall within the source video.")
        filters = ["setsar=1"]
        if options.get("width") or options.get("height"):
            width, height = int(options.get("width", 0)), int(options.get("height", 0))
            if width < 2 or height < 2 or width % 2 or height % 2 or max(width, height) > 8192:
                raise UserError("Set even output width/height between 2 and 8192.")
            filters += [f"scale={width}:{height}:force_original_aspect_ratio=decrease", f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"]
        else:
            filters += ["pad=ceil(iw/2)*2:ceil(ih/2)*2"]
        if options.get("subtitles"):
            subtitles = Path(options["subtitles"])
            if subtitles.suffix.lower() != ".srt":
                raise UserError("Subtitle input must be an SRT file.")
            shutil.copyfile(subtitles, tmpdir / "captions.srt")
            filters += ["subtitles=captions.srt"]
        if options.get("watermark"):
            size = int(options.get("watermark_width", 120))
            if not 8 <= size <= metadata["width"]:
                raise UserError("Watermark width is outside the frame bounds.")
            args += ["-filter_complex", f"[0:v]{','.join(filters)}[base];[1:v]scale={size}:-1[mark];[base][mark]overlay=W-w-20:H-h-20[out]", "-map", "[out]", "-map", "0:a?"]
        else:
            args += ["-vf", ",".join(filters), "-map", "0:v:0", "-map", "0:a?"]
        args += ["-ss", str(start), "-t", str(end-start), "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart"]
        if options.get("normalize_audio") and metadata["has_audio"]:
            args += ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]
        rendered = tmpdir / "output.mp4"
        args += [str(rendered)]
        run(args, timeout=600, cwd=tmpdir)
        qc = probe(rendered)
        if file_hash(source) != metadata["sha256"]:
            raise UserError("Source changed during rendering; output discarded.")
        qc["source_sha256"] = metadata["sha256"]
        qc["recipe_hash"] = key
        os.replace(rendered, output)
        write_json(report, qc)
    return {"path": str(output), "qc": qc, "recipe_hash": key, "cached": False}


def transcribe(path: Path, workspace: Path, *, language=None, model="small", model_path=None) -> dict:
    """Local transcription only. No customer media leaves the machine here."""
    metadata = probe(path, decode=False)
    if not metadata["has_audio"]:
        raise UserError("Video has no audio. Supply the idea/context instead of an invented transcript.")
    cache_key=digest({"source":metadata["sha256"],"language":language,"model":model,"model_hash":file_hash(Path(model_path).expanduser()) if model_path else None})
    target = workspace / "transcripts" / f"{cache_key}.json"
    if target.exists():
        return read_json(target)
    with tempfile.TemporaryDirectory(prefix="audio-", dir=workspace) as tmp:
        wav = Path(tmp) / "speech.wav"
        run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-vn", "-ar", "16000", "-ac", "1", str(wav)], timeout=180)
        if model_path and shutil.which("whisper-cli"):
            model_file = Path(model_path).expanduser().resolve()
            if not model_file.is_file():
                raise UserError("Local whisper model file not found.")
            output = Path(tmp) / "transcript"
            run(["whisper-cli", "-m", str(model_file), "-f", str(wav), "-l", language or "auto",
                 "-otxt", "-osrt", "-of", str(output)], timeout=600)
            text = output.with_suffix(".txt").read_text().strip()
            srt = output.with_suffix(".srt").read_text()
            segments = []
        else:
            try:
                from faster_whisper import WhisperModel
            except ImportError:
                raise UserError("Install the transcribe extra or configure whisper-cli and a local model. You can also supply a transcript from your host.") from None
            recognizer = WhisperModel(model, device="cpu", compute_type="int8")
            generated, info = recognizer.transcribe(str(wav), language=language)
            segments = [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in generated]
            text = " ".join(s["text"] for s in segments)
            def clock(seconds):
                milliseconds = round(seconds * 1000)
                hours, milliseconds = divmod(milliseconds, 3600000)
                minutes, milliseconds = divmod(milliseconds, 60000)
                seconds, milliseconds = divmod(milliseconds, 1000)
                return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"
            srt = "\n\n".join(f"{i}\n{clock(s['start'])} --> {clock(s['end'])}\n{s['text']}" for i,s in enumerate(segments,1))
        if not text:
            raise UserError("No speech was transcribed. Ask for the video's context.")
        result = {"sha256": metadata["sha256"], "text": text, "segments": segments, "source": "local-transcription", "language": language}
        write_json(target, result)
        atomic_write(target.with_suffix(".srt"), srt)
        return result
