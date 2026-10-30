import time
_import_begin = time.perf_counter()

import asyncio
import os
import threading
from urllib.parse import urlparse

import httpx
import mcp_server_fetch.server as fetch_server
from mcp.server.mcpserver.exceptions import ToolError

from . import storage
client = storage.storage.get_instance()

_import_end = time.perf_counter()

# The page the tool fetches is read from object storage instead of the internet,
# so that runs are reproducible. Everything else is the reference server's code.
corpus = {}
timings = {}


async def fetch_from_storage(url, user_agent, force_raw=False, **_):
    parsed = urlparse(url)
    key = os.path.join(corpus['prefix'], parsed.netloc + parsed.path + '.html')

    download_begin = time.perf_counter()
    try:
        page = client.download_stream(corpus['bucket'], key)
    except Exception as e:
        raise ToolError(f"Failed to fetch {url}: {e!r}")
    download_end = time.perf_counter()
    timings['download_time'] = (download_end - download_begin) * 1e6
    timings['download_size'] = len(page)

    page_raw = page.decode('utf-8')
    if force_raw:
        return page_raw, ''

    compute_begin = time.perf_counter()
    content = fetch_server.extract_content_from_html(page_raw)
    compute_end = time.perf_counter()
    timings['compute_time'] = (compute_end - compute_begin) * 1e6
    return content, ''

fetch_server.fetch_url = fetch_from_storage

# One long-lived server per container, as with uvicorn behind a Lambda web adapter:
# the session manager can be started only once, so it runs in a background loop.
_server_begin = time.perf_counter()
server = fetch_server.create_server(ignore_robots_txt=True)
app = server.streamable_http_app(stateless_http=True, json_response=True)
loop = asyncio.new_event_loop()
ready = threading.Event()
http = None


async def serve():
    global http
    async with server.session_manager.run():
        # The address a Lambda web adapter forwards requests to by default.
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8080')
        ready.set()
        await asyncio.Event().wait()

threading.Thread(target=loop.run_until_complete, args=(serve(),), daemon=True).start()
ready.wait()
_server_end = time.perf_counter()

cold = True


async def post(request):
    return await http.post('/mcp', json=request['body'], headers=request['headers'])


def handler(event):
    global cold

    corpus['bucket'] = event.get('bucket').get('bucket')
    corpus['prefix'] = event.get('bucket').get('input')
    timings.clear()

    request_begin = time.perf_counter()
    response = asyncio.run_coroutine_threadsafe(post(event.get('request')), loop).result()
    request_end = time.perf_counter()

    request_time = (request_end - request_begin) * 1e6
    # Notifications are acknowledged with 202 and no body, even with a JSON content type.
    is_json = response.content and response.headers.get('content-type', '').startswith('application/json')
    measurement = {
        'request_time': request_time,
        'download_time': timings.get('download_time', 0),
        'download_size': timings.get('download_size', 0),
        'compute_time': timings.get('compute_time', 0),
        'protocol_time': request_time - timings.get('download_time', 0) - timings.get('compute_time', 0),
        'response_size': len(response.content),
    }
    # Initialization happens once per container, so report it with the first request only.
    if cold:
        measurement['import_time'] = (_import_end - _import_begin) * 1e6
        measurement['server_init_time'] = (_server_end - _server_begin) * 1e6
        cold = False

    return {
        'result': {
            'status': response.status_code,
            'headers': dict(response.headers),
            'body': response.json() if is_json else response.text
        },
        'measurement': measurement
    }
