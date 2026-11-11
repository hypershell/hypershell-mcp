# AGENTS.md

This file provides guidance to WARP (warp.dev) when working with code in this repository.

## Project Overview

**hypershell-mcp** is an MCP (Model Context Protocol) server that enables agentic development workflows with HyperShell. HyperShell is a distributed task execution system for high-performance computing (HPC) that processes shell commands over a distributed, asynchronous queue.

The MCP server exposes HyperShell functionality through the Model Context Protocol, allowing AI agents to:
- Submit and manage distributed computing tasks
- Monitor task execution and query task history
- Configure HyperShell settings
- Manage the task database

### Related Projects
- **HyperShell**: The parent project at `/Users/geoffrey/Software/github.com/hypershell/hypershell`
- **HyperShell Documentation**: https://hypershell.readthedocs.io
- **FastMCP Documentation**: https://gofastmcp.com

## Development Setup

### Python Version
This project requires **Python 3.11+** (specified in `pyproject.toml`)

### Environment Setup
```bash
# Source .env for build dependencies (PostgreSQL path)
source .env

# Install dependencies and sync environment (creates .venv automatically)
uv sync

# Or activate the environment manually
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

### Running the Server
```bash
uv run hypershell-mcp                    # Run with stdio transport (default)
uv run hypershell-mcp -t sse -p 8080     # Run SSE server on port 8080
uv run hypershell-mcp -t http -H 0.0.0.0 # Run HTTP server on all interfaces
uv run hypershell-mcp --help             # Show all options
```

### Running Tests
```bash
uv run pytest                    # Run all tests
uv run pytest -m unit            # Run unit tests only
uv run pytest -m integration     # Run integration tests only
uv run pytest -v                 # Verbose output
```

### Building
```bash
uv build
```

## Architecture

### Package Structure
```
src/hypershell_mcp/
├── __init__.py      # Package exports and main() entry point
└── server.py        # FastMCP server with all tools and resources
```

### MCP Server Implementation
The server is built using FastMCP and exposes:

**Tools** (actions agents can perform):
- Configuration: `get_config`, `set_config`, `get_config_path`
- Database: `init_database`, `get_database_stats`, `vacuum_database`, `truncate_database`
- Task submission: `submit_task`, `submit_tasks`
- Task queries: `get_task_info`, `list_tasks`, `get_task_output`
- Task management: `cancel_task`, `cancel_tasks`, `revert_task`, `revert_interrupted_tasks`, `update_task_tags`

**Resources** (read-only data):
- `hypershell://config` - Current configuration
- `hypershell://stats` - Database statistics
- `hypershell://version` - Version information

### Key HyperShell Concepts
- **Tasks**: Shell commands with UUID, tags, and execution metadata
- **Groups**: Task ordering mechanism (lower groups execute first)
- **Tags**: Key-value pairs for filtering and organizing tasks
- **Database**: SQLite (default) or PostgreSQL for task persistence

### HyperShell Python API
When extending tools, use these HyperShell modules:
- `hypershell.core.config` - Configuration management
- `hypershell.data` - Database operations (`initdb`, `ensuredb`, `vacuumdb`)
- `hypershell.data.model.Task` - Task model with query methods
- `hypershell.submit` - Task submission (`submit_from`)

## Code Quality

### Style and Conventions
- Use type hints for all function signatures
- Document tools with docstrings (FastMCP uses these for tool descriptions)
- Return dictionaries from tools (FastMCP handles serialization)
- Use `ensuredb()` before any database operations

### Adding New Tools
```python
@mcp.tool
def my_tool(arg1: str, arg2: int = 10) -> Dict[str, Any]:
    """
    Tool description shown to agents.
    
    Args:
        arg1: Description of arg1.
        arg2: Description of arg2.
    
    Returns:
        Description of return value.
    """
    ensuredb()
    # Implementation
    return {"status": "success", "result": ...}
```

## Important Context

### Cross-Platform Considerations
HyperShell runs on Linux, macOS, and Windows. MCP tools should handle platform differences.

### Security
- The server currently runs without authentication (local development only)
- Bearer token authentication middleware will be added for production
- Be cautious with database operations (truncate requires confirmation)
