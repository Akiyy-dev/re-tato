// Bounded DEX-confirmed Tatotalk layer 123 User/Chat protocol families.
// Evidence: user-schema-evidence.md and chat-schema-evidence.md.
// Document is an external dependency of custom UserStatus28782e2e only.

// Public Tatotalk APK layer 123. IDs and wire types verified against DEX readers.
// Names suffixed with constructor IDs are local descriptive aliases; do not recompute CRCs.
// int = signed int32, long = int64. true fields consume zero bytes.
// FileLocation is supplied by chat-schema-fragment.tl. Document is an external dependency.

user#4e528244 flags:# self:flags.10?true contact:flags.11?true mutual_contact:flags.12?true deleted:flags.13?true bot:flags.14?true bot_chat_history:flags.15?true bot_nochats:flags.16?true verified:flags.17?true restricted:flags.18?true min:flags.20?true bot_inline_geo:flags.21?true support:flags.23?true scam:flags.24?true apply_min_photo:flags.25?true fake:flags.26?true bot_attach_menu:flags.27?true premium:flags.28?true attach_menu_enabled:flags.29?true flags2:# bot_can_edit:flags2.1?true close_friend:flags2.2?true stories_hidden:flags2.3?true stories_unavailable:flags2.4?true star:flags2.8?true hasAlias:flags2.11?true showPhoto:flags2.12?true groupShowPhoto:flags2.13?true id:int access_hash:flags.0?long first_name:flags.1?string last_name:flags.2?string username:flags.3?string phone:flags.4?string photo:flags.5?UserProfilePhoto status:flags.6?UserStatus bot_info_version:flags.14?int restriction_reason:flags.18?string bot_inline_placeholder:flags.19?string lang_code:flags.22?string emoji_status:flags.30?EmojiStatus usernames:flags2.0?Vector<Username> stories_max_id:flags2.5?int color:flags2.7?int background_emoji_id:flags2.6?long email:flags2.16?string = User;
userLegacyBcb626c1#bcb626c1 flags:# self:flags.10?true contact:flags.11?true mutual_contact:flags.12?true deleted:flags.13?true bot:flags.14?true bot_chat_history:flags.15?true bot_nochats:flags.16?true verified:flags.17?true restricted:flags.18?true min:flags.20?true bot_inline_geo:flags.21?true support:flags.23?true scam:flags.24?true apply_min_photo:flags.25?true fake:flags.26?true bot_attach_menu:flags.27?true premium:flags.28?true attach_menu_enabled:flags.29?true flags2:# bot_can_edit:flags2.1?true close_friend:flags2.2?true stories_hidden:flags2.3?true stories_unavailable:flags2.4?true star:flags2.8?true hasAlias:flags2.11?true showPhoto:flags2.12?true groupShowPhoto:flags2.13?true id:int access_hash:flags.0?long first_name:flags.1?string last_name:flags.2?string username:flags.3?string phone:flags.4?string photo:flags.5?UserProfilePhoto status:flags.6?UserStatus bot_info_version:flags.14?int restriction_reason:flags.18?string bot_inline_placeholder:flags.19?string lang_code:flags.22?string emoji_status:flags.30?EmojiStatus usernames:flags2.0?Vector<Username> stories_max_id:flags2.5?int color:flags2.7?int background_emoji_id:flags2.6?long = User;

// Legacy User variants: full serialized fields; zero-byte derived booleans omitted here.
userLegacyD10d979a#d10d979a flags:# id:int access_hash:flags.0?long first_name:flags.1?string last_name:flags.2?string username:flags.3?string phone:flags.4?string photo:flags.5?UserProfilePhoto status:flags.6?UserStatus bot_info_version:flags.14?int restriction_reason:flags.18?string bot_inline_placeholder:flags.19?string = User;
userLegacy22e49072#22e49072 flags:# id:int access_hash:flags.0?long first_name:flags.1?string last_name:flags.2?string username:flags.3?string phone:flags.4?string photo:flags.5?UserProfilePhoto status:flags.6?UserStatus bot_info_version:flags.14?int = User;
userLegacy2e13f4c3#2e13f4c3 flags:# id:int access_hash:flags.0?long first_name:flags.1?string last_name:flags.2?string username:flags.3?string phone:flags.4?string photo:flags.5?UserProfilePhoto status:flags.6?UserStatus bot_info_version:flags.14?int restriction_reason:flags.18?string bot_inline_placeholder:flags.19?string lang_code:flags.22?string = User;
userLegacyB29ad7cc#b29ad7cc id:int first_name:string last_name:string = User;
userLegacyCab35e18#cab35e18 id:int first_name:string last_name:string username:string access_hash:long phone:string photo:UserProfilePhoto status:UserStatus = User;
userLegacyD6016d7a#d6016d7a id:int first_name:string last_name:string username:string = User;
userLegacyD9ccc4ef#d9ccc4ef id:int first_name:string last_name:string username:string access_hash:long phone:string photo:UserProfilePhoto status:UserStatus = User;
userLegacyF2fb8319#f2fb8319 id:int first_name:string last_name:string access_hash:long phone:string photo:UserProfilePhoto status:UserStatus = User;
userLegacy075cf7a8#075cf7a8 id:int first_name:string last_name:string username:string access_hash:long photo:UserProfilePhoto status:UserStatus = User;
userLegacy1c60e608#1c60e608 id:int first_name:string last_name:string username:string phone:string photo:UserProfilePhoto status:UserStatus = User;
userEmpty#200250ba id:int = User;
userLegacy22e8ceb0#22e8ceb0 id:int first_name:string last_name:string access_hash:long phone:string photo:UserProfilePhoto status:UserStatus = User;
userLegacy5214c89d#5214c89d id:int first_name:string last_name:string access_hash:long photo:UserProfilePhoto status:UserStatus = User;
userLegacy7007b451#7007b451 id:int first_name:string last_name:string username:string phone:string photo:UserProfilePhoto status:UserStatus inactive:Bool = User;
userLegacy720535ec#720535ec id:int first_name:string last_name:string phone:string photo:UserProfilePhoto status:UserStatus inactive:Bool = User;

