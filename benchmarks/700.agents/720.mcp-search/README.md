# MCP Keyword Search

**Type:** Agents\
**Languages:** Python\
**Architecture:** x64

## Description

A Model Context Protocol (MCP) server with one tool, `search`, that an AI agent calls to look up documentation by keywords. The server is built with the official MCP Python SDK and served over Streamable HTTP in stateless mode with JSON responses, as in `710.mcp-fetch`. The tool ranks documents with BM25 (`bm25s`) and returns the `k` best matches with their scores, signatures and first lines.

The first request that names a document collection downloads it from object storage and builds the keyword index; later requests reuse the index. The MCP server starts once per container, during the cold start, and requests are passed to its ASGI application in-process. The function keeps the state of a request in module-level variables, so it expects one request at a time per container, as on AWS Lambda, OpenWhisk and the local deployment, where it is tested. Platforms that run several requests concurrently in one instance, such as Azure Functions, are not supported yet. The input is a `tools/call` request in the format of the official MCP Python client in the sessionless `2026-07-28` protocol revision.

## Input

The collection holds one document per public module, function, class and method of the Python 3.11 standard library: its name, signature and docstring (at most 2,000 characters). `make_corpus.py` generates it; common modules come first, so every size contains the core documents.

| Size | Documents | Compressed | Query | Expected hit |
| :--- | ---: | ---: | :--- | :--- |
| test | 500 | 43 kB | parse a JSON string into a Python object | `json.loads` |
| small | 2,500 | 184 kB | compute the median of a list of numbers | `statistics.median` |
| large | 4,104 | 315 kB | greatest common divisor of two integers | `math.gcd` |

Validation checks the HTTP status, the JSON-RPC id, that there is no tool error, that `k` = 5 hits come back, and that the expected document is among them.

## Measurements

All times are in microseconds.

- `request_time`: the full MCP request, from the HTTP request to the JSON-RPC response.
- `compute_time`: tokenizing the query and ranking the documents.
- `protocol_time`: the rest of `request_time`: HTTP handling, JSON-RPC, argument validation and serialization.
- `download_time`, `download_size`, `index_time`: downloading the collection and building the index. Non-zero only for the request that loads a collection.
- `documents`: the number of documents in the collection.
- `import_time`, `server_init_time`: importing the server and starting it. Reported only by the first request of a container.
