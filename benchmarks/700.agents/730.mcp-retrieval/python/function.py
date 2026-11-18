import time
_import_begin = time.perf_counter()

import asyncio
import gzip
import json
import os
import tempfile
import threading

import httpx
import numpy as np
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from model2vec import StaticModel

from . import storage
client = storage.storage.get_instance()

_import_end = time.perf_counter()

MODEL_FILES = ('config.json', 'model.safetensors', 'tokenizer.json')

# The embedding model and the embedded document collection. A retrieval server loads
# both at startup; here their location arrives with the first request that names them.
model = {}
collection = {}
timings = {}


def load_model(bucket, prefix):
    if model.get('key') == (bucket, prefix):
        return

    directory = tempfile.mkdtemp()
    download_begin = time.perf_counter()
    size = 0
    for name in MODEL_FILES:
        data = client.download_stream(bucket, os.path.join(prefix, name))
        size += len(data)
        with open(os.path.join(directory, name), 'wb') as f:
            f.write(data)
    download_end = time.perf_counter()
    loaded = StaticModel.from_pretrained(directory)
    load_end = time.perf_counter()

    model.update(key=(bucket, prefix), model=loaded)
    collection.clear()
    timings['model_download_time'] = (download_end - download_begin) * 1e6
    timings['model_size'] = size
    timings['model_load_time'] = (load_end - download_end) * 1e6


def load_collection(bucket, key):
    if collection.get('key') == (bucket, key):
        return

    download_begin = time.perf_counter()
    data = client.download_stream(bucket, key)
    download_end = time.perf_counter()

    docs = [json.loads(line) for line in gzip.decompress(data).splitlines()]
    vectors = model['model'].encode([d['title'] + '\n' + d['text'] for d in docs])
    vectors /= np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
    index_end = time.perf_counter()

    collection.update(key=(bucket, key), docs=docs, vectors=vectors)
    timings['download_time'] = (download_end - download_begin) * 1e6
    timings['download_size'] = len(data)
    timings['index_time'] = (index_end - download_end) * 1e6


server = MCPServer('python-docs-retrieval')


@server.tool()
def search(query: str, k: int = 5) -> str:
    """Search the Python standard library documentation by meaning and return the k closest matches."""
    docs, vectors = collection['docs'], collection['vectors']
    compute_begin = time.perf_counter()
    try:
        q = model['model'].encode([query])[0]
        scores = vectors @ (q / np.linalg.norm(q))
    except Exception as e:
        raise ToolError(f"Search failed for {query!r}: {e!r}")
    k = min(k, len(docs))
    ids = np.argpartition(-scores, k - 1)[:k]
    ids = ids[np.argsort(-scores[ids])]
    timings['compute_time'] = (time.perf_counter() - compute_begin) * 1e6

    hits = []
    for rank, i in enumerate(ids, start=1):
        doc = docs[i]
        hits.append(f"{rank}. {doc['id']} (score {scores[i]:.2f})\n   {doc['title']}\n   {doc['text'].splitlines()[0]}")
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
    load_model(bucket.get('bucket'), os.path.join(bucket.get('input'), event.get('model')))
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
        # Loading the model and the collection happens only when a request names new ones.
        'model_download_time': timings.get('model_download_time', 0),
        'model_size': timings.get('model_size', 0),
        'model_load_time': timings.get('model_load_time', 0),
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
