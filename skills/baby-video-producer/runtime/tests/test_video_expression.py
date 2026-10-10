import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from baby_video.pipeline import make_payload


class VideoExpressionTests(unittest.TestCase):
    def test_every_video_request_adds_continuous_smile_direction_to_custom_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assets = root / 'artifacts'
            assets.mkdir()
            (assets / 'image-provenance.json').write_text(json.dumps({'file': 'image.png'}))
            prompt_file = root / 'video-prompt.txt'
            job = {'video_prompt_file': prompt_file.name, 'video_duration_seconds': 9}
            fal = Mock()
            fal.upload.return_value = 'https://example.invalid/image.png'
            for custom in ('Keep the same baby in a cozy nursery.',
                           'Use a calm expression and a locked camera.'):
                prompt_file.write_text(custom)
                with patch('baby_video.pipeline.probe', return_value={'duration_seconds': 8.8}), \
                        patch('baby_video.pipeline.image_dimensions'):
                    payload = make_payload('video', fal, assets, job, root / 'job.json')
                self.assertTrue(payload['prompt'].startswith(custom))
                self.assertIn('natural, happy smile from the very first frame through the final frame', payload['prompt'])
                self.assertIn('mouth fully visible and free to move naturally for later speech lip-sync', payload['prompt'])
                self.assertIn('Do not drift into a neutral, serious, sad or frowning expression', payload['prompt'])
                self.assertEqual(payload['duration'], 9)
                self.assertEqual(payload['prompt_expansion_mode'], 'disabled')
                self.assertEqual(prompt_file.read_text(), custom)

    def test_lipsync_still_receives_only_video_and_audio(self):
        fal = Mock()
        fal.upload.side_effect = ['https://example.invalid/video.mp4', 'https://example.invalid/voice.mp3']
        with patch('baby_video.pipeline.preflight_lipsync'):
            payload = make_payload('lipsync', fal, Path('/unused/artifacts'), {}, Path('/unused/job.json'))
        self.assertEqual(set(payload), {'video_url', 'audio_url'})


if __name__ == '__main__':
    unittest.main()
