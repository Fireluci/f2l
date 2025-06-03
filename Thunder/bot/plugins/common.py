# Thunder/bot/plugins/common.py

import time
import asyncio
import uuid
from datetime import datetime, timedelta
from typing import Tuple, Optional
from urllib.parse import quote_plus
from pyrogram import filters, StopPropagation
from pyrogram.client import Client
from pyrogram.errors import RPCError, MessageNotModified
from pyrogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
    CallbackQuery,
    LinkPreviewOptions
)
from Thunder.bot import StreamBot
from Thunder.vars import Var
from Thunder.utils.database import db
from Thunder.utils.messages import *
from Thunder.utils.human_readable import humanbytes
from Thunder.utils.file_properties import get_media_file_size, get_name
from Thunder.utils.force_channel import force_channel_check
from Thunder.utils.logger import logger
from Thunder.utils.tokens import (
    check,
    get_pending_token_data,
    delete_pending_token,
)
from Thunder.utils.decorators import check_banned
from Thunder.utils.bot_utils import (
    notify_channel,
    log_new_user,
    generate_media_links,
    handle_user_error,
    generate_dc_text,
    get_user_safely
)

def has_media(message):
    return any(
        hasattr(message, attr) and getattr(message, attr)
        for attr in ["document", "photo", "video", "audio", "voice",
                    "sticker", "animation", "video_note"]
    )

