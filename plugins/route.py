from aiohttp import web
import re
import math
import logging
import secrets
import mimetypes
from dreamxbotz.Bot import multi_clients, work_loads
from dreamxbotz.server.exceptions import FIleNotFound, InvalidHash
from dreamxbotz.util.custom_dl import ByteStreamer
from dreamxbotz.util.render_template import render_page
import info

logger = logging.getLogger(__name__)
routes = web.RouteTableDef()


@routes.get("/favicon.ico")
async def favicon_route_handler(request):
    return web.FileResponse("dreamxbotz/template/favicon.ico")


@routes.get("/", allow_head=True)
async def root_route_handler(request):
    return web.json_response("dreamxbotz")


@routes.get("/miniapp", allow_head=True)
async def miniapp_route_handler(request):
    return web.Response(
        text="""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Opening Watch</title><script src="https://telegram.org/js/telegram-web-app.js"></script></head><body><p>Opening watch page…</p><script>
const tg=window.Telegram&&window.Telegram.WebApp;
if(tg){tg.ready();}
const p=tg&&tg.initDataUnsafe&&tg.initDataUnsafe.start_param;
const m=p&&p.match(/^w_(\\d+)_([A-Za-z0-9_-]{6,})$/);
if(m){location.replace('/watch/'+m[1]+'?hash='+encodeURIComponent(m[2]));}
else{location.replace('/');}
</script></body></html>""",
        content_type="text/html",
    )


@routes.get(r"/watch/{path:\S+}", allow_head=True)
async def watch_handler(request: web.Request):
    try:
        path = request.match_info["path"]
        match = re.search(r"^([a-zA-Z0-9_-]{6})(\d+)$", path)
        if match:
            secure_hash = match.group(1)
            file_id = int(match.group(2))
        else:
            id_match = re.search(r"(\d+)(?:/\S+)?", path)
            if not id_match:
                raise web.HTTPNotFound(text="Not found")
            file_id = int(id_match.group(1))
            secure_hash = request.rel_url.query.get("hash")

        return web.Response(
            text=await render_page(file_id, secure_hash),
            content_type="text/html",
        )
    except InvalidHash as e:
        raise web.HTTPForbidden(text=e.message)
    except FIleNotFound as e:
        raise web.HTTPNotFound(text=e.message)
    except (AttributeError, BadStatusLine, ConnectionResetError):
        raise web.HTTPNotFound(text="Not found")
    except Exception as e:
        logger.critical(e.with_traceback(None))
        raise web.HTTPInternalServerError(text=str(e))


# Dedicated download endpoint. Keeping it separate avoids accidental routing
# conflicts with the normal streaming route and makes the client URL explicit.
@routes.get(r"/download/{path:\S+}", allow_head=True)
async def download_handler(request: web.Request):
    try:
        path = request.match_info["path"]
        match = re.search(r"^([a-zA-Z0-9_-]{6})(\d+)$", path)
        if match:
            secure_hash = match.group(1)
            file_id = int(match.group(2))
        else:
            id_match = re.search(r"(\d+)(?:/\S+)?", path)
            if not id_match:
                raise web.HTTPNotFound(text="Not found")
            file_id = int(id_match.group(1))
            secure_hash = request.rel_url.query.get("hash")

        return await media_streamer(request, file_id, secure_hash, download=True)
    except InvalidHash as e:
        raise web.HTTPForbidden(text=e.message)
    except FIleNotFound as e:
        raise web.HTTPNotFound(text=e.message)
    except web.HTTPNotFound:
        raise
    except (AttributeError, BadStatusLine, ConnectionResetError):
        raise web.HTTPNotFound(text="Not found")
    except Exception as e:
        logger.critical(e.with_traceback(None))
        raise web.HTTPInternalServerError(text=str(e))


