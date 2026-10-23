#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""私人英语教练 —— 阶段1：注入人设 + 本地知识库。

底座：阿里云百炼 Token Plan 实时语音模型 qwen-audio-3.0-realtime-plus
协议：WebSocket（OpenAI Realtime 兼容协议，server_vad 自动断句）
输入：麦克风 PCM 16kHz / 16bit / 单声道
输出：扬声器 PCM 24kHz / 16bit / 单声道
人设/知识库：启动时读取同目录下的 人设.md 与 知识库.md，注入 instructions。

运行：python3 stage0_realtime.py   （对着麦克风说话，Ctrl+C 退出）
"""

import base64
import json
import pathlib
import subprocess
import threading

import numpy as np
import sounddevice as sd
import websocket

# ---- 配置 ----
MODEL = "qwen-audio-3.0-realtime-plus"
WS_URL = (
    "wss://token-plan.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime"
    "?model=" + MODEL
)
KEYCHAIN_SERVICE = "bailian-token-plan-key"  # API key 存 macOS Keychain，不硬编码
VOICE = "daniel"    # 英语音色；美音 Jennifer/Aiden 本模型不支持，可换 hannah/sherry/echo

BASE_DIR = pathlib.Path(__file__).resolve().parent
PERSONA_FILE = BASE_DIR / "人设.md"
KNOWLEDGE_FILE = BASE_DIR / "知识库.md"

SR_IN = 16000       # 输入采样率（麦克风）
SR_OUT = 24000      # 输出采样率（扬声器）
CHUNK_FRAMES = 1600  # 每次送 100ms 音频 = 1600 samples = 3200 bytes（16bit）


def load_key() -> str:
    """从 macOS Keychain 读取 API key，绝不硬编码。"""
    r = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise SystemExit(f"无法从 Keychain 读取 API key（service={KEYCHAIN_SERVICE}）")
    return r.stdout.strip()


def load_instructions() -> str:
    """读取人设 + 知识库文件，拼接成 system instructions。"""
    persona = PERSONA_FILE.read_text(encoding="utf-8").strip()
    knowledge = KNOWLEDGE_FILE.read_text(encoding="utf-8").strip()
    return persona + "\n\n" + knowledge


def recv_loop(ws, out_stream: sd.OutputStream):
    """主线程：接收服务端事件 → 播放 AI 语音 + 打印转写。"""
    while True:
        ev = json.loads(ws.recv())
        t = ev.get("type")

        if t == "response.audio.delta":
            # AI 回复的音频块（PCM 24kHz int16），直接喂给扬声器
            pcm = base64.b64decode(ev["delta"])
            out_stream.write(np.frombuffer(pcm, dtype="int16"))

        elif t == "conversation.item.input_audio_transcription.completed":
            # 用户说的话（服务端 fun-asr 转写）
            print(f"[你] {ev.get('transcript', '')}")

        elif t == "response.audio_transcript.done":
            # AI 说的话（转写）
            print(f"[教练] {ev.get('transcript', '')}")

        elif t == "error":
            print(f"[错误] {json.dumps(ev.get('error', {}), ensure_ascii=False)}")


def main():
    key = load_key()
    ws = websocket.create_connection(
        WS_URL, header=["Authorization: Bearer " + key], timeout=30
    )

    # 等 session.created
    while True:
        ev = json.loads(ws.recv())
        if ev.get("type") == "session.created":
            print("已连接，会话建立。")
            break
        if ev.get("type") == "error":
            raise SystemExit(f"连接失败：{ev}")

    # 配置会话：双向语音 + 人设 + 知识库 + 服务端 VAD 自动断句
    instructions = load_instructions()
    ws.send(json.dumps({
        "type": "session.update",
        "session": {
            "modalities": ["text", "audio"],
            "instructions": instructions,
            "voice": VOICE,
            "turn_detection": {"type": "server_vad", "threshold": 0.5,
                               "silence_duration_ms": 800},
        },
    }))
    print(f"已注入人设 + 知识库（音色 {VOICE}）。")

    stop = threading.Event()

    def sender():
        """子线程：持续录音并推送给服务端。"""
        with sd.InputStream(samplerate=SR_IN, channels=1, dtype="int16",
                            blocksize=CHUNK_FRAMES) as stream:
            print("开始录音，请对着麦克风说话（Ctrl+C 退出）…")
            while not stop.is_set():
                data, overflowed = stream.read(CHUNK_FRAMES)
                if overflowed:
                    continue
                ws.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(data.tobytes()).decode("ascii"),
                }))

    t = threading.Thread(target=sender, daemon=True)
    with sd.OutputStream(samplerate=SR_OUT, channels=1, dtype="int16") as out:
        t.start()
        try:
            recv_loop(ws, out)
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            ws.close()
    print("\n对话结束。")


if __name__ == "__main__":
    main()
