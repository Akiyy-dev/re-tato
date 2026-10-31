"""Cursor continuity and read-only gap recovery using recovered Tatotalk TL."""

from copy import deepcopy
import struct

from .events import PeerCache, normalize_updates


def merge_cursors(old, new):
    """Merge complete verified candidates without losing independent scopes."""
    if old is None:
        return deepcopy(new)
    if new is None:
        return deepcopy(old)
    result = deepcopy(old | new)
    if old.get('initial_dialogs_done') or new.get('initial_dialogs_done'):
        result['initial_dialogs_done'] = True
    for key in ('pts', 'qts', 'seq', 'date'):
        result[key] = max(old.get(key, 0), new.get(key, 0))
    channels = deepcopy(old.get('channels', {}))
    for cid, candidate in new.get('channels', {}).items():
        previous = channels.get(cid, {})
        channels[cid] = previous | candidate
        channels[cid]['pts'] = max(previous.get('pts', 0), candidate.get('pts', 0))
    result['channels'] = channels
    return result


def difference_request(cursor):
    return struct.pack('<Iiiii', 0x25939651, 0, cursor['pts'], cursor['date'], cursor['qts'])


def channel_request(cid, entry):
    return struct.pack('<IIIiqIii', 0x03173D78, 0, 0xAFEB712E, cid, entry['access_hash'],
                       0x94D42EE7, entry['pts'], 100)


def dialogs_request(offset=None, folder_id=None):
    offset = offset or {}
    flags = (1 if offset else 0) | (2 if folder_id is not None else 0)
    return (struct.pack('<II', 0xA0EE3B73, flags)
            + (struct.pack('<i', folder_id) if folder_id is not None else b'')
            + struct.pack('<ii', offset.get('date', 0), offset.get('id', 0))
            + offset.get('peer', struct.pack('<I', 0x7F3B18EA)) + struct.pack('<ii', 100, 0))


def dialogs_cursor(parsed, cursor):
    result = deepcopy(cursor)
    channels = result.setdefault('channels', {})
    chats = {chat['id']: chat for chat in parsed.get('chats', [])}
    for dialog in parsed.get('dialogs', []):
        cid = dialog.get('peer', {}).get('channel_id')
        chat = chats.get(cid, {})
        if cid is not None and 'pts' in dialog and 'access_hash' in chat:
            entry = channels.setdefault(str(cid), {'pts': 1 if cursor.get('initial_dialogs_done') else dialog['pts']})
            entry['access_hash'] = chat['access_hash']
    return result


def dialogs_offset(parsed):
    dialogs = [dialog for dialog in parsed.get('dialogs', []) if dialog.get('_') != 'dialogFolder']
    if not dialogs:
        return None
    last = dialogs[-1]
    peer, mid = last['peer'], last['top_message']
    name = peer['_']
    if name == 'peerChat':
        wire = struct.pack('<Ii', 0x179BE863, peer['chat_id'])
    else:
        cid = peer['channel_id'] if name == 'peerChannel' else peer['user_id']
        items = parsed['chats'] if name == 'peerChannel' else parsed['users']
        item = next((item for item in items if item['id'] == cid), None)
        if item is None or 'access_hash' not in item:
            raise ValueError('dialog_offset_missing_access_hash')
        wire = struct.pack('<Iiq', 0x20ADAEF8 if name == 'peerChannel' else 0x7B8E7DE6,
                           cid, item['access_hash'])
    candidates = [message for message in parsed.get('messages', []) if message['id'] == mid]
    message = next((m for m in candidates if m.get('to_id') == peer), None)
    if message is None and name == 'peerUser':
        message = next((m for m in candidates if m.get('from_id') == peer['user_id']), None)
    if message is None or 'date' not in message:
        raise ValueError('dialog_offset_missing_top_message')
    return {'date': message['date'], 'id': mid, 'peer': wire}


