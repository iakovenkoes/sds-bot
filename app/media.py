"""Telegram attachment classification for participant task drafts."""


def classify_task_part(message):
    """Return content type, optional text, reusable Telegram file_id."""
    if message.text:
        return 'text', message.text, None
    if message.photo:
        return 'photo', message.caption, message.photo[-1].file_id
    if message.video:
        return 'video', message.caption, message.video.file_id
    if message.video_note:
        return 'video_note', None, message.video_note.file_id
    if message.document:
        return 'document', message.caption, message.document.file_id
    return None, None, None


async def forward_task_part(bot, chat_id, kind, text, file_id, **kwargs):
    if kind == 'text':
        await bot.send_message(chat_id, (text or '')[:4000], **kwargs)
    elif kind == 'photo':
        await bot.send_photo(chat_id, file_id, caption=(text or '')[:1024] or None, **kwargs)
    elif kind == 'video':
        await bot.send_video(chat_id, file_id, caption=(text or '')[:1024] or None, **kwargs)
    elif kind == 'video_note':
        await bot.send_video_note(chat_id, video_note=file_id, **kwargs)
    elif kind == 'document':
        await bot.send_document(chat_id, file_id, caption=(text or '')[:1024] or None, **kwargs)
