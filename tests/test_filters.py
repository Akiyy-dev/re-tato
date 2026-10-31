import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tatocollector.filters import (CaptureFilter, FilterManager, MAX_FILTER_BYTES,
                                   MAX_FILTER_PEERS, load_filter, save_filter)


class CaptureFilterTests(unittest.TestCase):
    def test_disabled_filter_keeps_all_existing_message_types(self):
        policy = CaptureFilter()
        for kind in ('private', 'group', 'channel'):
            self.assertTrue(policy.allows({'chat_type': kind, 'chat_id': 123}))

    def test_enabled_empty_filter_matches_nothing(self):
        policy = CaptureFilter(enabled=True)
        for kind in ('private', 'group', 'channel'):
            self.assertFalse(policy.allows({'chat_type': kind, 'chat_id': 123}))

    def test_matching_uses_namespace_and_id_not_names_or_direction(self):
        policy = CaptureFilter(True, frozenset({('group', 123), ('channel', 456)}))
        self.assertTrue(policy.allows({'chat_type': 'group', 'chat_id': 123, 'conversation_name': 'old'}))
        self.assertTrue(policy.allows({'chat_type': 'group', 'chat_id': 123, 'conversation_name': 'renamed', 'out': True}))
        self.assertTrue(policy.allows({'chat_type': 'channel', 'chat_id': 456}))
        self.assertFalse(policy.allows({'chat_type': 'channel', 'chat_id': 123}))
        self.assertFalse(policy.allows({'chat_type': 'private', 'chat_id': 123}))
        self.assertFalse(policy.allows({'chat_type': 'group', 'chat_id': 789, 'conversation_name': 'old'}))

    def test_enabled_filter_rejects_malformed_event_identity(self):
        policy = CaptureFilter(True, frozenset({('group', 1)}))
        for event in ({}, None, {'chat_type': 'group', 'chat_id': True},
                      {'chat_type': 'group', 'chat_id': '1'}, {'chat_type': 'group', 'chat_id': -1}):
            with self.subTest(event=event):
                self.assertFalse(policy.allows(event))

    def test_constructor_rejects_invalid_or_mutable_rules(self):
        for enabled, peers in ((1, frozenset()), (True, {('group', 1)}),
                               (True, frozenset({('private', 1)})),
                               (True, frozenset({('group', True)})),
                               (True, frozenset({('channel', 0)})),
                               (True, frozenset({('channel', 2**63)})),
                               (True, frozenset({('group', 1, 'extra')}))):
            with self.subTest(enabled=enabled, peers=peers), self.assertRaises(ValueError):
                CaptureFilter(enabled, peers)

    def test_rules_are_not_exposed_in_repr(self):
        self.assertNotIn('123456789', repr(CaptureFilter(True, frozenset({('group', 123456789)}))))


class FilterFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'filter.local.json'

    def write_json(self, value):
        self.path.write_text(json.dumps(value), encoding='utf-8')

    def test_initial_missing_configuration_keeps_compatible_all_mode(self):
        self.assertEqual(load_filter(self.path), CaptureFilter())

    def test_saved_configuration_roundtrips_with_only_ids_and_types(self):
        policy = CaptureFilter(True, frozenset({('group', 1), ('channel', 2**63 - 1)}))
        save_filter(self.path, policy)
        self.assertEqual(load_filter(self.path), policy)
        document = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(set(document), {'enabled', 'include'})
        for peer in document['include']:
            self.assertEqual(set(peer), {'chat_type', 'chat_id'})
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_disabled_configuration_retains_chosen_ids_without_applying_them(self):
        policy = CaptureFilter(False, frozenset({('group', 1)}))
        save_filter(self.path, policy)
        restored = load_filter(self.path)
        self.assertEqual(restored, policy)
        self.assertTrue(restored.allows({'chat_type': 'private', 'chat_id': 9}))

    def test_invalid_shape_fields_and_identity_never_load_as_all(self):
        invalid = [None, [], {}, {'enabled': True}, {'enabled': 1, 'include': []},
                   {'enabled': False, 'include': [], 'unexpected': 'private'},
                   {'enabled': True, 'include': None},
                   {'enabled': True, 'include': [{'chat_type': 'private', 'chat_id': 1}]},
                   {'enabled': True, 'include': [{'chat_type': 'group', 'chat_id': True}]},
                   {'enabled': True, 'include': [{'chat_type': 'group', 'chat_id': -1}]},
                   {'enabled': True, 'include': [{'chat_type': 'channel', 'chat_id': 2**63}]},
                   {'enabled': True, 'include': [{'chat_type': 'group', 'chat_id': 1, 'title': 'private'}]},
                   {'enabled': True, 'include': [{'chat_type': 'group', 'chat_id': 1}] * 2}]
        for document in invalid:
            with self.subTest(document=document):
                self.write_json(document)
                with self.assertRaises(ValueError):
                    load_filter(self.path)

    def test_duplicate_json_keys_malformed_utf8_and_nonfinite_values_are_rejected(self):
        for data in (b'{"enabled":true,"enabled":false,"include":[]}',
                     b'{"enabled":true,"include":[{"chat_type":"group","chat_id":1,"chat_id":2}]}',
                     b'{"enabled":NaN,"include":[]}', b'\xff', b'{'):
            with self.subTest(data=data):
                self.path.write_bytes(data)
                with self.assertRaises(ValueError):
                    load_filter(self.path)

    def test_config_read_is_bounded_and_too_many_rules_are_rejected(self):
        self.path.write_bytes(b' ' * (MAX_FILTER_BYTES + 1))
        with self.assertRaises(ValueError):
            load_filter(self.path)
        self.write_json({'enabled': True, 'include': [
            {'chat_type': 'group', 'chat_id': number + 1} for number in range(MAX_FILTER_PEERS + 1)]})
        with self.assertRaises(ValueError):
            load_filter(self.path)

    def test_maximum_number_of_int64_rules_can_be_saved_and_loaded(self):
        peers = frozenset(('channel', 2**63 - 1 - number) for number in range(MAX_FILTER_PEERS))
        policy = CaptureFilter(True, peers)
        save_filter(self.path, policy)
        self.assertLessEqual(self.path.stat().st_size, MAX_FILTER_BYTES)
        self.assertEqual(load_filter(self.path), policy)

    def test_failed_atomic_replace_keeps_old_configuration_and_cleans_temporary(self):
        old = CaptureFilter(True, frozenset({('group', 1)}))
        save_filter(self.path, old)
        original = self.path.read_bytes()
        with patch.object(Path, 'replace', side_effect=OSError('synthetic error')):
            with self.assertRaises(OSError):
                save_filter(self.path, CaptureFilter(True, frozenset({('channel', 2)})))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_manager_reloads_updates_and_recovers_after_invalid_config(self):
        manager = FilterManager(self.path)
        self.assertFalse(manager.current().enabled)
        first = CaptureFilter(True, frozenset({('group', 1)}))
        save_filter(self.path, first)
        self.assertEqual(manager.current(), first)
        self.path.write_text('{"enabled":false,"include":[],"private":"must not leak"}', encoding='utf-8')
        closed = manager.current()
        self.assertTrue(closed.enabled)
        self.assertEqual(closed.peers, frozenset())
        self.assertTrue(manager.status()['error'])
        self.assertNotIn('must not leak', json.dumps(manager.status()))
        second = CaptureFilter(True, frozenset({('channel', 2)}))
        save_filter(self.path, second)
        self.assertEqual(manager.current(), second)
        self.assertIsNone(manager.status()['error'])

    def test_manager_closes_if_seen_configuration_is_deleted(self):
        save_filter(self.path, CaptureFilter(True, frozenset({('group', 1)})))
        manager = FilterManager(self.path)
        self.assertTrue(manager.current().enabled)
        self.path.unlink()
        self.assertEqual(manager.current(), CaptureFilter(True))
        self.assertTrue(manager.status()['error'])

    def test_manager_closes_on_unreadable_configuration_and_retries_without_stat_change(self):
        policy = CaptureFilter(True, frozenset({('group', 1)}))
        save_filter(self.path, policy)
        manager = FilterManager(self.path)
        with patch.object(Path, 'open', side_effect=PermissionError('private/path')):
            self.assertEqual(manager.current(), CaptureFilter(True))
        self.assertEqual(manager.current(), policy)

    def test_cached_all_mode_closes_when_access_is_lost_without_metadata_change(self):
        policy = CaptureFilter()
        save_filter(self.path, policy)
        manager = FilterManager(self.path)
        self.assertEqual(manager.current(), policy)
        # Changing a Windows ACL need not change size, mtime, creation time or
        # file identity. An old valid all-mode cache must not mask lost access.
        with patch.object(Path, 'open', side_effect=PermissionError('synthetic ACL change')):
            self.assertEqual(manager.current(), CaptureFilter(True))
            self.assertTrue(manager.status()['error'])
        self.assertEqual(manager.current(), policy)

    def test_delete_between_manager_stat_and_open_does_not_fall_back_to_all(self):
        save_filter(self.path, CaptureFilter(True, frozenset({('group', 1)})))
        manager = FilterManager(self.path)
        with patch.object(Path, 'open', side_effect=FileNotFoundError('synthetic race')):
            self.assertEqual(manager.current(), CaptureFilter(True))
        self.assertTrue(manager.status()['enabled'])

    def test_status_contains_only_safe_summary_values(self):
        save_filter(self.path, CaptureFilter(True, frozenset({('group', 123456789)})))
        manager = FilterManager(self.path)
        status = manager.status()
        self.assertEqual(status, {'enabled': True, 'selected_count': 1, 'error': None})
        self.assertNotIn('123456789', json.dumps(status))
        self.assertNotIn(self.temp.name, json.dumps(status))


if __name__ == '__main__':
    unittest.main()
