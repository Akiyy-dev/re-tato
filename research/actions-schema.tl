// Public Tatotalk Android APK, layer 123. All 38 MessageAction dispatcher IDs are explicit.
// APK SHA256 3769b604e4b7f66c9a44e00e9cab6e60d7723f4b6d2c357c396ae4bf198a54c4.
// Names are descriptive aliases; constructor values and wire fields were directly verified.
// External established families: Photo, UserProfilePhoto, Peer.
// One unresolved external family: DecryptedMessageAction (only action #555555f7 uses it).
// DataJSON may duplicate an existing common definition; merge by explicit constructor ID.

messageActionEmpty#b6aef7b0 = MessageAction;
messageActionChannelCreate#95d2ac92 title:string = MessageAction;
messageActionChannelMigrateFrom#b055eaee title:string chat_id:int = MessageAction;
messageActionChatAddUser#488a7337 users:Vector<int> = MessageAction;
messageActionChatAddUserLegacy5e3cfc4b#5e3cfc4b user_id:int = MessageAction;
messageActionChatCreate#a6638b9a title:string users:Vector<int> = MessageAction;
messageActionChatDeletePhoto#95e3fbef = MessageAction;
messageActionChatDeleteUser#b2ae9b0c user_id:int = MessageAction;
messageActionChatEditPhoto#7fcb13a8 photo:Photo = MessageAction;
messageActionChatEditTitle#b5a1ce5a title:string = MessageAction;
messageActionChatJoinedByLink#f89cf5e8 inviter_id:int = MessageAction;
messageActionInviter20b8d9b3#20b8d9b3 inviter_id:int = MessageAction;
messageActionChatMigrateTo#51bdb021 channel_id:int = MessageAction;
messageActionCreatedBroadcastList#55555557 = MessageAction;
messageActionCustomAction#fae69f56 message:string = MessageAction;
messageActionGameScore#92a72876 game_id:long score:int = MessageAction;
messageActionGeoChatCheckin#0c7d53de = MessageAction;
messageActionGeoChatCreate#6f038ebc title:string address:string = MessageAction;
messageActionGroupCall#7a0d7f42 flags:# call:InputGroupCall duration:flags.0?int = MessageAction;
messageActionHistoryClear#9fbab604 = MessageAction;
messageActionInviteToGroupCall#502f92f7 call:InputGroupCall users:Vector<long> = MessageAction;
messageActionLoginUnknownLocation#555555f5 title:string address:string = MessageAction;
messageActionPaymentSent#40699cd0 currency:string total_amount:long = MessageAction;
messageActionPhoneCall#80e11a7f flags:# call_id:long reason:flags.0?PhoneCallDiscardReason duration:flags.1?int video_call:flags.2?true = MessageAction;
messageActionPinMessage#94bd38ed = MessageAction;
messageActionScreenshotTaken#4792929b = MessageAction;
messageActionSetMessagesTTL#3c134d7b flags:# period:int auto_setting_from:flags.0?long = MessageAction;
messageActionTTLChange#55555552 ttl:int = MessageAction;
messageActionUserJoined#55555550 = MessageAction;
messageActionUserUpdatedPhoto#55555551 newUserPhoto:UserProfilePhoto = MessageAction;
messageActionEncryptedAction#555555f7 encryptedAction:DecryptedMessageAction = MessageAction;

messageActionCustomF587627d#f587627d name:string country:string city:string address:string lat:double long:double = MessageAction;
messageActionCustom623f5d57#623f5d57 tagCode:Vector<int> = MessageAction;
messageActionCustomC559c2b8#c559c2b8 contact_id:int = MessageAction;
messageActionCustom90356d2e#90356d2e receiver:Peer sender:Peer data:DataJSON = MessageAction;
messageActionCustom0d15eaeb#0d15eaeb flags:# type:flags.0?int receiver:Peer sender:Peer data:DataJSON = MessageAction;
messageActionCustomD1edec91#d1edec91 owner:int to_user:int = MessageAction;
messageActionCustom0fe9ee8d#0fe9ee8d from_id:int transfer:DataJSON = MessageAction;

inputGroupCall#d8aa840f id:long access_hash:long = InputGroupCall;
phoneCallDiscardReasonAfe2b839#afe2b839 encrypted_key:bytes = PhoneCallDiscardReason;
phoneCallDiscardReasonFaf7e8c9#faf7e8c9 = PhoneCallDiscardReason;
phoneCallDiscardReasonBef60650#bef60650 = PhoneCallDiscardReason;
phoneCallDiscardReasonA9ebe30b#a9ebe30b = PhoneCallDiscardReason;
phoneCallDiscardReasonE095c1a0#e095c1a0 = PhoneCallDiscardReason;
phoneCallDiscardReason57adc690#57adc690 = PhoneCallDiscardReason;
phoneCallDiscardReason85e42301#85e42301 = PhoneCallDiscardReason;
dataJSON#7d748d04 data:string = DataJSON;
