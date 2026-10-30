# Copyright 2020-2025 ETH Zurich and the SeBS authors. All rights reserved.
"""Agent session experiment for MCP tool benchmarks.

An AI agent rarely calls a tool once. It connects to the MCP server, lists the
tools, and calls a tool again when its output is truncated. When the MCP server
runs as a serverless function, every HTTP request of that exchange is a separate
invocation, and how many requests a session needs depends on the protocol
revision the client speaks.

This experiment drives a deployed MCP benchmark (700.agents) with the official
MCP Python client. Each HTTP request of the client becomes one invocation of the
function. A session lists the tools, then calls the tool from the benchmark
input and follows the continuation hints in its output until the whole result
has been read, as an agent reading a long web page does.

The MCP client is an optional dependency, installed with the ``agents`` extra.
"""

import asyncio
import csv
import glob
import json
import os
import re
from datetime import datetime
from typing import List, TYPE_CHECKING

from sebs.faas.system import System as FaaSSystem
from sebs.faas.function import Trigger
from sebs.experiments.experiment import Experiment
from sebs.experiments.result import Result as ExperimentResult
from sebs.experiments.config import Config as ExperimentConfig
from sebs.utils import serialize

# import cycle
if TYPE_CHECKING:
    from sebs import SeBS
    import httpx2

# The reference fetch server ends a truncated output by asking for another call
# that starts at the given character index.
CONTINUATION = re.compile(r"start_index of (\d+) to get more content\.</error>$")

# Set by the HTTP library or the connection, not by the MCP client. The server in
# the function checks the host against its own address.
DROPPED_HEADERS = {"host", "content-length", "user-agent", "accept-encoding", "connection"}

# Requests never leave the process: each one is turned into a function invocation.
SERVER_URL = "http://127.0.0.1:8080/mcp"


