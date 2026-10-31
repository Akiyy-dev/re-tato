// Public Tatotalk Android APK, layer 123. Explicit constructor values and DEX readers only.
// SHA256 3769b604e4b7f66c9a44e00e9cab6e60d7723f4b6d2c357c396ae4bf198a54c4.
// Local Legacy/Custom names describe proven layouts; do not recompute their IDs from names.
// External families: Message, MessageEntity, Chat, User, ChatPhoto, ChatAdminRights, ChatBannedRights.
// Peer definitions may duplicate the previously recovered shared fragment; merge by explicit ID.

---types---

messages.dialogs#15ba6c40 dialogs:Vector<Dialog> messages:Vector<Message> chats:Vector<Chat> users:Vector<User> = messages.Dialogs;
messages.dialogsSlice#71e094f3 count:int dialogs:Vector<Dialog> messages:Vector<Message> chats:Vector<Chat> users:Vector<User> = messages.Dialogs;
messages.dialogsWithParticipants#b1636607 dialogs:Vector<Dialog> messages:Vector<Message> chats:Vector<Chat> users:Vector<User> participants:Vector<ChannelParticipant> = messages.Dialogs;
messages.dialogsSliceWithParticipants#f177e11a count:int dialogs:Vector<Dialog> messages:Vector<Message> chats:Vector<Chat> users:Vector<User> participants:Vector<ChannelParticipant> = messages.Dialogs;

dialog#73abb983 flags:# pinned:flags.2?true unread_mark:flags.3?true delete_mark:flags.5?true peer:Peer top_message:int read_inbox_max_id:int read_outbox_max_id:int unread_count:int unread_mentions_count:int notify_settings:PeerNotifySettings pts:flags.0?int draft:flags.1?DraftMessage folder_id:flags.4?int ttl_period:flags.6?int = Dialog;
dialogLegacy2c171f72#2c171f72 flags:# pinned:flags.2?true unread_mark:flags.3?true delete_mark:flags.5?true peer:Peer top_message:int read_inbox_max_id:int read_outbox_max_id:int unread_count:int unread_mentions_count:int notify_settings:PeerNotifySettings pts:flags.0?int draft:flags.1?DraftMessage folder_id:flags.4?int = Dialog;
dialogFolder#71bd134c flags:# pinned:flags.2?true delete_mark:flags.5?true folder:Folder peer:Peer top_message:int unread_muted_peers_count:int unread_unmuted_peers_count:int unread_muted_messages_count:int unread_unmuted_messages_count:int = Dialog;
folder#ff544e65 flags:# autofill_new_broadcasts:flags.0?true autofill_public_groups:flags.1?true autofill_new_correspondents:flags.2?true id:int title:string photo:flags.3?ChatPhoto = Folder;

inputPeerEmpty#7f3b18ea = InputPeer;
inputPeerSelf#7da07ec9 = InputPeer;
inputPeerUser#7b8e7de6 user_id:int access_hash:long = InputPeer;
inputPeerChat#179be863 chat_id:int = InputPeer;
inputPeerChannel#20adaef8 channel_id:int access_hash:long = InputPeer;
peerUser#9db1bc6d user_id:int = Peer;
peerChat#bad0e5bb chat_id:int = Peer;
peerChannel#bddde532 channel_id:int = Peer;

peerNotifySettingsEmpty#70a68512 = PeerNotifySettings;
peerNotifySettings#9acda4c0 flags:# show_previews:flags.0?true silent:flags.1?true mute_until:int sound:string = PeerNotifySettings;
peerNotifySettingsLegacy8d5e11ee#8d5e11ee mute_until:int sound:string show_previews:Bool events_mask:int = PeerNotifySettings;
peerNotifySettingsPersonalized#10f1b3bb flags:# notifyWhenPersonlizedStatus:flags.0?true = PeerNotifySettings;

draftMessageEmpty#ba4baec5 = DraftMessage;
draftMessage#fd8e711f flags:# no_webpage:flags.1?true reply_to_msg_id:flags.0?int message:string entities:flags.3?Vector<MessageEntity> date:int = DraftMessage;

channelParticipant#50ca31f6 user_id:int date:int nick_name:string = ChannelParticipant;
channelParticipantLegacy15ebac1d#15ebac1d user_id:int date:int = ChannelParticipant;
channelParticipantSelf#d391d2dd user_id:int inviter_id:int date:int nick_name:string = ChannelParticipant;
channelParticipantLegacy98192d61#98192d61 user_id:int inviter_id:int date:int = ChannelParticipant;
channelParticipantLegacy91057fef#91057fef user_id:int inviter_id:int date:int = ChannelParticipant;
channelParticipantKickedLegacy8cc5e69a#8cc5e69a user_id:int kicked_by:int date:int = ChannelParticipant;
channelParticipantCreator#4ed6ec2e flags:# user_id:int nick_name:flags.0?string rank:flags.1?string = ChannelParticipant;
channelParticipantCreatorLegacy2a333e02#2a333e02 user_id:int nick_name:string = ChannelParticipant;
channelParticipantAdmin#86e6074f flags:# can_edit:flags.0?true self:flags.1?true user_id:int inviter_id:int promoted_by:int date:int admin_rights:ChatAdminRights rank:flags.2?string nick_name:flags.3?string = ChannelParticipant;
channelParticipantAdminLegacy5b866fbd#5b866fbd flags:# can_edit:flags.0?true user_id:int inviter_id:int promoted_by:int date:int admin_rights:ChatAdminRights nick_name:string = ChannelParticipant;
channelParticipantAdminLegacyA82fa898#a82fa898 flags:# can_edit:flags.0?true user_id:int inviter_id:int promoted_by:int date:int admin_rights:ChatAdminRights = ChannelParticipant;
channelParticipantBanned#e02ff941 flags:# left:flags.0?true user_id:int kicked_by:int date:int banned_rights:ChatBannedRights nick_name:string = ChannelParticipant;
channelParticipantNickname#57902cca channel_id:int user_id:int nick_name:string = ChannelParticipant;

---functions---

messages.getDialogs#a0ee3b73 flags:# exclude_pinned:flags.0?true folder_id:flags.1?int offset_date:int offset_id:int offset_peer:InputPeer limit:int hash:int = messages.Dialogs;
