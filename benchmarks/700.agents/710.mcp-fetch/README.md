# MCP Fetch Server

**Type:** Agents\
**Languages:** Python\
**Architecture:** x64

## Description

A Model Context Protocol (MCP) server that an AI agent calls to read a web page. The function runs the reference [Fetch MCP server](https://github.com/modelcontextprotocol/servers/tree/main/src/fetch) (`mcp-server-fetch`) unchanged, served over Streamable HTTP in stateless mode with JSON responses, as MCP servers are deployed on FaaS platforms. Each invocation processes one HTTP request of an agent. The input is the `tools/call` request that the official MCP Python client sends in the sessionless `2026-07-28` protocol revision.

The tool extracts the main content of the page with readabilipy, converts it to Markdown with markdownify, and returns at most `max_length` characters. The only change to the reference server is the source of the page: it is read from object storage instead of the internet, so that runs are reproducible and outputs can be validated. robots.txt is not consulted. Without Node.js, readabilipy uses its pure-Python extractor, as on a Python-only FaaS runtime.

The MCP server starts once per container, during the cold start, and serves all later requests, as with a web server behind a Lambda web adapter. Requests are passed to its ASGI application in-process.

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
