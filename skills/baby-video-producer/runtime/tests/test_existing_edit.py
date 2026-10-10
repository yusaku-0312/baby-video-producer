import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from baby_video.state import PipelineError, digest_file


class ExistingEditTests(unittest.TestCase):
    def test_short_existing_speech_is_kept_and_portrait_conversion_preserves_timeline(self):
        from baby_video.edit import edit_existing

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'original.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                            'color=c=white:s=384x672:r=30', '-f', 'lavfi', '-i',
                            'sine=f=440:r=44100', '-t', '0.3', '-c:v', 'libx264',
                            '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
                            str(source)], check=True, capture_output=True)
            before = digest_file(source)
            captions = root / 'captions.json'
            captions.write_text(json.dumps([{'text': 'こんにちは', 'from': 0.03, 'to': 0.25}]))
            output = root / 'edited/final.mp4'
            with patch('baby_video.edit.render', return_value={'decode_verified': True}) as renderer:
                result = edit_existing(source, captions, None, output)
                prepared, _, lines, destination, _ = renderer.call_args.args
                self.assertEqual(destination, output.resolve())
                self.assertEqual(lines[0]['text'], 'こんにちは')
                from baby_video.media import probe
                media = probe(prepared)
                self.assertEqual((media['streams'][0]['width'], media['streams'][0]['height']), (1080, 1920))
                self.assertAlmostEqual(media['duration_seconds'], 0.3, delta=0.05)
                self.assertEqual(digest_file(source), before)
                self.assertTrue(result['original_speech_preserved'])
                self.assertFalse(result['live_api_called'])

    def test_invalid_timing_or_existing_output_never_starts_rendering(self):
        from baby_video.edit import edit_existing

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'original.mp4'
            source.write_bytes(b'original')
            captions = root / 'captions.json'
            output = root / 'final.mp4'
            metadata = {'duration_seconds': 2, 'streams': [
                {'codec_type': 'video', 'width': 1080, 'height': 1920},
                {'codec_type': 'audio'}]}
            with patch('baby_video.edit.probe', return_value=metadata), patch('baby_video.edit.render') as renderer:
                for lines in ([{'text': '字幕', 'from': 1, 'to': 3}],
                              [{'text': '字幕', 'from': 0, 'to': 1}, {'text': '重複', 'from': .5, 'to': 1.5}],
                              [{'text': '', 'from': 0, 'to': 1}],
                              [{'text': '字幕', 'from': float('nan'), 'to': 1}]):
                    captions.write_text(json.dumps(lines))
                    with self.assertRaises(PipelineError):
                        edit_existing(source, captions, None, output)
                output.write_bytes(b'keep')
                captions.write_text('[]')
                with self.assertRaises(PipelineError):
                    edit_existing(source, captions, None, output)
                self.assertEqual(output.read_bytes(), b'keep')
                renderer.assert_not_called()


if __name__ == '__main__':
    unittest.main()