class AgentSessions(Experiment):
    """Replays MCP agent sessions against a deployed tool benchmark.

    Runs the configured number of sessions in each client mode: ``legacy``
    (initialize handshake), ``auto`` (discovery, then the newest revision) or a
    pinned protocol version such as ``2026-07-28``. Every invocation is recorded
    with its JSON-RPC method, client time, cold start flag and the benchmark's
    own measurements.

    Attributes:
        _benchmark: The benchmark to invoke
        _benchmark_input: Input of the benchmark; its ``request`` holds the tool call
        _function: The function to invoke
        _trigger: HTTP trigger of the function
        _out_dir: Directory for storing results
        _deployment_client: The deployment client to use
        _result: Experiment result of the current mode
        _session_invocations: Invocations of the current session
    """

    def __init__(self, config: ExperimentConfig):
        """Initialize a new AgentSessions experiment.

        Args:
            config: Experiment configuration
        """
        super().__init__(config)

    @staticmethod
    def name() -> str:
        """Get the name of the experiment.

        Returns:
            The name "agent-sessions"
        """
        return "agent-sessions"

    @staticmethod
    def typename() -> str:
        """Get the type name of the experiment.

        Returns:
            The type name "Experiment.AgentSessions"
        """
        return "Experiment.AgentSessions"

    def prepare(self, sebs_client: "SeBS", deployment_client: FaaSSystem) -> None:
        """Prepare the benchmark input, the function and its HTTP trigger.

        Args:
            sebs_client: The SeBS client to use
            deployment_client: The deployment client to use
        """
        try:
            import mcp  # noqa: F401
        except ImportError:
            raise RuntimeError(
                "The agent-sessions experiment needs the MCP client, install the agents extra."
            ) from None

        settings = self.config.experiment_settings(self.name())
        self._benchmark = sebs_client.get_benchmark(
            settings["benchmark"], deployment_client, self.config
        )
        self._benchmark_input: dict = self._benchmark.prepare_input(
            deployment_client.system_resources,
            size=settings["input-size"],
            replace_existing=self.config.update_storage,
        )
        self._function = deployment_client.get_function(self._benchmark)

        triggers = self._function.triggers(Trigger.TriggerType.HTTP)
        if len(triggers) == 0:
            self._trigger = deployment_client.create_trigger(
                self._function, Trigger.TriggerType.HTTP
            )
        else:
            self._trigger = triggers[0]

        self._out_dir = os.path.join(sebs_client.output_dir, "agent-sessions")
        os.makedirs(self._out_dir, exist_ok=True)
        self._deployment_client = deployment_client

    def run(self) -> None:
        """Run the sessions of each client mode and store one result file per mode."""
        settings = self.config.experiment_settings(self.name())
        repetitions = settings["repetitions"]

        for mode in settings["modes"]:
            self._result = ExperimentResult(self.config, self._deployment_client.config)
            self._result.begin()
            sessions = []
            for i in range(repetitions):
                try:
                    session = asyncio.run(self._session(mode))
                except Exception as e:
                    # The MCP client reports a failed request inside nested task groups.
                    error: BaseException = e
                    while len(getattr(error, "exceptions", ())) == 1:
                        error = getattr(error, "exceptions")[0]
                    self.logging.error(
                        f"Session {i + 1}/{repetitions} in mode {mode} failed: {error}"
                    )
                    session = {
                        "mode": mode,
                        "error": str(error),
                        "invocations": self._session_invocations,
                    }
                else:
                    self.logging.info(
                        f"Session {i + 1}/{repetitions} in mode {mode}: "
                        f"{len(session['invocations'])} invocations, "
                        f"{session['tool_calls']} tool calls, {session['time'] / 1000:.0f} ms"
                    )
                sessions.append(session)
            self._result.end()

            with open(os.path.join(self._out_dir, f"{mode}_results.json"), "w") as out_f:
                out_f.write(
                    serialize({**json.loads(serialize(self._result)), "sessions": sessions})
                )

    async def _session(self, mode: str) -> dict:
        """Run one agent session: list the tools, then read the tool's whole output.

        Args:
            mode: Client mode, ``legacy``, ``auto`` or a protocol version

        Returns:
            The session's duration, number of tool calls and its invocations
        """
        import httpx2
        from mcp.client.client import Client
        from mcp.client.streamable_http import streamable_http_client
        from mcp.types import TextContent

        self._session_invocations: List[dict] = []
        tool = self._benchmark_input["request"]["body"]["params"]
        arguments = dict(tool["arguments"])
        tool_calls = 0

        begin = datetime.now()
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(self._invoke)) as http:
            async with Client(
                streamable_http_client(SERVER_URL, http_client=http), mode=mode
            ) as client:
                await client.list_tools()
                while True:
                    output = await client.call_tool(tool["name"], arguments)
                    tool_calls += 1
                    text = "".join(c.text for c in output.content if isinstance(c, TextContent))
                    if output.is_error:
                        raise RuntimeError(f"Tool {tool['name']} failed: {text}")
                    continuation = CONTINUATION.search(text)
                    if continuation is None:
                        break
                    arguments["start_index"] = int(continuation.group(1))
        end = datetime.now()

        return {
            "mode": mode,
            "begin": begin.timestamp(),
            "time": (end - begin).total_seconds() * 1e6,
            "tool_calls": tool_calls,
            "invocations": self._session_invocations,
        }

    async def _invoke(self, request: "httpx2.Request") -> "httpx2.Response":
        """Send one HTTP request of the MCP client as a function invocation.

        Args:
            request: Request of the MCP client

        Returns:
            The MCP server's response, rebuilt from the function's output
        """
        import httpx2

        body = json.loads(request.content)
        headers = {k: v for k, v in request.headers.items() if k not in DROPPED_HEADERS}
        payload = {**self._benchmark_input, "request": {"headers": headers, "body": body}}

        ret = await asyncio.to_thread(self._trigger.sync_invoke, payload)
        self._result.add_invocation(self._function, ret)
        output = ret.output["result"]
        response = output["result"]

        invocation = {
            "request_id": ret.request_id,
            "method": body.get("method"),
            "status": response["status"],
            "is_cold": ret.stats.cold_start,
            "client_time": ret.times.client,
            "measurement": output["measurement"],
        }
        if body.get("method") == "tools/call":
            invocation["start_index"] = body["params"]["arguments"].get("start_index", 0)
        self._session_invocations.append(invocation)

        content = response["body"]
        if not isinstance(content, str):
            content = json.dumps(content)
        headers = {
            k: v
            for k, v in response["headers"].items()
            if k not in ("content-length", "content-encoding")
        }
        return httpx2.Response(
            response["status"], headers=headers, content=content.encode(), request=request
        )

    def process(
        self,
        sebs_client: "SeBS",
        deployment_client: FaaSSystem,
        directory: str,
        logging_filename: str,
        extend_time_interval: int,
    ) -> None:
        """Write one CSV row per invocation of every session.

        Args:
            sebs_client: The SeBS client to use
            deployment_client: The deployment client to use
            directory: Directory where results are stored
            logging_filename: Filename for logs
            extend_time_interval: Not used by this experiment
        """
        out_dir = os.path.join(directory, "agent-sessions")
        measurements = [
            "request_time",
            "download_time",
            "compute_time",
            "protocol_time",
            "import_time",
            "server_init_time",
        ]
        fields = [
            "mode",
            "session",
            "invocation",
            "method",
            "start_index",
            "status",
            "is_cold",
            "client_time",
            *measurements,
        ]
        rows: List[dict] = []
        for result_file in sorted(glob.glob(os.path.join(out_dir, "*_results.json"))):
            with open(result_file) as in_f:
                sessions = json.load(in_f)["sessions"]
            for session_idx, session in enumerate(sessions):
                for idx, invocation in enumerate(session["invocations"]):
                    measurement = invocation["measurement"]
                    rows.append(
                        {
                            "mode": session["mode"],
                            "session": session_idx,
                            "invocation": idx,
                            "method": invocation["method"],
                            "start_index": invocation.get("start_index", ""),
                            "status": invocation["status"],
                            "is_cold": invocation["is_cold"],
                            "client_time": invocation["client_time"],
                            **{field: measurement.get(field, "") for field in measurements},
                        }
                    )

        with open(os.path.join(out_dir, "agent_sessions.csv"), "w") as out_f:
            writer = csv.DictWriter(out_f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        self.logging.info(f"Wrote {len(rows)} invocations to {out_f.name}")