@check_banned
@StreamBot.on_message(filters.command("start") & filters.private)
async def start_command(bot: Client, message: Message):
    user_id = message.from_user.id if message.from_user else None
    user_id_str = str(user_id) if user_id else 'unknown'

    try:
        if message.from_user:
            await log_new_user(bot, user_id, message.from_user.first_name)

        if len(message.command) == 2:
            payload = message.command[1]

            pending_token_doc = await get_pending_token_data(payload)
            if pending_token_doc:
                if pending_token_doc["user_id"] == user_id:
                    main_token_expiry = datetime.utcnow() + timedelta(hours=getattr(Var, "TOKEN_TTL_HOURS", 24))

                    await db.save_main_token(
                        user_id,
                        pending_token_doc["token"],
                        main_token_expiry,
                        pending_token_doc["created_at"]
                    )
                    await delete_pending_token(pending_token_doc["token"])

                    expiry_date_str = main_token_expiry.strftime("%Y-%m-%d %H:%M:%S UTC")
                    await message.reply_text(
                        MSG_TOKEN_ACTIVATED.format(
                            expiry_date=expiry_date_str,
                            description="Access token"
                        ),
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                        quote=True
                    )
                    return
                else:
                    logger.warning(f"User {user_id} tried to use pending token {payload} belonging to user {pending_token_doc.get('user_id')}")
                    await message.reply_text(
                        MSG_TOKEN_FAILED.format(reason="Token not intended for your account.", error_id=uuid.uuid4().hex[:8]),
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                        quote=True
                    )
                    return

            try:
                msg_id = int(payload)
                retrieved_messages = await bot.get_messages(chat_id=Var.BIN_CHANNEL, message_ids=msg_id)
                if not retrieved_messages:
                    await handle_user_error(message, MSG_ERROR_FILE_INVALID)
                    return

                file_msg = retrieved_messages[0] if isinstance(retrieved_messages, list) else retrieved_messages
                if not file_msg:
                    await handle_user_error(message, MSG_ERROR_FILE_INVALID)
                    return

                stream_link, download_link, file_name, file_size = await generate_media_links(file_msg)
                reply_text = MSG_LINKS.format(
                    file_name=file_name,
                    file_size=file_size,
                    download_link=download_link,
                    stream_link=stream_link
                )

                await message.reply_text(
                    text=reply_text,
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(MSG_BUTTON_STREAM_NOW, url=stream_link),
                            InlineKeyboardButton(MSG_BUTTON_DOWNLOAD, url=download_link)
                        ]
                    ]),
                    quote=True,
                    link_preview_options=LinkPreviewOptions(is_disabled=True)
                )
                return
            except ValueError:
                logger.warning(f"Invalid /start payload from user {user_id_str}: {payload}")
                await message.reply_text(
                    MSG_TOKEN_ERROR.format(error_id=uuid.uuid4().hex[:8]),
                    quote=True,
                    link_preview_options=LinkPreviewOptions(is_disabled=True)
                )
                return
            except Exception as e:
                logger.error(f"Error processing /start payload '{payload}' for user {user_id_str}: {e}")
                await handle_user_error(message, MSG_FILE_ACCESS_ERROR)
                return

        parts = message.text.strip().split(maxsplit=1)
        if len(parts) == 1 or (len(parts) > 1 and parts[1].lower() == "start"):
            welcome_text = MSG_WELCOME.format(user_name=message.from_user.first_name if message.from_user else "Guest")

            if Var.FORCE_CHANNEL_ID:
                error_id_context = uuid.uuid4().hex[:8]
                try:
                    chat = await bot.get_chat(Var.FORCE_CHANNEL_ID)
                    invite_link = getattr(chat, 'invite_link', None)
                    chat_username = getattr(chat, 'username', None)
                    chat_title = getattr(chat, 'title', 'Channel')

                    if not invite_link and chat_username:
                        invite_link = f"https://t.me/{chat_username}"

                    if invite_link:
                        welcome_text += f"\n\n{MSG_COMMUNITY_CHANNEL.format(channel_title=chat_title)}"
                    else:
                        logger.warning(f"(ID: {error_id_context}) Could not retrieve invite link for FORCE_CHANNEL_ID {Var.FORCE_CHANNEL_ID} for /start message text (Channel: {chat_title}, User: {user_id_str}).")
                except Exception as e:
                    logger.error(f"(ID: {error_id_context}) Error adding force channel link to /start message text (User: {user_id_str}, FChannel: {Var.FORCE_CHANNEL_ID}): {e}")

            reply_markup_buttons = [
                [
                    InlineKeyboardButton(MSG_BUTTON_GET_HELP, callback_data="help_command"),
                    InlineKeyboardButton(MSG_BUTTON_ABOUT, callback_data="about_command"),
                ],
                [
                    InlineKeyboardButton(MSG_BUTTON_GITHUB, url="https://github.com/fyaz05/FileToLink/"),
                    InlineKeyboardButton(MSG_BUTTON_CLOSE, callback_data="close_panel")
                ]
            ]

            if Var.FORCE_CHANNEL_ID:
                try:
                    chat = await bot.get_chat(Var.FORCE_CHANNEL_ID)
                    invite_link = getattr(chat, 'invite_link', None)
                    chat_username = getattr(chat, 'username', None)
                    if not invite_link and chat_username: invite_link = f"https://t.me/{chat_username}"
                    if invite_link:
                        reply_markup_buttons.append([InlineKeyboardButton(MSG_BUTTON_JOIN_CHANNEL.format(channel_title=getattr(chat, 'title', 'Channel')), url=invite_link)])
                except Exception as e:
                    logger.error(f"Error adding force channel button to /start message (User: {user_id_str}, FChannel: {Var.FORCE_CHANNEL_ID}): {e}")

            reply_markup = InlineKeyboardMarkup(reply_markup_buttons)
            await message.reply_text(text=welcome_text, reply_markup=reply_markup, quote=True, link_preview_options=LinkPreviewOptions(is_disabled=True))
            return

    except Exception as e:
        logger.error(f"Error in start_command for user {user_id_str}: {e}")
        await handle_user_error(message, MSG_ERROR_GENERIC)

