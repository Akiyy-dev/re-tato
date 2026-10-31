// Public Tatotalk APK layer 123, exact static constructors and DEX readers.
// SHA256 3769b604e4b7f66c9a44e00e9cab6e60d7723f4b6d2c357c396ae4bf198a54c4.
// Local descriptive aliases do not imply CRC-derived names.
// FileLocation and GeoPoint are supplied by user-chat-schema.tl.
// Page is deliberately external: full instant-view block families were not expanded.
// Existing empty Photo/Document/WebPage declarations are repeated identically for family coverage.

---types---

photoEmpty#2331b22d id:long = Photo;
photo#9288dd29 flags:# has_stickers:flags.0?true id:long access_hash:long date:int sizes:Vector<PhotoSize> = Photo;
photoLegacyCded42fe#cded42fe id:long access_hash:long date:int sizes:Vector<PhotoSize> = Photo;
photoLegacyC3838076#c3838076 id:long access_hash:long user_id:int date:int geo:GeoPoint sizes:Vector<PhotoSize> = Photo;
photoLegacy22b56751#22b56751 id:long access_hash:long user_id:int date:int caption:string geo:GeoPoint sizes:Vector<PhotoSize> = Photo;

photoSizeEmpty#0e17e23c type:string = PhotoSize;
photoSize#77bfb61b type:string location:FileLocation w:int h:int size:int = PhotoSize;
photoCachedSize#e9a734fa type:string location:FileLocation w:int h:int bytes:bytes = PhotoSize;

documentEmpty#36f8c871 id:long = Document;
document#87232bc7 id:long access_hash:long date:int mime_type:string size:int thumb:PhotoSize dc_id:int version:int attributes:Vector<DocumentAttribute> = Document;
documentLegacyF9a39f4f#f9a39f4f id:long access_hash:long date:int mime_type:string size:int thumb:PhotoSize dc_id:int attributes:Vector<DocumentAttribute> = Document;
documentLegacy9efc6326#9efc6326 id:long access_hash:long user_id:int date:int file_name:string mime_type:string size:int thumb:PhotoSize dc_id:int = Document;
documentEncrypted55555558#55555558 id:long access_hash:long date:int mime_type:string size:int thumb:PhotoSize dc_id:int attributes:Vector<DocumentAttribute> key:bytes iv:bytes = Document;
documentEncrypted55555556#55555556 id:long access_hash:long user_id:int date:int file_name:string mime_type:string size:int thumb:PhotoSize dc_id:int key:bytes iv:bytes = Document;

documentAttributeImageSize#6c37c15c w:int h:int = DocumentAttribute;
documentAttributeAnimated#11b58939 = DocumentAttribute;
documentAttributeAudio#9852f9c6 flags:# voice:flags.10?true duration:int title:flags.0?string performer:flags.1?string waveform:flags.2?bytes = DocumentAttribute;
documentAttributeAudioLegacyDed218e0#ded218e0 duration:int title:string performer:string = DocumentAttribute;
documentAttributeAudioLegacy051448e5#051448e5 duration:int = DocumentAttribute;
documentAttributeFilename#15590068 file_name:string = DocumentAttribute;
documentAttributeSticker#6319d612 flags:# mask:flags.1?true alt:string stickerset:InputStickerSet mask_coords:flags.0?MaskCoords = DocumentAttribute;
documentAttributeStickerLegacy3a556302#3a556302 alt:string stickerset:InputStickerSet = DocumentAttribute;
documentAttributeStickerLegacy994c9882#994c9882 alt:string = DocumentAttribute;
documentAttributeEmptyFb0a5727#fb0a5727 = DocumentAttribute;
documentAttributeVideo#0ef02ce6 flags:# round_message:flags.0?true nosound:flags.3?true duration:int w:int h:int = DocumentAttribute;
documentAttributeVideoLegacy5910cccb#5910cccb duration:int w:int h:int = DocumentAttribute;
documentAttributeEmptyC25cc83d#c25cc83d = DocumentAttribute;
documentAttributeHasStickers#9801d2f7 = DocumentAttribute;

inputStickerSetEmpty#ffb62b95 = InputStickerSet;
inputStickerSetID#9de7a269 id:long access_hash:long = InputStickerSet;
inputStickerSetShortName#861cc8a0 short_name:string = InputStickerSet;
inputStickerSetShortNameLegacy689bcdc1#689bcdc1 short_name:string = InputStickerSet;
inputStickerSetEmoticonE67f520e#e67f520e emoticon:string = InputStickerSet;
maskCoords#aed6dbb2 n:int x:double y:double zoom:double = MaskCoords;

webPageEmpty#eb1477e8 id:long = WebPage;
webPagePending#c586da1c id:long date:int = WebPage;
webPageNotModified#85849473 = WebPage;
webPageUrlD41a5167#d41a5167 url:string = WebPage;
webPageLegacyA31ea0b5#a31ea0b5 flags:# id:long url:string display_url:string type:flags.0?string site_name:flags.1?string title:flags.2?string description:flags.3?string photo:flags.4?Photo embed_url:flags.5?string embed_type:flags.5?string embed_width:flags.6?int embed_height:flags.6?int duration:flags.7?int author:flags.8?string = WebPage;
webPageLegacyCa820ed7#ca820ed7 flags:# id:long url:string display_url:string type:flags.0?string site_name:flags.1?string title:flags.2?string description:flags.3?string photo:flags.4?Photo embed_url:flags.5?string embed_type:flags.5?string embed_width:flags.6?int embed_height:flags.6?int duration:flags.7?int author:flags.8?string document:flags.9?Document = WebPage;
webPage#5f07b4bc flags:# id:long url:string display_url:string hash:int type:flags.0?string site_name:flags.1?string title:flags.2?string description:flags.3?string photo:flags.4?Photo embed_url:flags.5?string embed_type:flags.5?string embed_width:flags.6?int embed_height:flags.6?int duration:flags.7?int author:flags.8?string document:flags.9?Document cached_page:flags.10?Page = WebPage;
