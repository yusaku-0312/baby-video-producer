import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from baby_video.media import probe
from baby_video.render import render
from baby_video.state import digest_file


class RenderGeometryTests(unittest.TestCase):
    def test_provider_width_change_is_normalized_without_changing_source_or_timing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'provider.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                            'color=c=white:s=1072x1920:r=30', '-f', 'lavfi', '-i',
                            'anullsrc=r=44100:cl=stereo', '-t', '0.3', '-c:v', 'libx264',
                            '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
                            str(source)], check=True, capture_output=True)
            source_hash = digest_file(source)
            before = probe(source)
            before_video = before['streams'][0]
            real_run = subprocess.run
            renderer_calls = []

            def run_command(args, **kwargs):
                if Path(args[0]).name == 'remotion':
                    prepared = root / 'render-assets/source.mp4'
                    media = probe(prepared)
                    video = media['streams'][0]
                    self.assertEqual((video['width'], video['height']), (1080, 1920))
                    self.assertEqual(video['r_frame_rate'], before_video['r_frame_rate'])
                    self.assertEqual(video['nb_frames'], before_video['nb_frames'])
                    self.assertAlmostEqual(media['duration_seconds'], before['duration_seconds'], delta=0.05)
                    shutil.copyfile(prepared, args[4])
                    renderer_calls.append(True)
                    return subprocess.CompletedProcess(args, 0)
                return real_run(args, **kwargs)

            with patch('baby_video.render.subprocess.run', side_effect=run_command):
                result = render(source, None, [], root / 'final.mp4')

            self.assertEqual(len(renderer_calls), 1)
            self.assertEqual((result['width'], result['height']), (1080, 1920))
            self.assertTrue(result['decode_verified'])
            self.assertEqual(digest_file(source), source_hash)


if __name__ == '__main__':
    unittest.main()