userProfilePhotoEmpty#4f11bae1 = UserProfilePhoto;
userProfilePhoto#d559d8c8 photo_id:long photo_small:FileLocation photo_big:FileLocation = UserProfilePhoto;
userProfilePhotoLegacy990d1493#990d1493 photo_small:FileLocation photo_big:FileLocation = UserProfilePhoto;

username#b4073647 flags:# editable:flags.0?true active:flags.1?true username:string = Username;
emojiStatusEmpty#2de11aae = EmojiStatus;
emojiStatus#929b619d document_id:long = EmojiStatus;
emojiStatusUntil#fa30a8c7 document_id:long until:int = EmojiStatus;

// UserStatus aliases avoid guessing semantic names from obfuscated class names.
userStatusEmpty09d05049#09d05049 = UserStatus;
userStatusEmpty77ebc742#77ebc742 = UserStatus;
userStatusEmpty07bf09fc#07bf09fc = UserStatus;
userStatusEmptyE26f42f1#e26f42f1 = UserStatus;
userStatusEmpty81fece3a#81fece3a = UserStatus;
userStatusExpires008c703f#008c703f expires:int = UserStatus;
userStatusExpiresEdb93949#edb93949 expires:int = UserStatus;
userStatusCustom8929453d#8929453d flags:# id:int emoji:string title:string duration:long onlineStatus:flags.0?UserStatus = UserStatus;
userStatusCustom28782e2e#28782e2e flags:# id:int emoji:string title:string duration:long onlineStatus:flags.0?UserStatus sticker_doc:flags.1?InputDocument sticker_doc_desc:flags.2?Document = UserStatus;

inputDocumentEmpty#72f0eaae = InputDocument;
inputDocument#18798952 id:long access_hash:long = InputDocument;

// Extracted only from the known public Tatotalk Android APK, layer 123.
// Source SHA256 3769b604e4b7f66c9a44e00e9cab6e60d7723f4b6d2c357c396ae4bf198a54c4.
// Constructor IDs are explicit observed values; local legacy names are descriptive aliases.
// flags.N?true is a zero-byte bit property, NOT a serialized Bool constructor.
// Every id/channel_id below is int32. See chat-schema-evidence.md for reader offsets.

chatEmpty#9ba2d800 id:int = Chat;
chatForbidden#07328bdb id:int title:string = Chat;
chatForbiddenWithDate#fb0ccc41 id:int title:string date:int = Chat;

chat#b7e37196 flags:# creator:flags.0?true kicked:flags.1?true left:flags.2?true admins_enabled:flags.3?true admin:flags.4?true deactivated:flags.5?true restricted:flags.9?true id:int title:string photo:ChatPhoto participants_count:int date:int version:int migrated_to:flags.6?InputChannel restriction_reason:flags.9?string = Chat;
chatLegacyD91cdd54#d91cdd54 flags:# creator:flags.0?true kicked:flags.1?true left:flags.2?true admins_enabled:flags.3?true admin:flags.4?true deactivated:flags.5?true id:int title:string photo:ChatPhoto participants_count:int date:int version:int migrated_to:flags.6?InputChannel = Chat;
chatLegacy7312bc48#7312bc48 flags:# creator:flags.0?true kicked:flags.1?true left:flags.2?true admins_enabled:flags.3?true admin:flags.4?true deactivated:flags.5?true id:int title:string photo:ChatPhoto participants_count:int date:int version:int = Chat;
chatLegacy6e9c9bc7#6e9c9bc7 id:int title:string photo:ChatPhoto participants_count:int date:int left:Bool version:int = Chat;

