import glob
import os

URL = 'https://en.wikipedia.org/wiki/{}'

# Page snapshots ship with the benchmark; a copy in benchmarks-data takes precedence.
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')

# Page fetched for each size, and a phrase its extracted content must contain.
size_generators = {
    'test': ('Function_as_a_service', 'Function as a service'),
    'small': ('Serverless_computing', 'Serverless computing'),
    'large': ('Cloud_computing', 'Cloud computing'),
}

# The fetch tool's default page length.
MAX_LENGTH = 5000
PROTOCOL_VERSION = '2026-07-28'

def buckets_count():
    return (1, 0)

'''
    The request the official MCP Python client (mcp 2.3.0) sends for one tool call
    in the sessionless 2026-07-28 protocol, as recorded from a live exchange.
'''
def tools_call(url, max_length):
    return {
        'headers': {
            'accept': 'application/json, text/event-stream',
            'content-type': 'application/json',
            'mcp-protocol-version': PROTOCOL_VERSION,
            'mcp-method': 'tools/call',
            'mcp-name': 'fetch'
        },
        'body': {
            'jsonrpc': '2.0',
            'id': 2,
            'method': 'tools/call',
            'params': {
                'name': 'fetch',
                'arguments': {'url': url, 'max_length': max_length},
                '_meta': {
                    'io.modelcontextprotocol/protocolVersion': PROTOCOL_VERSION,
                    'io.modelcontextprotocol/clientInfo': {'name': 'mcp', 'version': '0.1.0'},
                    'io.modelcontextprotocol/clientCapabilities': {}
                }
            }
        }
    }

'''
    Upload the page snapshots and generate a tools/call request for the given size.

    :param data_dir: directory where benchmark data is placed, None to use DATA_DIR
    :param size: workload size
    :param benchmarks_bucket: storage container for this benchmark
    :param input_paths: input prefixes, one per input bucket
    :param output_paths: output prefixes, unused
    :param upload_func: upload function taking three params(bucket_idx, key, filepath)
'''
def generate_input(data_dir, size, benchmarks_bucket, input_paths, output_paths, upload_func, nosql_func):

    data_dir = data_dir or DATA_DIR
    # Pages are stored under their host and path, e.g., en.wikipedia.org/wiki/Cloud_computing.html
    for file in glob.glob(os.path.join(data_dir, '**', '*.html'), recursive=True):
        upload_func(0, os.path.relpath(file, data_dir), file)

    page, _ = size_generators[size]
    input_config = {'bucket': {}}
    input_config['bucket']['bucket'] = benchmarks_bucket
    input_config['bucket']['input'] = input_paths[0]
    input_config['request'] = tools_call(URL.format(page), MAX_LENGTH)
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
        return f"Fetch tool reported an error: {content}"
    if len(content) == 0 or content[0].get('type') != 'text':
        return f"Expected one text content block, got {content}"

    text = content[0].get('text', '')
    url = request['params']['arguments']['url']
    header = f'Contents of {url}:\n'
    if not text.startswith(header):
        return f"Tool output does not start with '{header.strip()}'"

    max_length = request['params']['arguments']['max_length']
    page_text = text[len(header):].split('\n\n<error>Content truncated.')[0]
    if len(page_text) > max_length:
        return f"Returned {len(page_text)} characters of page content, more than max_length={max_length}"

    page = url.rsplit('/', 1)[-1]
    phrase = next(phrase for name, phrase in size_generators.values() if name == page)
    if phrase not in page_text:
        return f"Extracted content does not mention '{phrase}'"

    return None