def flatten(parsed):
    if parsed.get('_') in ('updates', 'updatesCombined'):
        return parsed.get('updates', [])
    if parsed.get('_') == 'updateShort':
        return flatten(parsed['update'])
    return [parsed]


def channel_id(update):
    if 'channel_id' in update:
        return update['channel_id']
    message = update.get('message', {})
    peer = message.get('to_id', {}) if isinstance(message, dict) else {}
    return peer.get('channel_id') if peer.get('_') == 'peerChannel' else None


def advance(cursor, parsed):
    """Return a copy, or the unchanged cursor if a sequence/pts gap is found.

    Events may still be saved on a gap. A successful difference later dedupes
    those events while recovering earlier messages. Channel pts never advances
    the independent global pts counter.
    """
    result, gaps = deepcopy(cursor), set()
    result.setdefault('channels', {})
    if parsed.get('_') == 'updatesTooLong':
        return result, {'global'}
    seq = parsed.get('seq', 0)
    seq_start = parsed.get('seq_start', seq)
    if seq_start and seq_start > result.get('seq', 0) + 1:
        gaps.add('global')
    for update in flatten(parsed):
        name = update.get('_', '')
        cid = channel_id(update)
        if name == 'updateChannelTooLong':
            gaps.add(f'channel:{cid}')
            continue
        if 'pts' not in update:
            continue
        target = result
        if cid is not None and ('Channel' in name or 'channel' in name):
            entry = result['channels'].get(str(cid))
            if entry is None:
                gaps.add(f'channel:{cid}')
                continue
            target = entry
        old, pts, count = target.get('pts', 0), update['pts'], update.get('pts_count', 0)
        if pts > old:
            if old + count != pts:
                gaps.add('global' if target is result else f'channel:{cid}')
            else:
                target['pts'] = pts
    if gaps:
        return deepcopy(cursor), gaps
    result['seq'] = max(result.get('seq', 0), seq)
    result['date'] = max(result.get('date', 0), parsed.get('date', 0))
    return result, gaps


def difference_events(parsed, cursor, account_id, peers=None):
    peers = peers if peers is not None else PeerCache()
    result = deepcopy(cursor)
    name = parsed['_']
    if name == 'updates.differenceTooLong':
        raise ValueError('difference_too_long')
    if name == 'updates.differenceEmpty':
        result.update(date=parsed['date'], seq=parsed['seq'])
        return [], result, False
    if name not in ('updates.difference', 'updates.differenceSlice'):
        raise ValueError('unexpected_difference')
    peers.observe(parsed.get('users', []), parsed.get('chats', []))
    updates = [{'_' : 'updateNewMessage', 'message': item} for item in parsed.get('new_messages', [])]
    updates += parsed.get('other_updates', [])
    events = [event for update in updates for event in normalize_updates(update, account_id, peers)]
    more = name == 'updates.differenceSlice'
    state = parsed['intermediate_state' if more else 'state']
    result.update({key: state[key] for key in ('pts', 'qts', 'date', 'seq')})
    return events, result, more


def channel_events(parsed, cursor, cid, account_id, peers=None):
    peers = peers if peers is not None else PeerCache()
    result = deepcopy(cursor)
    name = parsed['_']
    if name == 'updates.channelDifferenceTooLong':
        raise ValueError('channel_difference_too_long')
    if name not in ('updates.channelDifference', 'updates.channelDifferenceEmpty'):
        raise ValueError('unexpected_channel_difference')
    peers.observe(parsed.get('users', []), parsed.get('chats', []))
    updates = [{'_': 'updateNewChannelMessage', 'message': item} for item in parsed.get('new_messages', [])
               if item.get('date', 0) >= cursor.get('captured_since', 0)]
    updates += parsed.get('other_updates', [])
    events = [event for update in updates for event in normalize_updates(update, account_id, peers)]
    if str(cid) not in result.get('channels', {}):
        raise ValueError('channel_difference_requires_baseline')
    result['channels'][str(cid)]['pts'] = parsed['pts']
    return events, result, not parsed.get('final', False)
