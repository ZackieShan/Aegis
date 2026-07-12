"""
image_gen_server.py

MCP server exposing image generation via OpenAI-compatible APIs.
"""

import asyncio
import base64
import sys
import uuid
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.constants import GENERATED_IMAGES_DIR

server = Server("image_gen")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="generate_image",
            description="Generate an image using an image-capable model (e.g. gpt-image-1)",
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Image description prompt"},
                    "model": {"type": "string", "description": "Model name (auto-detects if omitted)"},
                    "size": {"type": "string", "description": "Image size (default 1024x1024)"},
                    "quality": {"type": "string", "description": "Quality: low, medium, high, auto (default medium)"},
                },
                "required": ["prompt"],
            },
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name != "generate_image":
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    prompt = arguments.get("prompt", "")
    model_spec = arguments.get("model", "")
    size = arguments.get("size", "")
    quality = arguments.get("quality", "")

    if not prompt:
        return [TextContent(type="text", text="Error: Image prompt is required")]

    # Delegate to the single in-process implementation so this MCP surface can
    # never drift from the agent's path (size caps, timeout, autodetect
    # fallback, SSRF-check, DALL-E URL localization, gallery save). owner is
    # None here — this stdio subprocess has no request context — so gallery
    # rows from a DIRECT MCP call aren't owner-scoped; the agent's own tool
    # calls go through do_generate_image directly (with owner), not this path.
    try:
        from src.settings import get_setting
        from src.ai_interaction import do_generate_image
        if not get_setting("image_gen_enabled", True):
            return [TextContent(type="text", text="Error: Image generation is disabled by the administrator.")]
        _content = "\n".join([prompt, model_spec, size, quality]).rstrip("\n")
        _res = await do_generate_image(_content)
        if _res.get("error"):
            return [TextContent(type="text", text=f"Error: {_res['error']}")]
        _url = _res.get("image_url", "")
        # _res["results"] carries any fallback note ("configured model
        # unavailable — used X") — surface it so the user learns their setting
        # is broken.
        _first = _res.get("results") or f"Generated image for: {prompt[:100]}"
        _text = (
            f"{_first}\n"
            f"Direct link: {_url}\n"
            f"model: {_res.get('image_model', '')}\nsize: {_res.get('image_size', '')}"
        )
        return [TextContent(type="text", text=_text)]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


async def run():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
