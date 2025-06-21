# Thunder/bot/plugins/stream.py

import asyncio
from typing import Optional, Dict, Any
from pyrogram import Client, filters, enums
from pyrogram.errors import (
    FloodWait,
    MessageNotModified,
    RPCError
)
from pyrogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Message,
    LinkPreviewOptions
)
from Thunder.bot import StreamBot
from Thunder.utils.database import db
from Thunder.utils.messages import *
from Thunder.utils.logger import logger
from Thunder.vars import Var
from Thunder.utils.decorators import check_banned, require_token, shorten_link
from Thunder.utils.force_channel import force_channel_check
from Thunder.utils.bot_utils import (
    notify_own,
    reply_user_err,
    log_newusr,
    gen_links,
    is_admin
)

async def fwd_media(m_msg: Message) -> Optional[Message]:
    try:
        return await m_msg.copy(chat_id=Var.BIN_CHANNEL)
    except FloodWait as e:
        logger.debug(f"FloodWait: fwd_media copy, sleep {e.value}s")
        await asyncio.sleep(e.value + 1)
        try:
            return await m_msg.copy(chat_id=Var.BIN_CHANNEL)
        except Exception as retry_e:
            logger.error(f"Error fwd_media copy on retry after FloodWait: {retry_e}")
            return None
    except RPCError as e:
        if "MEDIA_CAPTION_TOO_LONG" in str(e):
            logger.warning(f"MEDIA_CAPTION_TOO_LONG error, retrying without caption: {e}")
            try:
                return await m_msg.copy(chat_id=Var.BIN_CHANNEL, caption=None)
            except Exception as retry_e:
                logger.error(f"Error fwd_media copy on retry without caption: {retry_e}")
                return None
        else:
            logger.error(f"Error fwd_media copy: {e}")
            return None
    except Exception as e:
        logger.error(f"Error fwd_media copy: {e}")
        return None

def get_link_buttons(links):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(MSG_BUTTON_STREAM_NOW, url=links['stream_link']),
        InlineKeyboardButton(MSG_BUTTON_DOWNLOAD, url=links['online_link'])
    ]])

@StreamBot.on_message(filters.command("link") & ~filters.private)
@check_banned
@require_token
@force_channel_check
@shorten_link
async def link_handler(bot: Client, msg: Message, **kwargs):
    if msg.from_user and not await db.is_user_exist(msg.from_user.id):
        invite_link = f"https://t.me/{bot.me.username}?start=start"
        await msg.reply_text(
            MSG_ERROR_START_BOT.format(invite_link=invite_link),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(MSG_BUTTON_START_CHAT, url=invite_link)
            ]]),
            quote=True
        )
        return

    if msg.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
        if not await is_admin(bot, msg.chat.id):
            await reply_user_err(msg, MSG_ERROR_NOT_ADMIN)
            return

    if not msg.reply_to_message:
        await reply_user_err(msg, MSG_ERROR_REPLY_FILE)
        return

    if not msg.reply_to_message.media:
        await reply_user_err(msg, MSG_ERROR_NO_FILE)
        return

    parts = msg.text.split()
    num_files = 1
    if len(parts) > 1:
        try:
            num_files = int(parts[1])
            if not 1 <= num_files <= Var.MAX_BATCH_FILES:
                await reply_user_err(msg, MSG_ERROR_NUMBER_RANGE.format(max_files=Var.MAX_BATCH_FILES))
                return
        except ValueError:
            await reply_user_err(msg, MSG_ERROR_INVALID_NUMBER)
            return

    status_msg = await msg.reply_text(MSG_PROCESSING_REQUEST, quote=True)
    shortener_val = kwargs.get('shortener', Var.SHORTEN_MEDIA_LINKS)
    if num_files == 1:
        await process_single(bot, msg, msg.reply_to_message, status_msg, shortener_val)
    else:
        await process_batch(bot, msg, msg.reply_to_message.id, num_files, status_msg, shortener_val)

@StreamBot.on_message(
    filters.private &
    filters.incoming &
    (filters.document | filters.video | filters.photo | filters.audio |
     filters.voice | filters.animation | filters.video_note),
    group=4
)
@check_banned
@require_token
@force_channel_check
@shorten_link
async def private_receive_handler(bot: Client, msg: Message, **kwargs):
    if not msg.from_user:
        return
    await log_newusr(bot, msg.from_user.id, msg.from_user.first_name or "")
    status_msg = await msg.reply_text(MSG_PROCESSING_FILE, quote=True)
    shortener_val = kwargs.get('shortener', Var.SHORTEN_MEDIA_LINKS)
    await process_single(bot, msg, msg, status_msg, shortener_val)

@StreamBot.on_message(
    filters.channel &
    filters.incoming &
    (filters.document | filters.video | filters.audio) &
    ~filters.chat(Var.BIN_CHANNEL),
    group=-1
)
async def channel_receive_handler(bot: Client, msg: Message):
    if hasattr(Var, 'BANNED_CHANNELS') and msg.chat.id in Var.BANNED_CHANNELS:
        try:
            await bot.leave_chat(msg.chat.id)
        except Exception as e:
            logger.error(f"Error leaving banned channel {msg.chat.id}: {e}")
            pass
        return

    if not await is_admin(bot, msg.chat.id):
        logger.debug(f"Bot is not admin in channel {msg.chat.id} ({msg.chat.title or 'Unknown'}). Ignoring message.")
        return

    try:
        stored_msg = await fwd_media(msg)
        if not stored_msg:
            logger.error(f"Failed to forward media from channel {msg.chat.id}. Ignoring.")
            return

        links = await gen_links(stored_msg, shortener=Var.SHORTEN_MEDIA_LINKS)

        source_info = msg.chat.title or "Unknown Channel"
        await stored_msg.reply_text(
            MSG_NEW_FILE_REQUEST.format(
                source_info=source_info,
                id_=msg.chat.id,
                online_link=links['online_link'],
                stream_link=links['stream_link']
            ),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            quote=True
        )

        try:
            await msg.edit_reply_markup(reply_markup=get_link_buttons(links))
        except (MessageNotModified, Exception) as edit_e:
            await msg.reply_text(
                MSG_LINKS.format(
                    file_name=links['media_name'],
                    file_size=links['media_size'],
                    download_link=links['online_link'],
                    stream_link=links['stream_link']
                ),
                quote=True,
                parse_mode=enums.ParseMode.MARKDOWN,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                reply_markup=get_link_buttons(links)
            )

    except Exception as e:
        logger.error(f"Error in channel_receive_handler: {e}")
        pass

