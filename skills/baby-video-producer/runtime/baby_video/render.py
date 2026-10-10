"""Isolated local Remotion render using a per-run asset directory."""
import json
import shutil
import subprocess
from pathlib import Path

from .media import probe, check_vertical_output
from .providers import PROJECT
from .state import PipelineError


def render(video: Path, bgm: Path | None, captions: list, destination: Path,
           settings: dict | None = None) -> dict:
    settings = settings or {}
    source = probe(video)
    stream = next(s for s in source["streams"] if s.get("codec_type") == "video")
    numerator, denominator = stream["r_frame_rate"].split("/")
    fps = int(numerator) / int(denominator)
    assets = destination.parent / "render-assets"
    assets.mkdir(exist_ok=True)
    prepared = assets / "source.mp4"
    padding = None
    if (stream.get("width"), stream.get("height")) == (1080, 1920):
        shutil.copyfile(video, prepared)
    elif stream.get("height") == 1920 and 1064 <= stream.get("width", 0) < 1080 and stream["width"] % 2 == 0:
        # Kling may round the uploaded width to a codec block. Keep every input
        # pixel and the original timeline; only add small symmetric side bars.
        horizontal = (1080 - stream["width"]) // 2
        try:
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(video), "-map", "0:v:0", "-map", "0:a:0",
                "-vf", "pad=1080:1920:(ow-iw)/2:0:color=black", "-c:v", "libx264",
                "-crf", "18", "-preset", "fast", "-pix_fmt", "yuv420p",
                "-fps_mode", "passthrough", "-c:a", "copy", "-movflags", "+faststart",
                str(prepared)], capture_output=True, check=True, timeout=120)
            checked = check_vertical_output(prepared)
            if abs(checked["duration_seconds"] - source["duration_seconds"]) > 0.05:
                raise PipelineError("Input padding changed the timeline")
        except (OSError, subprocess.SubprocessError):
            raise PipelineError("Input side padding failed") from None
        padding = {"input_width": stream["width"], "input_height": stream["height"],
                   "left_px": horizontal, "right_px": horizontal,
                   "content_scaled": False, "timeline_shifted": False}
    else:
        raise PipelineError("Input is outside the approved portrait dimensions")
    bgm_name = None
    if bgm:
        bgm_name = "bgm" + bgm.suffix
        shutil.copyfile(bgm, assets / bgm_name)
    for caption in captions:
        if not isinstance(caption.get("text"), str) or not caption["text"]:
            raise PipelineError("Caption text is missing")
        if not 0 <= caption["from"] < caption["to"] <= source["duration_seconds"] + 0.05:
            raise PipelineError("Caption timing is outside the video")
    props = {"video": "source.mp4", "fps": fps, "bgm": bgm_name, "captions": captions,
             "originalVolume": settings.get("original_volume", 1),
             "bgmVolume": settings.get("bgm_volume", 0.12)}
    if "style" in settings:
        props["style"] = settings["style"]
    props_file = destination.parent / "render-props.json"
    props_file.write_text(json.dumps(props, ensure_ascii=False, indent=2))
    temporary = destination.with_suffix(".part.mp4")
    normalized = destination.with_suffix(".normalized.mp4")
    executable = PROJECT / "node_modules/.bin/remotion"
    args = [str(executable), "render", str(PROJECT / "remotion/index.ts"), "BabyVideo",
            str(temporary.resolve()), "--props", str(props_file.resolve()),
            "--public-dir", str(assets.resolve()), "--codec", "h264", "--pixel-format", "yuv420p",
            "--concurrency", "2"]
    if settings.get("browser_executable"):
        args.extend(["--browser-executable", settings["browser_executable"]])
    try:
        subprocess.run(args, check=True, capture_output=True, timeout=300, cwd=PROJECT)
        meta = probe(temporary)
        video_stream = next(s for s in meta["streams"] if s.get("codec_type") == "video")
        # Remotion's JPEG frame route can flag H.264 as full-range yuvj420p
        # despite the requested pixel format. Normalize the delivered file.
        if video_stream.get("pix_fmt") != "yuv420p":
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-n",
                "-i", str(temporary), "-map", "0:v:0", "-map", "0:a:0",
                "-vf", "scale=in_range=auto:out_range=tv,format=yuv420p", "-color_range", "tv",
                "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-pix_fmt", "yuv420p",
                "-fps_mode", "passthrough", "-c:a", "copy", "-movflags", "+faststart", str(normalized)],
                capture_output=True, check=True, timeout=120)
            normalized.replace(temporary)
        verification = check_vertical_output(temporary)
        temporary.replace(destination)
        if padding:
            verification["input_side_padding"] = padding
        return verification
    except (OSError, subprocess.SubprocessError):
        raise PipelineError("Remotion render failed; inspect local configuration before retrying") from None
    finally:
        temporary.unlink(missing_ok=True)
        normalized.unlink(missing_ok=True)
