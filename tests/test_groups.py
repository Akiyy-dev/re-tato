import asyncio
import struct
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from tatocollector.groups import SelectionModel, fetch_groups, groups_from_dialogs
from tatocollector.sync import dialogs_request


def dialog(kind, identifier, *, top=1):
    field = {'group': 'chat_id', 'channel': 'channel_id', 'private': 'user_id'}[kind]
    name = {'group': 'peerChat', 'channel': 'peerChannel', 'private': 'peerUser'}[kind]
    return {'_': 'dialog', 'peer': {'_': name, field: identifier}, 'top_message': top}


def page(dialogs=(), chats=(), *, count=None):
    result = {'_': 'messages.dialogsSlice' if count is not None else 'messages.dialogs',
              'dialogs': list(dialogs), 'chats': list(chats), 'users': [], 'messages': []}
    if count is not None:
        result['count'] = count
    for item in dialogs:
        result['messages'].append({'_': 'message', 'id': item['top_message'],
                                   'to_id': item['peer'], 'date': 1700000000})
    return result


class CatalogTests(unittest.TestCase):
    def test_catalog_uses_only_dialog_references_and_namespaces_same_numeric_ids(self):
        parsed = page([dialog('group', 9), dialog('channel', 9), dialog('private', 9)], [
            {'_': 'chat', 'id': 9, 'title': 'Ordinary'},
            {'_': 'channel', 'id': 9, 'title': 'Super', 'megagroup': True},
            {'_': 'chat', 'id': 10, 'title': 'Auxiliary only'}])
        entries = groups_from_dialogs(parsed)
        self.assertEqual({(e['chat_type'], e['chat_id'], e['title']) for e in entries},
                         {('group', 9, 'Ordinary'), ('channel', 9, 'Super')})
        self.assertEqual(next(e['kind'] for e in entries if e['chat_type'] == 'channel'), 'supergroup')

    def test_left_boxed_false_is_joined_but_true_kicked_and_forbidden_are_excluded(self):
        chats = [{'_': 'chatLegacy', 'id': 1, 'title': 'Joined', 'left': {'_': 'boolFalse'}},
                 {'_': 'chatLegacy', 'id': 2, 'title': 'Left', 'left': {'_': 'boolTrue'}},
                 {'_': 'channel', 'id': 3, 'title': 'Kicked', 'kicked': True},
                 {'_': 'channelForbidden', 'id': 4, 'title': 'Forbidden'},
                 {'_': 'channel', 'id': 5, 'title': 'Broadcast', 'broadcast': True}]
        parsed = page([dialog('group', 1), dialog('group', 2), dialog('channel', 3),
                       dialog('channel', 4), dialog('channel', 5)], chats)
        entries = groups_from_dialogs(parsed)
        self.assertEqual({e['chat_id'] for e in entries}, {1, 5})
        self.assertEqual(next(e['kind'] for e in entries if e['chat_id'] == 5), 'channel')

    def test_folder_wrapper_does_not_become_a_group_and_missing_name_uses_id(self):
        parsed = page([dialog('group', 9)], [])
        parsed['dialogs'].append({'_': 'dialogFolder', 'peer': {'_': 'peerChat', 'chat_id': 10}})
        entries = groups_from_dialogs(parsed)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['chat_id'], 9)
        self.assertIn('9', entries[0]['title'])


class FetchGroupsTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_pagination_including_archive_and_deduplication(self):
        first = page([dialog('group', 1)], [{'_': 'chat', 'id': 1, 'title': 'One'}], count=2)
        second = page([dialog('group', 2, top=2)], [{'_': 'chat', 'id': 2, 'title': 'Two'}], count=2)
        archive = page([dialog('group', 1), dialog('channel', 3)], [
            {'_': 'chat', 'id': 1, 'title': 'One'}, {'_': 'channel', 'id': 3, 'title': 'Three'}])
        connection = Mock(call=AsyncMock(side_effect=[b'a', b'b', b'c']))
        schema = Mock(decode_prefix=Mock(side_effect=[(first, 1), (second, 1), (archive, 1)]))
        entries = await fetch_groups(connection, schema)
        self.assertEqual({(e['chat_type'], e['chat_id']) for e in entries}, {('group', 1), ('group', 2), ('channel', 3)})
        self.assertEqual(connection.call.call_args_list[0].args[0], dialogs_request(folder_id=0))
        self.assertEqual(connection.call.call_args_list[1].args[0], dialogs_request(
            {'date': 1700000000, 'id': 1, 'peer': struct.pack('<Ii', 0x179be863, 1)}, 0))
        self.assertEqual(connection.call.call_args_list[2].args[0], dialogs_request(folder_id=1))
        self.assertTrue(all(call.kwargs == {} for call in connection.call.call_args_list))

    async def test_repeated_page_is_rejected_instead_of_returning_partial_directory(self):
        repeated = page([dialog('group', 1)], [{'_': 'chat', 'id': 1, 'title': 'One'}], count=2)
        connection = Mock(call=AsyncMock(return_value=b'a'))
        schema = Mock(decode_prefix=Mock(return_value=(repeated, 1)))
        with self.assertRaisesRegex(ValueError, 'pagination_stalled'):
            await fetch_groups(connection, schema)

    async def test_later_failure_and_truncated_count_do_not_return_partial_directory(self):
        first = page([dialog('group', 1)], [{'_': 'chat', 'id': 1, 'title': 'One'}], count=2)
        connection = Mock(call=AsyncMock(side_effect=[b'a', OSError('synthetic failure')]))
        schema = Mock(decode_prefix=Mock(return_value=(first, 1)))
        with self.assertRaises(OSError):
            await fetch_groups(connection, schema)
        connection.call = AsyncMock(side_effect=[b'a', b'b'])
        schema.decode_prefix = Mock(side_effect=[(first, 1), (page([], [], count=2), 1)])
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            await fetch_groups(connection, schema)

    async def test_wrong_response_and_page_limit_fail_closed(self):
        connection = Mock(call=AsyncMock(return_value=b'a'))
        schema = Mock(decode_prefix=Mock(return_value=({'_': 'updates'}, 1)))
        with self.assertRaisesRegex(ValueError, 'unexpected'):
            await fetch_groups(connection, schema)
        first = page([dialog('group', 1)], [{'_': 'chat', 'id': 1, 'title': 'One'}], count=2)
        schema.decode_prefix = Mock(return_value=(first, 1))
        with self.assertRaisesRegex(ValueError, 'page_limit'):
            await fetch_groups(connection, schema, max_pages=1)

    async def test_channel_page_offset_ignores_same_id_ordinary_chat_metadata(self):
        first = page([dialog('channel', 9)], [
            {'_': 'chat', 'id': 9, 'title': 'Unrelated ordinary'},
            {'_': 'channel', 'id': 9, 'title': 'Channel', 'access_hash': 1234}], count=2)
        second = page([dialog('group', 10)], [{'_': 'chat', 'id': 10, 'title': 'Ten'}], count=2)
        connection = Mock(call=AsyncMock(side_effect=[b'a', b'b', b'c']))
        schema = Mock(decode_prefix=Mock(side_effect=[(first, 1), (second, 1), (page(), 1)]))
        entries = await fetch_groups(connection, schema)
        self.assertEqual(len(entries), 2)
        self.assertEqual(connection.call.call_args_list[1].args[0], dialogs_request(
            {'date': 1700000000, 'id': 1, 'peer': struct.pack('<Iiq', 0x20adaef8, 9, 1234)}, 0))


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.entries = [dict(chat_type='group', chat_id=9, title='Same Name', kind='group'),
                        dict(chat_type='channel', chat_id=9, title='Same Name', kind='supergroup'),
                        dict(chat_type='channel', chat_id=10, title='Other', kind='channel')]

    def test_search_does_not_lose_selection_and_duplicate_names_remain_distinct(self):
        model = SelectionModel(self.entries)
        model.toggle(('group', 9))
        self.assertEqual(len(model.visible('same NAME')), 2)
        self.assertEqual([e['chat_id'] for e in model.visible('10')], [10])
        model.toggle(('channel', 10))
        self.assertEqual(model.selected, frozenset({('group', 9), ('channel', 10)}))
        model.set_visible('same', True)
        self.assertEqual(model.selected, frozenset({('group', 9), ('channel', 9), ('channel', 10)}))
        model.set_visible('same', False)
        self.assertEqual(model.selected, frozenset({('channel', 10)}))

    def test_unavailable_existing_selection_is_visible_and_requires_explicit_removal(self):
        model = SelectionModel(self.entries, {('channel', 999)})
        self.assertIn(('channel', 999), model.selected)
        row = next(e for e in model.visible('999') if e['chat_id'] == 999)
        self.assertTrue(row['unavailable'])
        model.toggle(('channel', 999))
        self.assertNotIn(('channel', 999), model.selected)

    def test_cancel_model_and_filtered_selection_do_not_mutate_inputs(self):
        selected = {('group', 9)}
        model = SelectionModel(self.entries, selected)
        model.clear()
        self.assertEqual(selected, {('group', 9)})
        self.assertEqual(len(self.entries), 3)
        with self.assertRaises(ValueError):
            model.toggle(('private', 9))


class PickerActionsTests(unittest.TestCase):
    def test_save_preserves_unavailable_selection_and_disable_keeps_it_for_later(self):
        from tatocollector.filters import CaptureFilter
        from tatocollector.group_picker import persist_selection
        model = SelectionModel([], {('channel', 999)})
        with patch('tatocollector.group_picker.save_filter') as save:
            result = persist_selection(model, Path('capture-filter.local.json'))
            self.assertEqual(result, CaptureFilter(True, frozenset({('channel', 999)})))
            self.assertEqual(save.call_args.args, (Path('capture-filter.local.json'), result))
            result = persist_selection(model, Path('capture-filter.local.json'), enabled=False)
            self.assertFalse(result.enabled)
            self.assertIn(('channel', 999), result.peers)

    def test_write_failure_propagates_and_invalid_configuration_never_opens_picker(self):
        from tatocollector.group_picker import main, persist_selection
        model = SelectionModel([])
        with patch('tatocollector.group_picker.save_filter', side_effect=OSError('synthetic private file')):
            with self.assertRaises(OSError):
                persist_selection(model, Path('capture-filter.local.json'))
        with patch('tatocollector.group_picker.load_filter', side_effect=ValueError('private invalid document')):
            with patch('builtins.print') as output:
                self.assertEqual(main([]), 1)
        self.assertNotIn('private invalid document', str(output.call_args_list))

    def test_tk_startup_failure_does_not_start_network_worker_or_print_exception_text(self):
        import tkinter as tk
        from tatocollector.filters import CaptureFilter
        from tatocollector.group_picker import main
        with patch('tatocollector.group_picker.load_filter', return_value=CaptureFilter()):
            with patch('tkinter.Tk', side_effect=tk.TclError('synthetic private display name')):
                with patch('tatocollector.group_picker.threading.Thread') as thread:
                    with patch('builtins.print') as output:
                        self.assertEqual(main([]), 1)
        thread.assert_not_called()
        self.assertNotIn('synthetic private display name', str(output.call_args_list))


if __name__ == '__main__':
    unittest.main()