channel#450b7115 flags:# creator:flags.0?true kicked:flags.1?true left:flags.2?true game:flags.23?true broadcast:flags.5?true verified:flags.7?true megagroup:flags.8?true restricted:flags.9?true democracy:flags.10?true signatures:flags.11?true has_link:flags.20?true join_to_send:flags.28?true min:flags.12?true allBanned:flags.30?true isPrivateChat:flags.29?true anonymous:flags.24?true guarantee:flags.21?true id:int access_hash:flags.13?long title:string username:flags.6?string photo:ChatPhoto date:int version:int restriction_reason:flags.9?string admin_rights:flags.14?ChatAdminRights banned_rights:flags.15?ChatBannedRights participants_count:flags.17?int = Chat;
channelLegacy0cb44b1c#0cb44b1c flags:# creator:flags.0?true left:flags.2?true broadcast:flags.5?true verified:flags.7?true megagroup:flags.8?true restricted:flags.9?true democracy:flags.10?true signatures:flags.11?true min:flags.12?true id:int access_hash:flags.13?long title:string username:flags.6?string photo:ChatPhoto date:int version:int restriction_reason:flags.9?string admin_rights:flags.14?ChatAdminRights banned_rights:flags.15?ChatBannedRights = Chat;
channelLegacyA14dca52#a14dca52 flags:# creator:flags.0?true kicked:flags.1?true left:flags.2?true moderator:flags.4?true broadcast:flags.5?true verified:flags.7?true megagroup:flags.8?true restricted:flags.9?true democracy:flags.10?true signatures:flags.11?true min:flags.12?true id:int access_hash:flags.13?long title:string username:flags.6?string photo:ChatPhoto date:int version:int restriction_reason:flags.9?string = Chat;
channelLegacy4b1b7506#4b1b7506 flags:# creator:flags.0?true kicked:flags.1?true left:flags.2?true moderator:flags.4?true broadcast:flags.5?true verified:flags.7?true megagroup:flags.8?true restricted:flags.9?true democracy:flags.10?true signatures:flags.11?true id:int access_hash:long title:string username:flags.6?string photo:ChatPhoto date:int version:int restriction_reason:flags.9?string = Chat;
channelLegacy678e9587#678e9587 flags:# creator:flags.0?true kicked:flags.1?true left:flags.2?true moderator:flags.4?true broadcast:flags.5?true verified:flags.7?true megagroup:flags.8?true explicit_content:flags.9?true id:int access_hash:long title:string username:flags.6?string photo:ChatPhoto date:int version:int = Chat;

channelForbidden#289da732 flags:# broadcast:flags.5?true megagroup:flags.8?true id:int access_hash:long title:string until_date:flags.16?int = Chat;
channelForbiddenLegacy8537784f#8537784f flags:# broadcast:flags.5?true megagroup:flags.8?true id:int access_hash:long title:string = Chat;
channelForbiddenLegacy2d85832c#2d85832c id:int access_hash:long title:string = Chat;
geoChat#75eaea5a id:int access_hash:long title:string address:string venue:string geo:GeoPoint photo:ChatPhoto participants_count:int date:int checked_in:Bool version:int = Chat;

chatPhotoEmpty#37c1011c = ChatPhoto;
chatPhoto#6153276a photo_small:FileLocation photo_big:FileLocation = ChatPhoto;
chatPhotoUsers#2e717ec3 users:Vector<UserPhoto> = ChatPhoto;
userPhoto#5fbbf792 user_id:int first_name:string last_name:string photo:UserProfilePhoto = UserPhoto;

inputChannelEmpty#ee8c1e86 = InputChannel;
inputChannel#afeb712e channel_id:int access_hash:long = InputChannel;

channelAdminRights#5d7ceba5 flags:# change_info:flags.0?true post_messages:flags.1?true edit_messages:flags.2?true delete_messages:flags.3?true ban_users:flags.4?true invite_users:flags.5?true invite_link:flags.6?true pin_messages:flags.7?true add_admins:flags.9?true = ChatAdminRights;
channelBannedRights#58cf4249 flags:# view_messages:flags.0?true send_messages:flags.1?true send_media:flags.2?true send_stickers:flags.3?true send_gifs:flags.4?true send_games:flags.5?true send_inline:flags.6?true embed_links:flags.7?true invite_user:flags.8?true until_date:int = ChatBannedRights;

fileLocation#44071204 flags:# canCdn:flags.0?true dc_id:int volume_id:long local_id:int secret:long = FileLocation;
fileLocationLegacy53d69076#53d69076 dc_id:int volume_id:long local_id:int secret:long = FileLocation;
fileLocationUnavailable#7c596b46 volume_id:long local_id:int secret:long = FileLocation;
fileLocationWithoutSecret#bc7fc6cd volume_id:long local_id:int = FileLocation;
fileEncryptedLocation#55555554 dc_id:int volume_id:long local_id:int secret:long key:bytes iv:bytes = FileLocation;

geoPointEmpty#1117dd5f = GeoPoint;
geoPoint#2049d70c long:double lat:double = GeoPoint;
