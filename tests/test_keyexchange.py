import asyncio
import importlib.util
from pathlib import Path
import unittest

HAS_DEPS = bool(importlib.util.find_spec('Crypto') and importlib.util.find_spec('telethon'))


@unittest.skipUnless(HAS_DEPS, 'optional cryptographic probe dependencies are not installed')
class KeyExchangeTests(unittest.TestCase):
    def test_spki_public_key_is_normalized_to_pkcs1_for_transport_library(self):
        import rsa
        from tatocollector.keyexchange import to_pkcs1_public_key, verify_public_key
        pem = Path('research/android-static/embedded-public-0.pem').read_bytes()
        normalized = to_pkcs1_public_key(pem)
        key = rsa.PublicKey.load_pkcs1(normalized)
        self.assertEqual(key.e, 65537)
        self.assertEqual(verify_public_key(normalized, '2bad57ceedcd0248'), '2bad57ceedcd0248')

    def test_public_key_fingerprint_matches_independently_observed_server(self):
        from tatocollector.keyexchange import verify_public_key
        pem = Path('research/android-static/embedded-public-0.pem').read_bytes()
        self.assertEqual(verify_public_key(pem, '2bad57ceedcd0248'), '2bad57ceedcd0248')

    def test_different_key_fingerprint_is_rejected_before_any_network_connection(self):
        from tatocollector.keyexchange import verify_public_key
        pem = Path('research/android-static/embedded-public-0.pem').read_bytes()
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            verify_public_key(pem, '1122334455667788')

    def test_bounded_codec_rejects_huge_response_before_reading_payload(self):
        from tatocollector.keyexchange import BoundedAbridgedCodec

        async def check():
            reader = asyncio.StreamReader()
            reader.feed_data(b'\x7f\xff\xff\xff')
            codec = BoundedAbridgedCodec(None)
            with self.assertRaises(ValueError):
                await codec.read_packet(reader)

        asyncio.run(check())


if __name__ == '__main__':
    unittest.main()
