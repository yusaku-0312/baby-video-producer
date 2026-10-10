"""Local media checks, performed before a chargeable lip-sync submission."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .state import PipelineError


@dataclass(frozen=True)
class LipSyncContract:
    model: str
    min_video_seconds: float
    max_video_seconds: float
    max_audio_bytes: int | None = None
    max_video_bytes: int | None = None
    audio_must_fit_video: bool = True
    min_audio_seconds: float = 2
    max_audio_seconds: float = 60
    min_video_side: int | None = None
    max_video_side: int | None = None


def probe(path: Path) -> dict:
    if not path.is_file():
        raise PipelineError("Media file does not exist")
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise PipelineError("ffprobe is required for preflight checks")
    try:
        result = subprocess.run([ffprobe, "-v", "error", "-show_format", "-show_streams",
                                 "-of", "json", str(path.resolve())],
                                capture_output=True, timeout=30, check=True)
        metadata = json.loads(result.stdout)
        duration = float(metadata.get("format", {}).get("duration", 0))
    except (subprocess.SubprocessError, ValueError, KeyError):
        raise PipelineError("Media probe failed; file is not suitable for submission") from None
    if duration <= 0:
        raise PipelineError("Media duration is missing or invalid")
    return {"duration_seconds": duration, "size_bytes": path.stat().st_size,
            "streams": metadata.get("streams", [])}


def image_dimensions(path: Path) -> dict:
    if not path.is_file() or not 0 < path.stat().st_size <= 50_000_000:
        raise PipelineError("Image file is missing or outside the size limit")
    try:
        response = subprocess.run([shutil.which("ffprobe") or "ffprobe", "-v", "error",
            "-select_streams", "v:0", "-show_entries", "stream=width,height,codec_name",
            "-of", "json", str(path.resolve())], capture_output=True, check=True, timeout=30)
        stream = json.loads(response.stdout)["streams"][0]
        if stream["codec_name"] not in {"png", "mjpeg", "webp"} or not (
            0 < stream["width"] * stream["height"] <= 33_554_432):
            raise ValueError()
        return {"width": stream["width"], "height": stream["height"], "codec": stream["codec_name"]}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError):
        raise PipelineError("Imported file is not a supported raster image") from None


def check_speech_duration(audio: Path) -> dict:
    """Exclude leading/trailing silence from the 8.5–10 second reading window."""
    # Imported here because caption alignment also uses the media probe.
    from .captions import audio_activity

    metadata = probe(audio)
    try:
        with tempfile.TemporaryDirectory(prefix="baby-speech-") as directory:
            wav = Path(directory) / "voice.wav"
            subprocess.run([shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-i", str(audio),
                            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
                           capture_output=True, check=True, timeout=30)
            activity = audio_activity(wav)
    except (OSError, subprocess.SubprocessError):
        raise PipelineError("Speech duration could not be measured") from None
    start = activity[0]["from"] if activity else None
    end = activity[-1]["to"] if activity else None
    span = round(end - start, 4) if activity else 0
    problems = []
    if not activity:
        problems.append("no_audio_activity")
    if span < 8.5:
        problems.append("speech_too_short")
    if span > 10:
        problems.append("speech_too_long")
    if metadata["duration_seconds"] > 10:
        problems.append("audio_file_exceeds_ten_seconds")
    return {"passed": not problems, "problems": problems,
            "minimum_seconds": 8.5, "target_seconds": 9, "maximum_seconds": 10,
            "speech_start_seconds": start, "speech_end_seconds": end,
            "speech_span_seconds": span, "file_duration_seconds": metadata["duration_seconds"],
            "audio_activity": activity, "method": "20ms_rms_activity_first_to_last",
            "note": "Natural sentence pauses are included; leading/trailing silence is excluded. Acoustic activity is not semantic ASR or perceptual verification.",
            "perceptual_quality_verified": False}


def preflight_lipsync(video: Path, audio: Path, contract: LipSyncContract) -> dict:
    v, a = probe(video), probe(audio)
    if not any(s.get("codec_type") == "video" for s in v["streams"]):
        raise PipelineError("Video input has no video stream")
    if not any(s.get("codec_type") == "audio" for s in a["streams"]):
        raise PipelineError("Audio input has no audio stream")
    if not contract.min_video_seconds <= v["duration_seconds"] <= contract.max_video_seconds:
        raise PipelineError("Video duration violates the selected lip-sync model contract; no trimming or splitting was performed")
    if contract.max_video_bytes is not None and v["size_bytes"] > contract.max_video_bytes:
        raise PipelineError("Video input exceeds model size limit")
    if contract.max_audio_bytes is not None and a["size_bytes"] > contract.max_audio_bytes:
        raise PipelineError("Audio input exceeds model size limit")
    if not contract.min_audio_seconds <= a["duration_seconds"] <= contract.max_audio_seconds:
        raise PipelineError("Audio duration violates the selected model contract")
    if contract.min_video_side is not None or contract.max_video_side is not None:
        for s in v["streams"]:
            if s.get("codec_type") == "video":
                for side in (s.get("width", 0), s.get("height", 0)):
                    if contract.min_video_side is not None and side < contract.min_video_side:
                        raise PipelineError("Video dimensions are below the model minimum")
                    if contract.max_video_side is not None and side > contract.max_video_side:
                        raise PipelineError("Video dimensions exceed the model maximum")
    if contract.audio_must_fit_video and a["duration_seconds"] > v["duration_seconds"] + 0.05:
        raise PipelineError("Audio is longer than the video; resolve the duration before submission")
    return {"model": contract.model, "video_seconds": v["duration_seconds"],
            "audio_seconds": a["duration_seconds"], "ready": True}


def upscale_portrait(source: Path, destination: Path) -> dict:
    """Scale and minimally crop to 9:16, keeping timestamps and frame cadence."""
    before = probe(source)
    temporary = destination.with_suffix(".part.mp4")
    command = [shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error",
               "-n", "-i", str(source.resolve()), "-map", "0:v:0", "-map", "0:a?",
               "-vf", "scale=-2:1920,crop=w='min(iw,1080)':h=1920,pad=1080:1920:(ow-iw)/2:0,setsar=1",
               "-fps_mode", "passthrough", "-c:v", "libx264", "-preset", "fast", "-crf", "18",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(temporary)]
    try:
        subprocess.run(command, capture_output=True, check=True, timeout=120)
        after = probe(temporary)
        original = next(s for s in before["streams"] if s.get("codec_type") == "video")
        converted = next(s for s in after["streams"] if s.get("codec_type") == "video")
        if abs(before["duration_seconds"] - after["duration_seconds"]) > 0.05:
            raise PipelineError("Portrait conversion changed the clip duration")
        if original.get("r_frame_rate") != converted.get("r_frame_rate"):
            raise PipelineError("Portrait conversion changed the source frame rate")
        if original.get("nb_frames") and converted.get("nb_frames") != original.get("nb_frames"):
            raise PipelineError("Portrait conversion changed the frame count")
        if converted.get("width") != 1080 or converted.get("height") != 1920:
            raise PipelineError("Portrait conversion has incorrect dimensions")
        temporary.replace(destination)
        return {"source_seconds": before["duration_seconds"], "output_seconds": after["duration_seconds"],
                "source_fps": original.get("r_frame_rate"), "output_fps": converted.get("r_frame_rate"),
                "source_frames": original.get("nb_frames"), "output_frames": converted.get("nb_frames"),
                "width": 1080, "height": 1920, "vertical_content_cropped": False,
                "duration_and_fps_preserved": True}
    except (OSError, subprocess.SubprocessError, StopIteration):
        raise PipelineError("Local portrait conversion failed") from None
    finally:
        temporary.unlink(missing_ok=True)


def check_vertical_output(path: Path, width: int = 1080, height: int = 1920) -> dict:
    media = probe(path)
    videos = [s for s in media["streams"] if s.get("codec_type") == "video"]
    audios = [s for s in media["streams"] if s.get("codec_type") == "audio"]
    if len(videos) != 1 or not audios:
        raise PipelineError("Final output requires one video and an audio stream")
    stream = videos[0]
    if (stream.get("width"), stream.get("height")) != (width, height):
        raise PipelineError("Final output dimensions do not match the requested portrait format")
    if stream.get("codec_name") != "h264" or stream.get("pix_fmt") != "yuv420p" or audios[0].get("codec_name") != "aac":
        raise PipelineError("Final output must use H.264/yuv420p video and AAC audio")
    try:
        subprocess.run([shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-i",
                        str(path.resolve()), "-f", "null", "-"],
                       capture_output=True, check=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        raise PipelineError("Final output failed the decode integrity check") from None
    return {"width": width, "height": height, "duration_seconds": media["duration_seconds"],
            "video_codec": stream.get("codec_name"), "audio_codec": audios[0].get("codec_name"),
            "fps": stream.get("r_frame_rate"), "pixel_format": stream.get("pix_fmt"),
            "video_start_seconds": float(stream.get("start_time", 0)),
            "audio_start_seconds": float(audios[0].get("start_time", 0)),
            "decode_verified": True}