@check_banned
@StreamBot.on_message(filters.command("help") & filters.private)
async def help_command(bot: Client, message: Message):
    try:
        user_id_str = str(message.from_user.id) if message.from_user else 'unknown'
        if message.from_user:
            await log_new_user(bot, message.from_user.id, message.from_user.first_name)

        help_text = MSG_HELP
        buttons = [
            [InlineKeyboardButton(MSG_BUTTON_ABOUT, callback_data="about_command")]
        ]

        if Var.FORCE_CHANNEL_ID:
            error_id_context_help = uuid.uuid4().hex[:8]
            try:
                chat = await bot.get_chat(Var.FORCE_CHANNEL_ID)
                invite_link = getattr(chat, 'invite_link', None)
                chat_username = getattr(chat, 'username', None)
                chat_title = getattr(chat, 'title', 'Channel')

                if not invite_link and chat_username:
                    invite_link = f"https://t.me/{chat_username}"

                if invite_link:
                    buttons.append([
                        InlineKeyboardButton(MSG_BUTTON_JOIN_CHANNEL.format(channel_title=chat_title), url=invite_link)
                    ])
                else:
                    logger.warning(f"(ID: {error_id_context_help}) Could not retrieve or construct invite link for FORCE_CHANNEL_ID {Var.FORCE_CHANNEL_ID} for /help command (User: {user_id_str}).")
            except Exception as e_help:
                logger.error(f"(ID: {error_id_context_help}) Error adding force channel button to /help command (User: {user_id_str}, FChannel: {Var.FORCE_CHANNEL_ID}): {e_help}")

        buttons.append([InlineKeyboardButton(MSG_BUTTON_CLOSE, callback_data="close_panel")])
        reply_markup = InlineKeyboardMarkup(buttons)

        await message.reply_text(
            text=help_text,
            reply_markup=reply_markup,
            quote=True,
            link_preview_options=LinkPreviewOptions(is_disabled=True)
        )
    except Exception as e:
        logger.error(f"Error in help_command: {e}")
        await handle_user_error(message, MSG_ERROR_GENERIC)

@check_banned
@StreamBot.on_message(filters.command("about") & filters.private)
async def about_command(bot: Client, message: Message):
    try:
        if message.from_user:
            await log_new_user(bot, message.from_user.id, message.from_user.first_name)

        about_text = MSG_ABOUT
        buttons = [
            [InlineKeyboardButton(MSG_BUTTON_GET_HELP, callback_data="help_command")],
            [InlineKeyboardButton(MSG_BUTTON_GITHUB, url="https://github.com/fyaz05/FileToLink"),
             InlineKeyboardButton(MSG_BUTTON_CLOSE, callback_data="close_panel")]
        ]
        reply_markup = InlineKeyboardMarkup(buttons)

        await message.reply_text(
            text=about_text,
            reply_markup=reply_markup,
            quote=True,
            link_preview_options=LinkPreviewOptions(is_disabled=True)
        )
    except Exception as e:
        logger.error(f"Error in about_command: {e}")
        await handle_user_error(message, MSG_ERROR_GENERIC)

