"""Read-only joined-group catalog and a name-independent selection model."""

from .sync import dialogs_offset, dialogs_request


_DIALOGS_TYPES = frozenset(('messages.dialogs', 'messages.dialogsSlice',
                          'messages.dialogsWithParticipants', 'messages.dialogsSliceWithParticipants'))


def _flag(value):
    if type(value) is dict:
        return value.get('_') == 'boolTrue'
    return value is True


def _peer_key(peer):
    if not isinstance(peer, dict):
        return None
    for name, field, kind in (('peerChat', 'chat_id', 'group'),
                              ('peerChannel', 'channel_id', 'channel'),
                              ('peerUser', 'user_id', 'private')):
        if peer.get('_') == name:
            identifier = peer.get(field)
            if type(identifier) is not int or not 0 < identifier < 2**63:
                raise ValueError('group_catalog_invalid_identity')
            return kind, identifier
    return None


def _chat_key(chat):
    name, identifier = chat.get('_', ''), chat.get('id')
    if type(identifier) is not int or not 0 < identifier < 2**63:
        return None
    if name.startswith('channel'):
        return 'channel', identifier
    if name.startswith('chat') or name == 'geoChat':
        return 'group', identifier
    return None


def groups_from_dialogs(parsed):
    """List only group/channel peers referenced by actual dialogs on this page.

    The auxiliary chats vector can contain unrelated objects. Its integer IDs
    also span independent chat/channel namespaces, so never index by ID alone.
    """
    chats = {_chat_key(chat): chat for chat in parsed.get('chats', [])}
    result = {}
    for dialog in parsed.get('dialogs', []):
        if dialog.get('_') == 'dialogFolder':
            continue
        key = _peer_key(dialog.get('peer'))
        if key is None or key[0] == 'private':
            continue
        chat = chats.get(key, {})
        name = chat.get('_', '')
        if ('Forbidden' in name or 'Empty' in name
                or any(_flag(chat.get(flag)) for flag in ('left', 'kicked', 'deactivated'))):
            continue
        kind = ('supergroup' if _flag(chat.get('megagroup')) else 'channel') if key[0] == 'channel' else 'group'
        title = chat.get('title') or f'{key[0]}:{key[1]}'
        if type(title) is not str:
            raise ValueError('group_catalog_invalid_title')
        result[key] = {'chat_type': key[0], 'chat_id': key[1], 'title': title, 'kind': kind}
    return list(result.values())


def _dialog_identity(dialog):
    if dialog.get('_') == 'dialogFolder':
        return 'folder', dialog.get('folder', {}).get('id')
    return _peer_key(dialog.get('peer'))


async def fetch_groups(connection, schema, *, max_pages=100):
    """Fetch a complete catalog without changing capture checkpoints or sending.

    Only read-only getDialogs RPCs are issued. Any undecodable, incomplete or
    stalled page fails the entire operation; no partial catalog is returned.
    """
    if type(max_pages) is not int or not 1 <= max_pages <= 100:
        raise ValueError('group_catalog_invalid_page_limit')
    entries = {}
    for folder in (0, 1):
        offset, seen_offsets, seen_dialogs = None, set(), set()
        for _ in range(max_pages):
            raw = await connection.call(dialogs_request(offset, folder))
            parsed, _ = schema.decode_prefix(raw, 'messages.Dialogs')
            if parsed.get('_') not in _DIALOGS_TYPES:
                raise ValueError('group_catalog_unexpected_response')
            dialogs = parsed.get('dialogs', [])
            if type(dialogs) is not list:
                raise ValueError('group_catalog_invalid_dialogs')
            seen_dialogs.update(_dialog_identity(dialog) for dialog in dialogs)
            seen_dialogs.discard(None)
            if len(seen_dialogs) > 20000:
                raise ValueError('group_catalog_size_limit')
            for entry in groups_from_dialogs(parsed):
                entries[(entry['chat_type'], entry['chat_id'])] = entry
            count = parsed.get('count')
            if count is not None and (type(count) is not int or count < 0):
                raise ValueError('group_catalog_invalid_count')
            if count is None or len(seen_dialogs) >= count:
                break
            if not dialogs:
                raise ValueError('group_catalog_incomplete')
            # dialogs_offset uses chats only for a channel offset. Supply its
            # namespace explicitly so an ordinary chat with the same number
            # cannot shadow the channel's access_hash.
            offset_page = parsed | {'chats': [chat for chat in parsed.get('chats', [])
                                              if (_chat_key(chat) or (None,))[0] == 'channel']}
            offset = dialogs_offset(offset_page)
            key = (offset['id'], offset['peer']) if offset else None
            if key is None or key in seen_offsets:
                raise ValueError('group_catalog_pagination_stalled')
            seen_offsets.add(key)
        else:
            raise ValueError('group_catalog_page_limit')
    return sorted(entries.values(), key=lambda item: (item['title'].casefold(), item['chat_type'], item['chat_id']))


class SelectionModel:
    """Search changes visibility only; stable peer IDs own selection state."""

    def __init__(self, entries, selected=()):
        self._entries = {}
        for entry in entries:
            key = self._validate_key((entry['chat_type'], entry['chat_id']))
            self._entries[key] = dict(entry)
        self._selected = {self._validate_key(key) for key in selected}
        for kind, identifier in self._selected - self._entries.keys():
            self._entries[(kind, identifier)] = {
                'chat_type': kind, 'chat_id': identifier,
                'title': '目录中不可见的已选会话', 'kind': 'unavailable', 'unavailable': True}

    @staticmethod
    def _validate_key(key):
        if (type(key) is not tuple or len(key) != 2 or key[0] not in ('group', 'channel')
                or type(key[1]) is not int or not 0 < key[1] < 2**63):
            raise ValueError('invalid_group_selection_identity')
        return key

    @property
    def selected(self):
        return frozenset(self._selected)

    def visible(self, query=''):
        needle = query.strip().casefold()
        entries = [dict(entry) for entry in self._entries.values()
                   if not needle or needle in entry['title'].casefold()
                   or needle in f"{entry['chat_type']}:{entry['chat_id']}"]
        return sorted(entries, key=lambda item: (not item.get('unavailable', False),
                                               item['title'].casefold(), item['chat_type'], item['chat_id']))

    def toggle(self, key):
        self._validate_key(key)
        if key not in self._entries:
            raise ValueError('unknown_group_selection_identity')
        if key in self._selected:
            self._selected.remove(key)
        else:
            self._selected.add(key)

    def set_visible(self, query, selected):
        for entry in self.visible(query):
            key = entry['chat_type'], entry['chat_id']
            if selected:
                self._selected.add(key)
            else:
                self._selected.discard(key)

    def clear(self):
        self._selected.clear()
