# Thunder/utils/custom_dl.py

import math
import asyncio
from typing import Dict, Union
from pyrogram import Client, utils, raw
from pyrogram.session import Session, Auth
from pyrogram.errors import AuthBytesInvalid, RPCError, FloodWait
from pyrogram.file_id import FileId, FileType, ThumbnailSource
from Thunder.vars import Var
from Thunder.bot import work_loads
from Thunder.server.exceptions import FileNotFound
from .file_properties import get_file_ids
from Thunder.utils.logger import logger

class ByteStreamer:
    def __init__(self, client: Client):
        self.client = client
        self.clean_timer = 30 * 60  
        self.cached_file_ids: Dict[int, FileId] = {}
        self.cache_lock = asyncio.Lock()
        asyncio.create_task(self.clean_cache())
        logger.info("ByteStreamer initialized")

    async def get_file_properties(self, message_id: int) -> FileId:
        async with self.cache_lock:
            file_id = self.cached_file_ids.get(message_id)
        
        if not file_id:
            file_id = await self.generate_file_properties(message_id)
            async with self.cache_lock:
                self.cached_file_ids[message_id] = file_id
        
        return file_id

    async def generate_file_properties(self, message_id: int) -> FileId:
        file_id = await get_file_ids(self.client, Var.BIN_CHANNEL, message_id)
        
        if not file_id:
            logger.warning(f"Message ID {message_id} not found in channel")
            raise FileNotFound(f"File with message ID {message_id} not found")
        
        async with self.cache_lock:
            self.cached_file_ids[message_id] = file_id
        
        return file_id

    async def generate_media_session(self, client: Client, file_id: FileId) -> Session:
        media_session = client.media_sessions.get(file_id.dc_id, None)

        if media_session is None:
            if file_id.dc_id != await client.storage.dc_id():
                media_session = Session(
                    client,
                    file_id.dc_id,
                    await Auth(
                        client, file_id.dc_id, await client.storage.test_mode()
                    ).create(),
                    await client.storage.test_mode(),
                    is_media=True,
                )
                await media_session.start()

                for _ in range(6):
                    exported_auth = await client.invoke(
                        raw.functions.auth.ExportAuthorization(dc_id=file_id.dc_id)
                    )

                    try:
                        await media_session.send(
                            raw.functions.auth.ImportAuthorization(
                                id=exported_auth.id, bytes=exported_auth.bytes
                            )
                        )
                        break
                    except AuthBytesInvalid:
                        continue
                else:
                    await media_session.stop()
                    raise AuthBytesInvalid
            else:
                media_session = Session(
                    client,
                    file_id.dc_id,
                    await client.storage.auth_key(),
                    await client.storage.test_mode(),
                    is_media=True,
                )
                await media_session.start()

            client.media_sessions[file_id.dc_id] = media_session

        return media_session

    @staticmethod
    async def get_location(file_id: FileId) -> Union[
        raw.types.InputPhotoFileLocation,
        raw.types.InputDocumentFileLocation,
        raw.types.InputPeerPhotoFileLocation,
    ]:
        file_type = file_id.file_type

        if file_type == FileType.CHAT_PHOTO:
            if file_id.chat_id > 0:
                peer = raw.types.InputPeerUser(
                    user_id=file_id.chat_id, access_hash=file_id.chat_access_hash
                )
            else:
                if file_id.chat_access_hash == 0:
                    peer = raw.types.InputPeerChat(chat_id=-file_id.chat_id)
                else:
                    peer = raw.types.InputPeerChannel(
                        channel_id=utils.get_channel_id(file_id.chat_id),
                        access_hash=file_id.chat_access_hash,
                    )
            location = raw.types.InputPeerPhotoFileLocation(
                peer=peer,
                volume_id=file_id.volume_id,
                local_id=file_id.local_id,
                big=file_id.thumbnail_source == ThumbnailSource.CHAT_PHOTO_BIG,
            )
        elif file_type == FileType.PHOTO:
            location = raw.types.InputPhotoFileLocation(
                id=file_id.media_id,
                access_hash=file_id.access_hash,
                file_reference=file_id.file_reference,
                thumb_size=file_id.thumbnail_size,
            )
        else:
            location = raw.types.InputDocumentFileLocation(
                id=file_id.media_id,
                access_hash=file_id.access_hash,
                file_reference=file_id.file_reference,
                thumb_size=file_id.thumbnail_size,
            )
        return location

    async def yield_file(
        self,
        file_id: FileId,
        index: int,
        offset: int,
        first_part_cut: int,
        last_part_cut: int,
        part_count: int,
        chunk_size: int,
    ) -> Union[bytes, None]:
        client = self.client
        work_loads[index] += 1

        media_session = await self.generate_media_session(client, file_id)
        current_part = 1
        location = await self.get_location(file_id)

        try:
            r = await media_session.send(
                raw.functions.upload.GetFile(
                    location=location, offset=offset, limit=chunk_size
                ),
            )
            if isinstance(r, raw.types.upload.File):
                while True:
                    chunk = r.bytes
                    if not chunk:
                        break
                    elif part_count == 1:
                        yield chunk[first_part_cut:last_part_cut]
                    elif current_part == 1:
                        yield chunk[first_part_cut:]
                    elif current_part == part_count:
                        yield chunk[:last_part_cut]
                    else:
                        yield chunk

                    current_part += 1
                    offset += chunk_size

                    if current_part > part_count:
                        break

                    r = await media_session.send(
                        raw.functions.upload.GetFile(
                            location=location, offset=offset, limit=chunk_size
                        ),
                    )
        except (TimeoutError, AttributeError) as e:
            logger.error(f"File yield error: {e}")
            raise
        finally:
            work_loads[index] -= 1

    async def clean_cache(self) -> None:
        while True:
            await asyncio.sleep(self.clean_timer)
            async with self.cache_lock:
                self.cached_file_ids.clear()
