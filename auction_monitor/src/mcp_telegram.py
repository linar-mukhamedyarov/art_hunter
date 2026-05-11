#!/usr/bin/env python3
"""
MCP server: Telegram notifications.
Implements JSON-RPC 2.0 over stdio — Claude CLI spawns this as a subprocess.
"""
import sys
import json
import os
import re
import requests

# MCP requires UTF-8 on stdout
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8")


TOOL_DEF = {
    "name": "send_telegram_message",
    "description": "Send a notification message via Telegram bot",
    "inputSchema": {
        "type": "object",
        "properties": {
            "chat_id": {
                "type": "string",
                "description": "Telegram chat ID (user or group)",
            },
            "text": {
                "type": "string",
                "description": "Message text. Emojis supported, plain text.",
            },
        },
        "required": ["chat_id", "text"],
    },
}


def write_response(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def ok(req_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def err(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def strip_markdown(text: str) -> str:
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)          # ### заголовки
    text = re.sub(r"\[([^\]]+)\]\(([^\)]+)\)", r"\1 (\2)", text)        # [текст](url) → текст (url)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)                 # **жирный**
    text = re.sub(r"\*(.+?)\*", r"\1", text)                     # *курсив*
    text = re.sub(r"`(.+?)`", r"\1", text)                       # `код`
    text = re.sub(r"^-{3,}\s*$", "", text, flags=re.MULTILINE)   # --- линии
    text = re.sub(r"\n{3,}", "\n\n", text)                       # лишние пустые строки
    return text.strip()


def handle_tools_call(req_id, params: dict) -> dict:
    tool_name = params.get("name", "")
    args = params.get("arguments", {})

    if tool_name != "send_telegram_message":
        return err(req_id, -32601, f"Unknown tool: {tool_name}")

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return ok(req_id, {
            "content": [{"type": "text", "text": "ERROR: TELEGRAM_BOT_TOKEN not set"}],
            "isError": True,
        })

    chat_id = str(args.get("chat_id", "")).strip()
    text = strip_markdown(str(args.get("text", "")))

    if not chat_id or not text:
        return ok(req_id, {
            "content": [{"type": "text", "text": "ERROR: chat_id and text are required"}],
            "isError": True,
        })

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
        data = resp.json()
        if data.get("ok"):
            return ok(req_id, {
                "content": [{"type": "text", "text": "Message sent successfully"}]
            })
        desc = data.get("description", "Unknown Telegram error")
        return ok(req_id, {
            "content": [{"type": "text", "text": f"Telegram API error: {desc}"}],
            "isError": True,
        })
    except requests.RequestException as exc:
        return ok(req_id, {
            "content": [{"type": "text", "text": f"Network error: {exc}"}],
            "isError": True,
        })


def main() -> None:
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue

        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            write_response(err(None, -32700, "Parse error"))
            continue

        req_id = req.get("id")  # None for notifications
        method = req.get("method", "")
        params = req.get("params", {})

        # Notifications have no id — don't respond
        if req_id is None:
            continue

        if method == "initialize":
            write_response(ok(req_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "telegram-mcp", "version": "1.0.0"},
            }))

        elif method == "tools/list":
            write_response(ok(req_id, {"tools": [TOOL_DEF]}))

        elif method == "tools/call":
            write_response(handle_tools_call(req_id, params))

        elif method == "ping":
            write_response(ok(req_id, {}))

        else:
            write_response(err(req_id, -32601, f"Method not found: {method}"))


if __name__ == "__main__":
    main()
