// Recovered offline from Tatotalk.exe SHA256 76c9c41b7dfac2861f3d30aea53e7a6261b136c610acddd87097dae8f1a6de58
// Full declarations below match the executable registry and normalized TL CRC32.
// Not a complete protocol: unresolved dependency families include User, Chat,
// MessageEntity, MessageMedia except messageMediaEmpty, ReplyMarkup, MessageAction,
// MessageReplyHeader, MessageReplies, MessageReactions, updates.Difference.
// Unknown constructors or optional payloads cannot be skipped safely without a schema.
// Do not equate this recovered declaration set with complete live-message coverage.
---types---
updates.state#a56c2a3e pts:int qts:int date:int seq:int unread_count:int = updates.State;
updates.stateContactsApply#17765890 apply_id:int date:int unread_count:int = updates.State;
updateShortMessage#914fbf11 flags:# out:flags.1?true mentioned:flags.4?true media_unread:flags.5?true silent:flags.13?true id:int user_id:int message:string pts:int pts_count:int date:int fwd_from:flags.2?MessageFwdHeader via_bot_id:flags.11?int reply_to_msg_id:flags.3?int entities:flags.7?Vector<MessageEntity> = Updates;
updateShortChatMessage#16812688 flags:# out:flags.1?true mentioned:flags.4?true media_unread:flags.5?true silent:flags.13?true id:int from_id:int chat_id:int message:string pts:int pts_count:int date:int fwd_from:flags.2?MessageFwdHeader via_bot_id:flags.11?int reply_to_msg_id:flags.3?int entities:flags.7?Vector<MessageEntity> = Updates;
updateNewMessage#1f2b0afd message:Message pts:int pts_count:int = Update;
updateNewChannelMessage#62ba04d9 message:Message pts:int pts_count:int = Update;
updatesCombined#725b04c3 updates:Vector<Update> users:Vector<User> chats:Vector<Chat> date:int seq_start:int seq:int = Updates;
updates#74ae4240 updates:Vector<Update> users:Vector<User> chats:Vector<Chat> date:int seq:int = Updates;
updateShort#78d4dec1 update:Update date:int = Updates;
message#81ff9c4a flags:# out:flags.1?true mentioned:flags.4?true media_unread:flags.5?true silent:flags.13?true post:flags.14?true flags2:# id:int from_id:flags.8?int to_id:Peer fwd_from:flags.2?MessageFwdHeader via_bot_id:flags.11?int reply_to_msg_id:flags.3?int date:int message:string media:flags.9?MessageMedia reply_markup:flags.6?ReplyMarkup entities:flags.7?Vector<MessageEntity> views:flags.10?int edit_date:flags.15?int post_author:flags.16?string grouped_id:flags.17?long award_enabled:flags.18?true reply_to:flags.19?MessageReplyHeader ttl_period:flags2.1?int replies:flags2.2?MessageReplies edit_hide:flags2.3?true reactions:flags2.4?MessageReactions from_peer:flags2.5?Peer = Message;
messageEmpty#83e5de54 id:int = Message;
messageService#8c75c832 flags:# out:flags.1?true mentioned:flags.4?true media_unread:flags.5?true silent:flags.13?true post:flags.14?true flags2:# id:int from_id:flags.8?int to_id:Peer reply_to_msg_id:flags.3?int date:int action:MessageAction ttl_period:flags2.1?int = Message;
messageMediaEmpty#3ded6320 = MessageMedia;
peerUser#9db1bc6d user_id:int = Peer;
peerChat#bad0e5bb chat_id:int = Peer;
peerChannel#bddde532 channel_id:int = Peer;
messageFwdHeader#559ebe6d flags:# from_id:flags.0?int date:int channel_id:flags.1?int channel_post:flags.2?int post_author:flags.3?string saved_from_peer:flags.4?Peer saved_from_msg_id:flags.4?int = MessageFwdHeader;
---functions---
updates.getState#edd4882a = updates.State;
updates.getDifference#25939651 flags:# pts:int pts_total_limit:flags.0?int date:int qts:int = updates.Difference;