async def process_single(bot: Client, msg: Message, file_msg: Message, status_msg: Message, shortener_val: bool):
    try:
        stored_msg = await fwd_media(file_msg)
        links = await gen_links(stored_msg, shortener=shortener_val)

        await msg.reply_text(
            MSG_LINKS.format(
                file_name=links['media_name'],
                file_size=links['media_size'],
                download_link=links['online_link'],
                stream_link=links['stream_link']
            ),
            quote=True,
            parse_mode=enums.ParseMode.MARKDOWN,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            reply_markup=get_link_buttons(links)
        )

        if msg.from_user:
            source_info = f"{msg.from_user.first_name or ''} {msg.from_user.last_name or ''}".strip()
            if not source_info:
                source_info = f"@{msg.from_user.username}" if msg.from_user.username else "Unknown User"
            await stored_msg.reply_text(
                MSG_NEW_FILE_REQUEST.format(
                    source_info=source_info,
                    id_=msg.from_user.id,
                    online_link=links['online_link'],
                    stream_link=links['stream_link']
                ),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                quote=True
            )

        await status_msg.delete()
    except Exception as e:
        await status_msg.edit_text(MSG_ERROR_PROCESSING_MEDIA)
        if str(e):
            await notify_own(bot, MSG_CRITICAL_ERROR.format(
                error=str(e),
                error_id=str(id(e))[:8]
            ))

async def process_batch(bot: Client, msg: Message, start_id: int, count: int, status_msg: Message, shortener_val: bool):
    processed = 0
    failed = 0
    links_list = []

    for batch_start in range(0, count, 10):
        batch_size = min(10, count - batch_start)
        batch_ids = list(range(start_id + batch_start, start_id + batch_start + batch_size))

        try:
            await status_msg.edit_text(
                MSG_PROCESSING_BATCH.format(
                    batch_number=(batch_start // 10) + 1,
                    total_batches=(count + 9) // 10,
                    file_count=batch_size
                )
            )
        except MessageNotModified:
            pass

        try:
            messages = await bot.get_messages(msg.chat.id, batch_ids)
        except FloodWait as e:
            logger.warning(f"FloodWait: process_batch get_messages, sleep {e.value}s")
            await asyncio.sleep(e.value + 1)
            messages = await bot.get_messages(msg.chat.id, batch_ids)
        except Exception as e:
            logger.error(f"Error getting messages in batch: {e}")
            messages = []

        for m in messages:
            if m and m.media:
                try:
                    stored_msg = await fwd_media(m)
                    links = await gen_links(stored_msg, shortener=shortener_val)
                    links_list.append(links['online_link'])
                    processed += 1

                    if msg.from_user:
                        source_info = f"{msg.from_user.first_name or ''} {msg.from_user.last_name or ''}".strip()
                        if not source_info:
                            source_info = f"@{msg.from_user.username}" if msg.from_user.username else "Unknown User"
                        await stored_msg.reply_text(
                            MSG_NEW_FILE_REQUEST.format(
                                source_info=source_info,
                                id_=msg.from_user.id,
                                online_link=links['online_link'],
                                stream_link=links['stream_link']
                            ),
                            link_preview_options=LinkPreviewOptions(is_disabled=True),
                            quote=True
                        )

                except Exception as e:
                    logger.error(f"Error processing message in batch: {e}")
                    failed += 1
            else:
                failed += 1

        if (processed + failed) % 5 == 0 or (processed + failed) == count:
            try:
                await status_msg.edit_text(
                    MSG_PROCESSING_STATUS.format(
                        processed=processed,
                        total=count,
                        failed=failed
                    )
                )
            except MessageNotModified:
                pass

    for i in range(0, len(links_list), 20):
        chunk = links_list[i:i+20]
        chunk_text = MSG_BATCH_LINKS_READY.format(count=len(chunk)) + f"\n\n`{chr(10).join(chunk)}`"
        await msg.reply_text(
            chunk_text,
            quote=True,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            parse_mode=enums.ParseMode.MARKDOWN
        )

        if msg.chat.type != enums.ChatType.PRIVATE and msg.from_user:
            try:
                await bot.send_message(
                    chat_id=msg.from_user.id,
                    text=MSG_DM_BATCH_PREFIX.format(chat_title=msg.chat.title or "the chat") + "\n" + chunk_text,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    parse_mode=enums.ParseMode.MARKDOWN
                )
            except Exception as e:
                logger.error(f"Error sending DM in batch: {e}")
                await reply_user_err(msg, MSG_ERROR_DM_FAILED)

        if i + 20 < len(links_list):
            await asyncio.sleep(0.3)

    await status_msg.edit_text(
        MSG_PROCESSING_RESULT.format(
            processed=processed,
            total=count,
            failed=failed
        )
    )
