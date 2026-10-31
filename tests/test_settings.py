import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


@unittest.skipUnless(sys.platform == 'win32', 'Windows DPAPI required')
class SettingsTests(unittest.TestCase):
    def test_http_token_is_encrypted_locally_and_roundtrips_without_network(self):
        from tatocollector.settings import configure_ingest, load_ingest_config
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'collector.local.json'
            token = 'tgfi_synthetic_test_token_only'
            configure_ingest('https://example.invalid/api/ingest/v1/messages', token, path)
            self.assertNotIn(token, path.read_text(encoding='utf-8'))
            configured = load_ingest_config(path)
            self.assertEqual(configured.token, token)
            self.assertNotIn(token, repr(configured))
            document = json.loads(path.read_text(encoding='utf-8'))
            self.assertNotIn(token.encode(), (Path(temp) / document['token_file']).read_bytes())

    def test_invalid_new_token_does_not_overwrite_working_configuration(self):
        from tatocollector.settings import configure_ingest, load_ingest_config
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'collector.local.json'
            configure_ingest('https://example.invalid/api/ingest/v1/messages', 'tgfi_old_valid_token', path)
            with self.assertRaises(ValueError):
                configure_ingest('https://example.invalid/api/ingest/v1/messages', 'not-a-token', path)
            self.assertEqual(load_ingest_config(path).token, 'tgfi_old_valid_token')

    def test_config_publish_failure_keeps_old_url_token_pair_and_cleans_new_file(self):
        from tatocollector.settings import configure_ingest, load_ingest_config
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'collector.local.json'
            old_url = 'https://old.example.invalid/api/ingest/v1/messages'
            old_token = 'tgfi_old_synthetic_token'
            configure_ingest(old_url, old_token, path)
            original_document = path.read_bytes()
            original_files = {item.name for item in Path(temp).iterdir()}
            token_path = Path(temp) / json.loads(original_document)['token_file']
            original_ciphertext = token_path.read_bytes()
            real_replace = Path.replace

            def fail_config_publish(source, target):
                if Path(target) == path:
                    raise OSError('synthetic config publication failure')
                return real_replace(source, target)

            with patch.object(Path, 'replace', fail_config_publish):
                with self.assertRaises(OSError):
                    configure_ingest('https://new.example.invalid/api/ingest/v1/messages',
                                     'tgfi_new_synthetic_token', path)
            restored = load_ingest_config(path)
            self.assertEqual(restored.url, old_url)
            self.assertTrue(restored.token == old_token,
                            'Configuration failure replaced the old protected token')
            self.assertEqual(path.read_bytes(), original_document)
            self.assertEqual(token_path.read_bytes(), original_ciphertext)
            self.assertEqual({item.name for item in Path(temp).iterdir()}, original_files)

    def test_configs_and_rotations_use_independent_tokens_and_keep_old_snapshots_working(self):
        from tatocollector.settings import configure_ingest, load_ingest_config
        with tempfile.TemporaryDirectory() as temp:
            first = Path(temp) / 'first.json'
            second = Path(temp) / 'second.json'
            url = 'https://example.invalid/api/ingest/v1/messages'
            configure_ingest(url, 'tgfi_synthetic_first', first)
            snapshot = first.read_text(encoding='utf-8')
            old_name = json.loads(snapshot)['token_file']
            configure_ingest(url, 'tgfi_synthetic_second', second)
            self.assertTrue(load_ingest_config(first).token == 'tgfi_synthetic_first',
                            'Another configuration replaced the first protected token')
            configure_ingest(url, 'tgfi_synthetic_first_rotated', first)
            new_name = json.loads(first.read_text(encoding='utf-8'))['token_file']
            self.assertNotEqual(old_name, new_name)
            self.assertTrue((Path(temp) / old_name).exists())
            self.assertTrue(load_ingest_config(first).token == 'tgfi_synthetic_first_rotated')
            self.assertTrue(load_ingest_config(second).token == 'tgfi_synthetic_second')
            old_snapshot_path = Path(temp) / 'old-document-snapshot.json'
            old_snapshot_path.write_text(snapshot, encoding='utf-8')
            self.assertTrue(load_ingest_config(old_snapshot_path).token == 'tgfi_synthetic_first',
                            'An in-flight older document can no longer load its token')


if __name__ == '__main__':
    unittest.main()
