import hashlib
import os
import urllib.request

# The document collection ships with the benchmark (the same files as 720.mcp-search);
# a copy in benchmarks-data takes precedence.
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')

# Collection searched for each size, the query, and the document that must be among the hits.
size_generators = {
    'test': ('corpus-test.jsonl.gz', 'parse a JSON string into a Python object', 'json.loads'),
    'small': ('corpus-small.jsonl.gz', 'compute the median of a list of numbers', 'statistics.median'),
    'large': ('corpus-large.jsonl.gz', 'greatest common divisor of two integers', 'math.gcd'),
}

# Static embedding model (model2vec, MIT license), pinned to one revision. Downloaded once
# into data/model/ and checked against these hashes.
MODEL_URL = 'https://huggingface.co/minishlab/potion-base-8M/resolve/bf8b056651a2c21b8d2565580b8569da283cab23/{}'
MODEL_FILES = {
    'config.json': '2a6ac0e9aaa356a68a5688070db78fc3a464fefe85d2f06a1905ce3718687553',
    'model.safetensors': 'f65d0f325faadc1e121c319e2faa41170d3fa07d8c89abd48ca5358d9a223de2',
    'tokenizer.json': 'e67e803f624fb4d67dea1c730d06e1067e1b14d830e2c2202569e3ef0f70bb50',
}
MODEL_PREFIX = 'model'

TOP_K = 5
PROTOCOL_VERSION = '2026-07-28'

def buckets_count():
    return (1, 0)

'''
    A tools/call request as the official MCP Python client (mcp 2.3.0) sends it in the
    sessionless 2026-07-28 protocol, in the format recorded for 710.mcp-fetch.
'''
def tools_call(query, k):
    return {
        'headers': {
            'accept': 'application/json, text/event-stream',
            'content-type': 'application/json',
            'mcp-protocol-version': PROTOCOL_VERSION,
            'mcp-method': 'tools/call',
            'mcp-name': 'search'
        },
        'body': {
            'jsonrpc': '2.0',
            'id': 2,
            'method': 'tools/call',
            'params': {
                'name': 'search',
                'arguments': {'query': query, 'k': k},
                '_meta': {
                    'io.modelcontextprotocol/protocolVersion': PROTOCOL_VERSION,
                    'io.modelcontextprotocol/clientInfo': {'name': 'mcp', 'version': '0.1.0'},
                    'io.modelcontextprotocol/clientCapabilities': {}
                }
            }
        }
    }

def model_file(data_dir, name):
    path = os.path.join(data_dir, MODEL_PREFIX, name)
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Download next to the target and move it into place only when complete and verified,
        # so an interrupted download never leaves a broken file behind.
        partial = path + '.part'
        with urllib.request.urlopen(MODEL_URL.format(name), timeout=60) as response, open(partial, 'wb') as f:
            f.write(response.read())
        os.replace(partial, path)
    with open(path, 'rb') as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    if digest != MODEL_FILES[name]:
        raise RuntimeError(f"{path} has SHA-256 {digest}, expected {MODEL_FILES[name]}; delete it to download again")
    return path

'''
    Upload the embedding model and the document collection of the given size,
    and generate a search request.

    :param data_dir: directory where benchmark data is placed, None to use DATA_DIR
    :param size: workload size
    :param benchmarks_bucket: storage container for this benchmark
    :param input_paths: input prefixes, one per input bucket
    :param output_paths: output prefixes, unused
    :param upload_func: upload function taking three params(bucket_idx, key, filepath)
'''
def generate_input(data_dir, size, benchmarks_bucket, input_paths, output_paths, upload_func, nosql_func):

    data_dir = data_dir or DATA_DIR
    for name in MODEL_FILES:
        upload_func(0, os.path.join(MODEL_PREFIX, name), model_file(data_dir, name))
    # SeBS uploads a benchmark's input once, not per size, so every collection goes up.
    for corpus, _, _ in size_generators.values():
        upload_func(0, corpus, os.path.join(data_dir, corpus))
    corpus, query, _ = size_generators[size]

    input_config = {'bucket': {}}
    input_config['bucket']['bucket'] = benchmarks_bucket
    input_config['bucket']['input'] = input_paths[0]
    input_config['model'] = MODEL_PREFIX
    input_config['corpus'] = corpus
    input_config['request'] = tools_call(query, TOP_K)
    return input_config

def validate_output(data_dir: str | None, input_config: dict, output: dict, language: str, storage = None) -> str | None:

    result = output.get('result', {})
    status = result.get('status')
    if status != 200:
        return f"Expected HTTP status 200 from the MCP server, got {status}"

    body = result.get('body') or {}
    request = input_config['request']['body']
    if body.get('id') != request['id']:
        return f"Response id {body.get('id')} does not match request id {request['id']}"
    if 'error' in body:
        return f"MCP server returned a JSON-RPC error: {body['error']}"

    tool_result = body.get('result', {})
    content = tool_result.get('content') or []
    if tool_result.get('isError'):
        return f"Search tool reported an error: {content}"
    if len(content) == 0 or content[0].get('type') != 'text':
        return f"Expected one text content block, got {content}"

    # Each hit starts with "<rank>. <document id> (score ...)"
    text = content[0].get('text', '')
    hits = [line.split('. ', 1)[1].split(' (score')[0] for line in text.splitlines() if line[:1].isdigit()]
    k = request['params']['arguments']['k']
    if len(hits) != k:
        return f"Expected {k} hits, got {len(hits)}"

    _, query, expected = next(g for g in size_generators.values() if g[1] == request['params']['arguments']['query'])
    if expected not in hits:
        return f"Expected {expected} among the hits for {query!r}, got {hits}"

    return None
