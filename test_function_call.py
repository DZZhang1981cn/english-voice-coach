#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""临时验证脚本：百炼 qwen-audio-3.0-realtime-plus 的 function calling 是否可用。

用文本输入触发模型，观察是否会返回 response.function_call_arguments.done。
"""

import json
import subprocess
import websocket

MODEL = "qwen-audio-3.0-realtime-plus"
WS_URL = (
    "wss://token-plan.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime?model=" + MODEL
)
KEYCHAIN_SERVICE = "bailian-token-plan-key"


def load_key():
    r = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise SystemExit("无法读取 Keychain key")
    return r.stdout.strip()


TOOLS = [
    {
        "type": "function",
        "name": "log_correction",
        "description": "Record a mistake the student just made.",
        "parameters": {
            "type": "object",
            "properties": {
                "original": {"type": "string"},
                "corrected": {"type": "string"},
                "error_type": {"type": "string", "enum": ["grammar", "vocabulary", "pronunciation"]},
            },
            "required": ["original", "corrected"],
        },
    },
    {
        "type": "function",
        "name": "log_assessment",
        "description": "Rate the student's spoken English.",
        "parameters": {
            "type": "object",
            "properties": {
                "grammar_score": {"type": "number"},
                "fluency_score": {"type": "number"},
                "vocabulary_score": {"type": "number"},
                "comment": {"type": "string"},
            },
            "required": ["grammar_score", "fluency_score", "vocabulary_score"],
        },
    },
]


def main():
    key = load_key()
    ws = websocket.create_connection(
        WS_URL, header=["Authorization: Bearer " + key], timeout=30
    )

    # 等 session.created
    while True:
        ev = json.loads(ws.recv())
        if ev.get("type") == "session.created":
            print("session.created OK")
            break
        if ev.get("type") == "error":
            raise SystemExit(f"连接失败: {ev}")

    instructions = (
        "You are an English coach. When the student makes a mistake, call log_correction. "
        "After the student's turn, call log_assessment with scores."
    )
    ws.send(json.dumps({
        "type": "session.update",
        "session": {
            "modalities": ["text", "audio"],
            "instructions": instructions,
            "tools": TOOLS,
            "turn_detection": {"type": "server_vad"},
        },
    }))
    print("已发送 session.update（含 tools）")

    # 发文本输入，触发模型回复（必须包成 message + role=user）
    ws.send(json.dumps({
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "I meet a customer yesterday and we talk about tire price."}],
        },
    }))
    ws.send(json.dumps({"type": "response.create"}))
    print("已发送文本 + response.create，等待回复…\n")

    # 收事件
    got_call = False
    while True:
        ev = json.loads(ws.recv())
        t = ev.get("type")
        if t == "response.function_call_arguments.done":
            got_call = True
            print(">>> FUNCTION_CALL:", ev.get("name"))
            print("    arguments:", ev.get("arguments"))
        elif t == "response.done":
            print("response.done")
            break
        elif t == "error":
            print("ERROR:", json.dumps(ev, ensure_ascii=False))
            break

    ws.close()
    print("\n结论:", "✅ function calling 可用" if got_call else "❌ 未收到 function_call 事件")


if __name__ == "__main__":
    main()
