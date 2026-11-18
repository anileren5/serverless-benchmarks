import time
_import_begin = time.perf_counter()

import asyncio
import gzip
import json
import os
import threading

import bm25s
import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import storage
client = storage.storage.get_instance()

_import_end = time.perf_counter()

# The document collection and its keyword index. A search server builds its index at
# startup; here the collection's location arrives with the first request that names it.
collection = {}
timings = {}


def load_collection(bucket, key):
    if collection.get('key') == (bucket, key):
        return

    download_begin = time.perf_counter()
    data = client.download_stream(bucket, key)
    download_end = time.perf_counter()

    docs = [json.loads(line) for line in gzip.decompress(data).splitlines()]
    tokens = bm25s.tokenize([d['title'] + '\n' + d['text'] for d in docs], stopwords='en', show_progress=False)
    retriever = bm25s.BM25()
    retriever.index(tokens, show_progress=False)
    index_end = time.perf_counter()

    collection.update(key=(bucket, key), docs=docs, retriever=retriever)
    timings['download_time'] = (download_end - download_begin) * 1e6
    timings['download_size'] = len(data)
    timings['index_time'] = (index_end - download_end) * 1e6
    timings['documents'] = len(docs)


server = MCPServer('python-docs-search')


@server.tool()
def search(query: str, k: int = 5) -> str:
    """Search the Python standard library documentation by keywords and return the k best matches."""
    docs = collection['docs']
    compute_begin = time.perf_counter()
    try:
        tokens = bm25s.tokenize([query], stopwords='en', show_progress=False)
        ids, scores = collection['retriever'].retrieve(tokens, k=min(k, len(docs)), show_progress=False)
    except Exception as e:
        raise ToolError(f"Search failed for {query!r}: {e!r}")
    timings['compute_time'] = (time.perf_counter() - compute_begin) * 1e6

    hits = []
    for rank, (i, score) in enumerate(zip(ids[0], scores[0]), start=1):
        doc = docs[i]
        hits.append(f"{rank}. {doc['id']} (score {score:.2f})\n   {doc['title']}\n   {doc['text'].splitlines()[0]}")
    return '\n'.join(hits)


# One long-lived server per container, as in 710.mcp-fetch.
_server_begin = time.perf_counter()
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

    timings.clear()
    bucket = event.get('bucket')
    load_collection(bucket.get('bucket'), os.path.join(bucket.get('input'), event.get('corpus')))

    request_begin = time.perf_counter()
    response = asyncio.run_coroutine_threadsafe(post(event.get('request')), loop).result()
    request_end = time.perf_counter()

    request_time = (request_end - request_begin) * 1e6
    # Notifications are acknowledged with 202 and no body, even with a JSON content type.
    is_json = response.content and response.headers.get('content-type', '').startswith('application/json')
    measurement = {
        'request_time': request_time,
        'compute_time': timings.get('compute_time', 0),
        'protocol_time': request_time - timings.get('compute_time', 0),
        'response_size': len(response.content),
        # Loading the collection happens only when a request names a new one.
        'download_time': timings.get('download_time', 0),
        'download_size': timings.get('download_size', 0),
        'index_time': timings.get('index_time', 0),
        'documents': len(collection['docs']),
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
