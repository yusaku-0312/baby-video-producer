"""Add captions and BGM to existing footage locally, without generating new speech."""
from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

from .media import probe
from .render import render
from .state import PipelineError, digest_file


def edit_existing(video: Path, captions_file: Path, bgm: Path | None, output: Path,
                  settings: dict | None = None) -> dict:
    video, output = video.resolve(), output.resolve()
    if output.exists():
        raise PipelineError("Output already exists; choose a new filename")
    if bgm is not None and not bgm.is_file():
        raise PipelineError("BGM file does not exist")
    metadata = probe(video)
    stream = next((s for s in metadata['streams'] if s.get('codec_type') == 'video'), None)
    if not stream or stream['width'] >= stream['height']:
        raise PipelineError("Existing-video editing expects portrait footage")
    if not any(s.get('codec_type') == 'audio' for s in metadata['streams']):
        raise PipelineError("Existing footage has no original audio stream")
    try:
        captions = json.loads(captions_file.read_text())
        if not isinstance(captions, list) or not captions:
            raise ValueError()
        previous_end = 0
        for caption in captions:
            start, end = caption['from'], caption['to']
            if not isinstance(caption['text'], str) or not caption['text'].strip():
                raise ValueError()
            if not (math.isfinite(start) and math.isfinite(end) and
                    previous_end <= start < end <= metadata['duration_seconds'] + .05):
                raise ValueError()
            previous_end = end
    except (OSError, ValueError, TypeError, KeyError):
        raise PipelineError("Captions must contain nonempty text and ordered, nonoverlapping times inside the video") from None
    output.parent.mkdir(parents=True, exist_ok=True)
    assets = output.parent / 'edit-input'
    assets.mkdir(exist_ok=True)
    prepared = video
    if (stream['width'], stream['height']) != (1080, 1920):
        prepared = assets / 'portrait.mp4'
        try:
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(video),
                '-map', '0:v:0', '-map', '0:a:0', '-vf',
                'scale=1080:1920:force_original_aspect_ratio=decrease:force_divisible_by=2,'
                'pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1',
                '-c:v', 'libx264', '-crf', '18', '-preset', 'fast', '-pix_fmt', 'yuv420p',
                '-fps_mode', 'passthrough', '-c:a', 'copy', '-movflags', '+faststart', str(prepared)],
                check=True, capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            raise PipelineError("Existing footage could not be fitted to the portrait canvas") from None
        converted = probe(prepared)
        after = next(s for s in converted['streams'] if s.get('codec_type') == 'video')
        if (abs(converted['duration_seconds'] - metadata['duration_seconds']) > .05 or
                after.get('r_frame_rate') != stream.get('r_frame_rate') or
                (stream.get('nb_frames') and after.get('nb_frames') != stream['nb_frames'])):
            raise PipelineError("Portrait preparation changed the original timeline")
    result = render(prepared, bgm, captions, output, settings)
    result.update({'source_sha256': digest_file(video), 'caption_count': len(captions),
                   'original_speech_preserved': True, 'content_cropped': False,
                   'live_api_called': False, 'perceptual_sync_verified': False,
                   'unverified': ['実聴取による発音・字幕同期・BGM音量バランス']})
    (output.parent / 'edit-verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result
