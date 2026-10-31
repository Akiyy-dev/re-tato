import base64
import unittest

from tatocollector.captcha import decode_image_data, slider_answer, slider_y


class CaptchaTests(unittest.TestCase):
    def test_slider_y_uses_540_high_coordinate_space(self):
        self.assertEqual(slider_y(270, 153), 76.5)

    def test_slider_reports_normalized_960_coordinate_not_window_pixels(self):
        self.assertEqual(slider_answer(50, 200), {'x': 240.0})
        with self.assertRaises(ValueError):
            slider_answer(10, 0)

    def test_base64_and_image_data_url_are_decoded_without_network(self):
        image_bytes = b'PNG fixture'
        encoded = base64.b64encode(image_bytes).decode()
        self.assertEqual(decode_image_data(encoded), image_bytes)
        self.assertEqual(decode_image_data('data:image/png;base64,' + encoded), image_bytes)

    def test_remote_urls_and_unbounded_or_invalid_image_data_are_rejected(self):
        for value in ['https://example.invalid/image', '###', 'A' * 4_000_001]:
            with self.assertRaises(ValueError):
                decode_image_data(value)


if __name__ == '__main__':
    unittest.main()
