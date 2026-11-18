# MCP Fetch Server

**Type:** Agents\
**Languages:** Python\
**Architecture:** x64

## Description

A Model Context Protocol (MCP) server that an AI agent calls to read a web page. The function runs the reference [Fetch MCP server](https://github.com/modelcontextprotocol/servers/tree/main/src/fetch) (`mcp-server-fetch`) unchanged, served over Streamable HTTP in stateless mode with JSON responses, as MCP servers are deployed on FaaS platforms. Each invocation processes one HTTP request of an agent. The input is the `tools/call` request that the official MCP Python client sends in the sessionless `2026-07-28` protocol revision.

The tool extracts the main content of the page with readabilipy, converts it to Markdown with markdownify, and returns at most `max_length` characters. The only change to the reference server is the source of the page: it is read from object storage instead of the internet, so that runs are reproducible and outputs can be validated. robots.txt is not consulted. Without Node.js, readabilipy uses its pure-Python extractor, as on a Python-only FaaS runtime.

The MCP server starts once per container, during the cold start, and serves all later requests, as with a web server behind a Lambda web adapter. Requests are passed to its ASGI application in-process. The function keeps the state of a request in module-level variables, so it expects one request at a time per container, as on AWS Lambda, OpenWhisk and the local deployment, where it is tested. Platforms that run several requests concurrently in one instance, such as Azure Functions, are not supported yet.

## Input

Snapshots of three English Wikipedia articles, licensed under CC BY-SA 4.0. They are stored in `data/` under their host and path, and are used from `benchmarks-data/700.agents/710.mcp-fetch/` instead when that directory exists.

| Size | Article | Revision | HTML size |
| :--- | :--- | :--- | :--- |
| test | Function as a service | 1357453728 | 110 kB |
| small | Serverless computing | 1375122930 | 176 kB |
| large | Cloud computing | 1382131775 | 513 kB |

## Measurements

All times are in microseconds.

- `request_time`: the full MCP request, from the HTTP request to the JSON-RPC response.
- `download_time`, `download_size`: reading the page from object storage.
- `compute_time`: content extraction and conversion to Markdown.
- `protocol_time`: the rest of `request_time`: HTTP handling, JSON-RPC, argument validation and serialization.
- `import_time`, `server_init_time`: importing the server and starting it. Reported only by the first request of a container.

## Agent sessions

An agent rarely sends a single request. With the default `max_length` of 5000 characters, reading a whole article takes several `tools/call` requests: each output ends by asking for another call from a later `start_index`, and the server fetches and extracts the whole page again for each of them. The requests around the tool calls depend on the client's protocol revision: `tools/list` only (`2026-07-28`), `server/discover` first (`auto`), or the `initialize` handshake (`legacy`).

| Size | Tool calls to read the page | Invocations per session (`2026-07-28` / `auto` / `legacy`) |
| :--- | ---: | :--- |
| test | 2 | 3 / 4 / 5 |
| small | 4 | 5 / 6 / 7 |
| large | 13 | 14 / 15 / 16 |

The `agent-sessions` experiment replays such sessions with the official MCP Python client, one function invocation per HTTP request. It needs the client from the `agents` extra (`pip install -e '.[agents]'` or `uv sync --extra agents`):

```bash
sebs experiment invoke agent-sessions --config configs/example.json --deployment local \
    --storage-configuration out_storage.json --output-dir results
sebs experiment process agent-sessions --config configs/example.json --deployment local \
    --output-dir results
```

The input size, the client modes and the number of sessions per mode are set under `experiments.agent-sessions` in the configuration. Each mode's sessions and invocations are written to `agent-sessions/<mode>_results.json`, and `process` writes one CSV row per invocation to `agent-sessions/agent_sessions.csv`.

## Keeping the extracted page

With `"cache": true` in the input, the function keeps the extracted text of each page in the container, so the next slice of the same page skips the download and the extraction. Pages are kept per agent session (the `session` field of the input) and only the 16 most recent ones. Without the field, the function behaves like the reference server. Each request reports `cache_hit` in its measurements.

The `agent-sessions` experiment sets `session` itself and adds the fields of its `input` setting to every request, so `"input": {"cache": true}` under `experiments.agent-sessions` runs the sessions with the cache.
