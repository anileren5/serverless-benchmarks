import os

# The document collection ships with the benchmark; a copy in benchmarks-data takes precedence.
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')

# Collection searched for each size, the query, and the document that must be among the hits.
size_generators = {
    'test': ('corpus-test.jsonl.gz', 'parse a JSON string into a Python object', 'json.loads'),
    'small': ('corpus-small.jsonl.gz', 'compute the median of a list of numbers', 'statistics.median'),
    'large': ('corpus-large.jsonl.gz', 'greatest common divisor of two integers', 'math.gcd'),
}

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

'''
    Upload the document collection of the given size and generate a search request.

    :param data_dir: directory where benchmark data is placed, None to use DATA_DIR
    :param size: workload size
    :param benchmarks_bucket: storage container for this benchmark
    :param input_paths: input prefixes, one per input bucket
    :param output_paths: output prefixes, unused
    :param upload_func: upload function taking three params(bucket_idx, key, filepath)
'''
def generate_input(data_dir, size, benchmarks_bucket, input_paths, output_paths, upload_func, nosql_func):

    data_dir = data_dir or DATA_DIR
    # SeBS uploads a benchmark's input once, not per size, so every collection goes up.
    for corpus, _, _ in size_generators.values():
        upload_func(0, corpus, os.path.join(data_dir, corpus))
    corpus, query, _ = size_generators[size]

    input_config = {'bucket': {}}
    input_config['bucket']['bucket'] = benchmarks_bucket
    input_config['bucket']['input'] = input_paths[0]
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
