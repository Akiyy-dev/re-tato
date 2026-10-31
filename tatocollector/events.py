"""Normalize decoded new-message updates without inventing message content."""


def _metadata(value):
    if type(value) is dict:
        return {key: _metadata(item) for key, item in value.items() if not isinstance(item, bytes)}
    if type(value) is list:
        return [_metadata(item) for item in value if not isinstance(item, bytes)]
    return value


class PeerCache:
    def __init__(self):
        self.users = {}
        self.chats = {}

    def observe(self, users=(), chats=()):
        for user in users:
            if isinstance(user, dict) and type(user.get('id')) is int:
                label = ' '.join(str(user.get(key) or '').strip() for key in ('first_name', 'last_name')).strip()
                self.users[user['id']] = label or user.get('username') or f"user:{user['id']}"
        for chat in chats:
            if isinstance(chat, dict) and type(chat.get('id')) is int:
                self.chats[chat['id']] = chat.get('title') or f"chat:{chat['id']}"


def normalize_updates(parsed, account_id, peers=None):
    peers = peers if peers is not None else PeerCache()
    name = parsed.get('_')
    if name in ('updates', 'updatesCombined'):
        peers.observe(parsed.get('users', ()), parsed.get('chats', ()))
        return [event for update in parsed['updates'] for event in normalize_updates(update, account_id, peers)]
    if name == 'updateShort':
        return normalize_updates(parsed['update'], account_id, peers)
    if name in ('updateShortMessage', 'updateShortChatMessage'):
        private = name == 'updateShortMessage'
        chat_id = parsed['user_id'] if private else parsed['chat_id']
        sender_id = (account_id if parsed.get('out') else chat_id) if private else parsed['from_id']
        event = {'chat_type': 'private' if private else 'group', 'chat_id': chat_id,
                 'sender_id': sender_id, 'message_id': parsed['id'], 'text': parsed['message'],
                 'date': parsed['date'], 'out': bool(parsed.get('out')), 'media_type': 'text'}
    elif name in ('updateNewMessage', 'updateNewChannelMessage'):
        message = parsed['message']
        if message.get('_') == 'messageEmpty':
            return []
        destination = message.get('to_id', {})
        destination_type = destination.get('_')
        sender_id = message.get('from_id')
        sender_peer = message.get('from_peer', {})
        sender_is_channel = sender_peer.get('_') == 'peerChannel'
        if sender_id is None:
            sender_id = sender_peer.get('user_id', sender_peer.get('channel_id'))
        if destination_type == 'peerUser':
            chat_type, chat_id = 'private', destination['user_id']
            if not message.get('out') and chat_id == account_id:
                if sender_id is None:
                    raise ValueError('private incoming message lacks sender identity')
                chat_id = sender_id
        elif destination_type == 'peerChat':
            chat_type, chat_id = 'group', destination['chat_id']
        elif destination_type == 'peerChannel':
            # PeerChannel also represents supergroups; keep the namespace stable.
            chat_type, chat_id = 'channel', destination['channel_id']
        else:
            raise ValueError('unknown new-message destination')
        media = message.get('media') or {}
        text = message.get('message') or media.get('caption') or ''
        event = {'chat_type': chat_type, 'chat_id': chat_id, 'sender_id': sender_id,
                 'message_id': message['id'], 'text': text, 'date': message['date'],
                 'out': bool(message.get('out')), 'media_type': media.get('_', 'text')}
        if media:
            event['media'] = _metadata(media)
        if message.get('action'):
            event['service_action'] = _metadata(message['action'])
        if sender_is_channel:
            event['sender_name'] = peers.chats.get(sender_id, f'channel:{sender_id}')
    else:
        return []
    for field in ('chat_id', 'message_id'):
        if type(event[field]) is not int or event[field] <= 0:
            raise ValueError('new-message identity is invalid')
    event['account_id'] = account_id
    event['sender_name'] = event.get('sender_name') or peers.users.get(event['sender_id'])
    event['conversation_name'] = (peers.users if event['chat_type'] == 'private' else peers.chats).get(event['chat_id'])
    for field in ('pts', 'pts_count'):
        if field in parsed:
            event[field] = parsed[field]
    return [event]