@check_banned
@force_channel_check
@StreamBot.on_message(filters.command("dc"))
async def dc_command(bot: Client, message: Message):
    try:
        if not message.from_user and not message.reply_to_message:
            await handle_user_error(message, MSG_DC_ANON_ERROR)
            return

        async def process_dc_info(user: User):
            dc_text = await generate_dc_text(user)
            buttons = []

            if user.username:
                profile_url = f"https://t.me/{user.username}"
            else:
                profile_url = f"tg://user?id={user.id}"

            buttons.append([
                InlineKeyboardButton(MSG_BUTTON_VIEW_PROFILE, url=profile_url)
            ])
            buttons.append([
                InlineKeyboardButton(MSG_BUTTON_CLOSE, callback_data="close_panel")
            ])

            dc_keyboard = InlineKeyboardMarkup(buttons)
            await message.reply_text(
                dc_text,
                reply_markup=dc_keyboard,
                quote=True,
                link_preview_options=LinkPreviewOptions(is_disabled=True)
            )

        async def process_file_dc_info(file_msg: Message):
            try:
                file_name = get_name(file_msg) or "Untitled File"
                file_size = humanbytes(get_media_file_size(file_msg))

                file_type_map = {
                    "document": MSG_FILE_TYPE_DOCUMENT,
                    "photo": MSG_FILE_TYPE_PHOTO,
                    "video": MSG_FILE_TYPE_VIDEO,
                    "audio": MSG_FILE_TYPE_AUDIO,
                    "voice": MSG_FILE_TYPE_VOICE,
                    "sticker": MSG_FILE_TYPE_STICKER,
                    "animation": MSG_FILE_TYPE_ANIMATION,
                    "video_note": MSG_FILE_TYPE_VIDEO_NOTE
                }

                file_type_attr = next((attr for attr in file_type_map if getattr(file_msg, attr, None)), "unknown")
                file_type_display = file_type_map.get(file_type_attr, MSG_FILE_TYPE_UNKNOWN)

                actual_media = None
                if file_type_attr == "photo" and file_msg.photo and file_msg.photo.sizes:
                    actual_media = file_msg.photo.sizes[-1]
                elif file_type_attr != "unknown":
                    potential = getattr(file_msg, file_type_attr, None)
                    if potential:
                        actual_media = potential

                dc_id = MSG_DC_UNKNOWN
                if hasattr(file_msg, 'raw') and hasattr(file_msg.raw, 'media'):
                    if hasattr(file_msg.raw.media, 'document') and hasattr(file_msg.raw.media.document, 'dc_id'):
                        dc_id = file_msg.raw.media.document.dc_id

                dc_text = MSG_DC_FILE_INFO.format(
                    file_name=file_name,
                    file_size=file_size,
                    file_type=file_type_display,
                    dc_id=dc_id
                )

                await message.reply_text(
                    dc_text,
                    quote=True,
                    link_preview_options=LinkPreviewOptions(is_disabled=True)
                )
            except Exception as e:
                logger.error(f"Error processing file info for DC command: {e}")
                await handle_user_error(message, MSG_DC_FILE_ERROR)

        args = message.text.strip().split(maxsplit=1)
        if len(args) > 1:
            query = args[1].strip()
            user = await get_user_safely(bot, query)
            if user:
                await process_dc_info(user)
            else:
                await handle_user_error(message, MSG_ERROR_USER_INFO)
            return

        if message.reply_to_message:
            if has_media(message.reply_to_message):
                await process_file_dc_info(message.reply_to_message)
                return
            elif message.reply_to_message.from_user:
                await process_dc_info(message.reply_to_message.from_user)
                return
            else:
                await handle_user_error(message, MSG_DC_INVALID_USAGE)
                return

        if message.from_user:
            await process_dc_info(message.from_user)
        else:
            await handle_user_error(message, MSG_DC_ANON_ERROR)
    except Exception as e:
        logger.error(f"Error in dc_command: {e}")
        await handle_user_error(message, MSG_ERROR_GENERIC)

@check_banned
@force_channel_check
@StreamBot.on_message(filters.command("ping") & filters.private)
async def ping_command(bot: Client, message: Message):
    try:
        start_time = time.time()
        sent_msg = await message.reply_text(MSG_PING_START, quote=True, link_preview_options=LinkPreviewOptions(is_disabled=True))
        end_time = time.time()
        time_taken_ms = (end_time - start_time) * 1000

        buttons = [
            [InlineKeyboardButton(MSG_BUTTON_GET_HELP, callback_data="help_command"),
             InlineKeyboardButton(MSG_BUTTON_CLOSE, callback_data="close_panel")]
        ]

        await sent_msg.edit_text(
            MSG_PING_RESPONSE.format(time_taken_ms=time_taken_ms),
            reply_markup=InlineKeyboardMarkup(buttons),
            link_preview_options=LinkPreviewOptions(is_disabled=True)
        )
    except Exception as e:
        logger.error(f"Error in ping_command: {e}")
        await handle_user_error(message, MSG_ERROR_GENERIC)

async def handle_callback_error(callback_query, error, operation="callback"):
    error_id = uuid.uuid4().hex[:8]
    logger.error(f"Error in {operation}: {error}")
    try:
        await callback_query.answer(
            MSG_ERROR_GENERIC_CALLBACK.format(error_id=error_id),
            show_alert=True
        )
    except Exception as e:
        logger.error(f"Failed to send error callback: {e}")

@StreamBot.on_callback_query(filters.regex(r"^close_panel$"))
async def close_panel_callback(client: Client, callback_query: CallbackQuery):
    try:
        await callback_query.answer()
        await callback_query.message.delete()
        if callback_query.message.reply_to_message:
            try:
                ctx = callback_query.message.reply_to_message
                await ctx.delete()
                if ctx.reply_to_message:
                    await ctx.reply_to_message.delete()
            except Exception as e:
                logger.warning(f"Error deleting command messages: {e}")
    except Exception as e:
        await handle_callback_error(callback_query, e, "close_panel_callback")
    finally:
        raise StopPropagation
