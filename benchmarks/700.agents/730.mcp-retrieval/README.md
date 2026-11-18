# MCP Embedding Retrieval

**Type:** Agents\
**Languages:** Python\
**Architecture:** x64

## Description

A Model Context Protocol (MCP) server with one tool, `search`, that an AI agent calls to look up documentation by meaning instead of keywords. It searches the same collection with the same queries as `720.mcp-search`, so the two benchmarks compare keyword search with embedding retrieval. The server is built with the official MCP Python SDK and served over Streamable HTTP in stateless mode with JSON responses, as in `710.mcp-fetch`.

The tool embeds the query with a static embedding model (`model2vec`, model `minishlab/potion-base-8M`, 256 dimensions, MIT license) and returns the `k` documents with the highest cosine similarity. The first request downloads the model (31 MB) from object storage and loads it; the first request that names a document collection downloads it and embeds every document. Later requests reuse both. The MCP server starts once per container, during the cold start, and requests are passed to its ASGI application in-process. The function keeps the state of a request in module-level variables, so it expects one request at a time per container, as on AWS Lambda, OpenWhisk and the local deployment, where it is tested. Platforms that run several requests concurrently in one instance, such as Azure Functions, are not supported yet.

## Input

The document collection and queries of `720.mcp-search` (`data/corpus-*.jsonl.gz`, identical files):

| Size | Documents | Query | Expected hit |
| :--- | ---: | :--- | :--- |
| test | 500 | parse a JSON string into a Python object | `json.loads` |
| small | 2,500 | compute the median of a list of numbers | `statistics.median` |
| large | 4,104 | greatest common divisor of two integers | `math.gcd` |

The model files are downloaded once from Hugging Face at a pinned revision into `data/model/`, checked against their SHA-256 hashes, and uploaded with the input. Validation checks the HTTP status, the JSON-RPC id, that there is no tool error, that `k` = 5 hits come back, and that the expected document is among them.

## Measurements

All times are in microseconds.

- `request_time`: the full MCP request, from the HTTP request to the JSON-RPC response.
- `compute_time`: embedding the query and ranking the documents.
- `protocol_time`: the rest of `request_time`: HTTP handling, JSON-RPC, argument validation and serialization.
- `model_download_time`, `model_size`, `model_load_time`: downloading and loading the embedding model. Non-zero only for the request that loads it.
- `download_time`, `download_size`, `index_time`: downloading the collection and embedding its documents. Non-zero only for the request that loads a collection.
- `documents`: the number of documents in the collection.
- `import_time`, `server_init_time`: importing the server and starting it. Reported only by the first request of a container.
