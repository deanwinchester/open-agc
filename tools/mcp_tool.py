import os
import asyncio
import threading
import concurrent.futures
from typing import Dict, Any, List, Optional
from pydantic import Field
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from tools.base import BaseTool

class MCPToolWrapper(BaseTool):
    model_config = {"extra": "allow", "arbitrary_types_allowed": True}
    
    name: str = ""
    description: str = ""
    
    def __init__(self, mcp_server_name: str, tool_name: str, description: str, input_schema: dict, mcp_manager: Any, **kwargs):
        super().__init__(**kwargs)
        # Combine server name and tool name to avoid conflicts, e.g., "sqlite_query"
        self.name = f"{mcp_server_name}_{tool_name}"
        self.description = description
        self.input_schema = input_schema
        self.tool_name = tool_name
        self.mcp_server_name = mcp_server_name
        self.mcp_manager = mcp_manager
        
    def get_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema
            }
        }
        
    def execute(self, **kwargs) -> Any:
        return self.mcp_manager.call_tool_sync(self.mcp_server_name, self.tool_name, kwargs)

class MCPClientManager:
    """
    Manages connections to multiple MCP (Model Context Protocol) servers in a background asyncio event loop.
    Exposes synchronous methods to the rest of the Open-AGC application.
    """
    def __init__(self):
        self.servers: Dict[str, dict] = {}
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        
        # Keep track of active sessions and contexts
        self._sessions: Dict[str, ClientSession] = {}
        self._contexts: Dict[str, AsyncExitStack] = {}
        self._tools: Dict[str, BaseTool] = {}
        
    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def load_servers(self, mcp_servers_config: dict) -> Dict[str, BaseTool]:
        """
        Connect to the given MCP servers, load their tools, and return them.
        This is a synchronous blocking call. Existing sessions are kept only
        if they are still alive; dead sessions are torn down and reconnected.
        """
        if not mcp_servers_config:
            return {}

        future = asyncio.run_coroutine_threadsafe(self._async_load_servers(mcp_servers_config), self._loop)
        try:
            return future.result(timeout=30.0) # Wait up to 30s for MCP servers to boot
        except Exception as e:
            print(f"[MCPClientManager] Error loading MCP servers: {e}")
            return self._tools

    async def _async_load_servers(self, config: dict) -> Dict[str, BaseTool]:
        # Remember configs so timed-out/dead sessions can be rebuilt later.
        self.servers.update(config)
        for name, server_cfg in config.items():
            if name in self._sessions:
                if await self._session_alive(name):
                    continue
                # Dead session — tear it down and reconnect below.
                print(f"[MCPClientManager] Session for '{name}' is dead, reconnecting...")
                await self._teardown_session(name)
            await self._connect_one(name, server_cfg)
        return self._tools

    async def _session_alive(self, name: str) -> bool:
        """Liveness probe: ping the server, with a short timeout."""
        session = self._sessions.get(name)
        if session is None:
            return False
        try:
            await asyncio.wait_for(session.send_ping(), timeout=5.0)
            return True
        except Exception:
            return False

    async def _teardown_session(self, name: str):
        """Close the exit stack and drop session/tools for one server."""
        self._sessions.pop(name, None)
        # Drop wrapped tools belonging to this server
        self._tools = {
            k: v for k, v in self._tools.items()
            if getattr(v, "mcp_server_name", None) != name
        }
        stack = self._contexts.pop(name, None)
        if stack is not None:
            try:
                await stack.aclose()
            except Exception as e:
                print(f"[MCPClientManager] Error closing session '{name}': {e}")

    async def _connect_one(self, name: str, server_cfg: dict):
        """Start one MCP server, create its session and wrap its tools."""
        command = server_cfg.get("command")
        args = server_cfg.get("args", [])
        env = server_cfg.get("env")

        if not command:
            return

        server_params = StdioServerParameters(
            command=command,
            args=args,
            env={**os.environ, **env} if env else None
        )

        print(f"[MCPClientManager] Starting MCP server '{name}' via command: {command} {' '.join(args)}")
        try:
            stack = AsyncExitStack()
            read, write = await stack.enter_async_context(stdio_client(server_params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()

            self._contexts[name] = stack
            self._sessions[name] = session

            # List tools and wrap them
            result = await session.list_tools()
            for tool in result.tools:
                tool_wrapper = MCPToolWrapper(
                    mcp_server_name=name,
                    tool_name=tool.name,
                    description=tool.description or f"MCP tool {tool.name}",
                    input_schema=tool.inputSchema,
                    mcp_manager=self
                )
                self._tools[tool_wrapper.name] = tool_wrapper

            print(f"[MCPClientManager] Successfully connected to '{name}'. Loaded {len(result.tools)} tools.")
        except Exception as e:
            print(f"[MCPClientManager] Failed to load MCP server '{name}': {e}")

    def _reset_session(self, server_name: str):
        """Tear down a (possibly hung) session and reconnect if config is known."""
        future = asyncio.run_coroutine_threadsafe(
            self._async_reset_session(server_name), self._loop
        )
        try:
            future.result(timeout=30.0)
        except Exception as e:
            print(f"[MCPClientManager] Session reset for '{server_name}' failed: {e}")

    async def _async_reset_session(self, name: str):
        await self._teardown_session(name)
        cfg = self.servers.get(name)
        if cfg:
            await self._connect_one(name, cfg)

    def call_tool_sync(self, server_name: str, tool_name: str, arguments: dict,
                       timeout: float = 120.0) -> Any:
        future = asyncio.run_coroutine_threadsafe(
            self._async_call_tool(server_name, tool_name, arguments),
            self._loop
        )
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            # The server is hung. Cancel the coroutine and rebuild the session
            # so the next call gets a fresh connection instead of a dead one.
            future.cancel()
            print(f"[MCPClientManager] Tool '{tool_name}' on '{server_name}' timed out "
                  f"after {timeout}s; resetting session.")
            self._reset_session(server_name)
            return (f"Error: MCP tool '{tool_name}' on server '{server_name}' timed out "
                    f"after {int(timeout)}s. The session has been reset — please retry.")
        except Exception as e:
            return f"Error executing MCP tool: {e}"
        
    async def _async_call_tool(self, server_name: str, tool_name: str, arguments: dict) -> Any:
        session = self._sessions.get(server_name)
        if not session:
            return f"Error: MCP Server '{server_name}' not connected."
            
        try:
            result = await session.call_tool(tool_name, arguments)
            
            # Result is usually a list of content objects
            output_texts = []
            for content in result.content:
                if content.type == "text":
                    output_texts.append(content.text)
                else:
                    output_texts.append(str(content))
                    
            if result.isError:
                return f"MCP Tool Error: {''.join(output_texts)}"
                
            return "\n".join(output_texts) if output_texts else "Success (no output)"
        except Exception as e:
            return f"Error executing tool '{tool_name}' on server '{server_name}': {str(e)}"

_global_mcp_manager = None

def get_mcp_manager() -> MCPClientManager:
    global _global_mcp_manager
    if _global_mcp_manager is None:
        _global_mcp_manager = MCPClientManager()
    return _global_mcp_manager
