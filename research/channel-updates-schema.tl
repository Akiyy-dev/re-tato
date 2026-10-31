// Public Tatotalk Android APK, layer 123; source hash and reader evidence in companion note.
// Names are descriptive aliases. All IDs are accepted by the actual Update dispatcher.
// ChannelParticipant's 13 accepted variants are defined in dialog-schema.tl.
// The two ChannelRead names intentionally do not infer inbox/outbox direction from wire alone.

updateChannelParticipants#a8e68187 channel_id:int participants:Vector<ChannelParticipant> = Update;
updateChannel#b6d45656 channel_id:int = Update;
updateChannelTooLong#eb0467fb flags:# channel_id:int pts:flags.0?int = Update;
updateChannelRead4214f37f#4214f37f channel_id:int max_id:int = Update;
updateChannelRead25d6c9c7#25d6c9c7 channel_id:int max_id:int = Update;
updateChannelMessageViews#98a12b4b channel_id:int id:int views:int = Update;