@routes.get(r"/{path:\S+}", allow_head=True)
async def stream_handler(request: web.Request):
    try:
        path = request.match_info["path"]
        match = re.search(r"^([a-zA-Z0-9_-]{6})(\d+)$", path)
        if match:
            secure_hash = match.group(1)
            file_id = int(match.group(2))
        else:
            id_match = re.search(r"(\d+)(?:/\S+)?", path)
            if not id_match:
                raise web.HTTPNotFound(text="Not found")
            file_id = int(id_match.group(1))
            secure_hash = request.rel_url.query.get("hash")

        return await media_streamer(
            request,
            file_id,
            secure_hash,
            download=request.rel_url.query.get("download") == "1",
        )
    except InvalidHash as e:
        raise web.HTTPForbidden(text=e.message)
    except FIleNotFound as e:
        raise web.HTTPNotFound(text=e.message)
    except web.HTTPNotFound:
        raise
    except (AttributeError, BadStatusLine, ConnectionResetError):
        raise web.HTTPNotFound(text="Not found")
    except Exception as e:
        logger.critical(e.with_traceback(None))
        raise web.HTTPInternalServerError(text=str(e))


class_cache = {}


def parse_range_header(range_header: str | None, file_size: int):
    """Return inclusive (start, end) for a single HTTP byte range."""
    if not range_header:
        return 0, file_size - 1, False

    if not range_header.startswith("bytes="):
        raise ValueError("Invalid Range header")

    value = range_header[6:].strip()
    # We only support one range. Browsers/video players normally use one.
    if "," in value:
        raise ValueError("Multiple ranges are not supported")

    start_text, end_text = value.split("-", 1)

    if start_text == "":
        # Suffix range: bytes=-500
        suffix = int(end_text)
        if suffix <= 0:
            raise ValueError("Invalid suffix range")
        start = max(file_size - suffix, 0)
        end = file_size - 1
    else:
        start = int(start_text)
        if start < 0 or start >= file_size:
            raise ValueError("Range start outside file")
        if end_text == "":
            end = file_size - 1
        else:
            end = int(end_text)
            if end < start:
                raise ValueError("Range end before start")
            end = min(end, file_size - 1)

    return start, end, True


async def media_streamer(request: web.Request, id: int, secure_hash: str, download: bool = False):
    index = min(work_loads, key=work_loads.get)
    faster_client = multi_clients[index]

    if info.MULTI_CLIENT:
        logger.info(f"Client {index} is now serving {request.remote}")

    if faster_client in class_cache:
        tg_connect = class_cache[faster_client]
    else:
        tg_connect = ByteStreamer(faster_client)
        class_cache[faster_client] = tg_connect

    file_id = await tg_connect.get_file_properties(id)

    if not secure_hash or file_id.unique_id[:6] != secure_hash:
        logger.debug(f"Invalid hash for message with ID {id}")
        raise InvalidHash

    file_size = int(file_id.file_size or 0)
    if file_size <= 0:
        raise FIleNotFound

    range_header = request.headers.get("Range")
    try:
        from_bytes, until_bytes, is_range = parse_range_header(range_header, file_size)
    except (ValueError, TypeError):
        return web.Response(
            status=416,
            text="416: Range Not Satisfiable",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    chunk_size = 1024 * 1024
    until_bytes = min(until_bytes, file_size - 1)
    if from_bytes > until_bytes:
        return web.Response(
            status=416,
            text="416: Range Not Satisfiable",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    offset = from_bytes - (from_bytes % chunk_size)
    first_part_cut = from_bytes - offset
    last_part_cut = until_bytes % chunk_size + 1
    req_length = until_bytes - from_bytes + 1
    part_count = math.ceil((until_bytes + 1) / chunk_size) - math.floor(offset / chunk_size)

    body = tg_connect.yield_file(
        file_id,
        index,
        offset,
        first_part_cut,
        last_part_cut,
        part_count,
        chunk_size,
    )

    mime_type = file_id.mime_type or mimetypes.guess_type(file_id.file_name or "")[0]
    file_name = file_id.file_name or f"{secrets.token_hex(2)}.bin"
    mime_type = mime_type or "application/octet-stream"

    headers = {
        "Content-Type": mime_type,
        "Content-Length": str(req_length),
        "Accept-Ranges": "bytes",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
        "Access-Control-Allow-Headers": "Range, Content-Type",
        "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges",
        "Cache-Control": "no-cache",
    }

    if is_range:
        headers["Content-Range"] = f"bytes {from_bytes}-{until_bytes}/{file_size}"

    disposition = "attachment" if download else "inline"
    safe_name = file_name.replace("\\", "_").replace('"', "'")
    headers["Content-Disposition"] = f'{disposition}; filename="{safe_name}"'

    return web.Response(
        status=206 if is_range else 200,
        body=body,
        headers=headers,
    )
