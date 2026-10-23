#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段1 验证：真实人设 + 知识库注入是否生效（与主脚本同一套文件）。

读取 人设.md + 知识库.md，用商务轮胎场景音频测试三个维度：
  1. 是否用英语回复（人设生效）
  2. 是否有教练纠错口吻（人设生效）
  3. 是否结合轮胎/商务背景（知识库生效）
用法：python3 verify_instructions.py <16k.wav>
"""

import base64
import json
import pathlib
import subprocess
import sys
import wave

import websocket

MODEL = "qwen-audio-3.0-realtime-plus"
WS_URL = ("wss://token-plan.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime?model=" + MODEL)
KEYCHAIN_SERVICE = "bailian-token-plan-key"
VOICE = "daniel"
CHUNK_BYTES = 3200  # 100ms @ 16kHz 16bit mono

BASE_DIR = pathlib.Path(__file__).resolve().parent
PERSONA_FILE = BASE_DIR / "人设.md"
KNOWLEDGE_FILE = BASE_DIR / "知识库.md"


def load_key() -> str:
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"Keychain 无 key（service={KEYCHAIN_SERVICE}）")
    return r.stdout.strip()


def load_instructions() -> str:
    persona = PERSONA_FILE.read_text(encoding="utf-8").strip()
    knowledge = KNOWLEDGE_FILE.read_text(encoding="utf-8").strip()
    return persona + "\n\n" + knowledge


def main():
    wav_path = sys.argv[1]
    with wave.open(wav_path, "rb") as w:
        assert w.getframerate() == 16000, f"需 16kHz，实际 {w.getframerate()}"
        assert w.getsampwidth() == 2, "需 16bit"
        assert w.getnchannels() == 1, "需 mono"
        pcm = w.readframes(w.getnframes())

    key = load_key()
    ws = websocket.create_connection(WS_URL, header=["Authorization: Bearer " + key], timeout=30)

    while True:
        ev = json.loads(ws.recv())
        if ev.get("type") == "session.created":
            print("✅ session.created")
            break
        if ev.get("type") == "error":
            raise SystemExit(f"连接错误：{ev}")

    # 注入真实人设 + 知识库 + 音色
    ws.send(json.dumps({"type": "session.update", "session": {
        "modalities": ["text", "audio"],
        "instructions": load_instructions(),
        "voice": VOICE,
        "turn_detection": {"type": "server_vad", "threshold": 0.5, "silence_duration_ms": 800},
    }}))

    n = 0
    for off in range(0, len(pcm), CHUNK_BYTES):
        chunk = pcm[off:off + CHUNK_BYTES]
        ws.send(json.dumps({"type": "input_audio_buffer.append",
                            "audio": base64.b64encode(chunk).decode("ascii")}))
        n += 1
    print(f"已推送音频 {n} 块")

    silence = b"\x00\x00" * (CHUNK_BYTES // 2)
    for _ in range(25):
        ws.send(json.dumps({"type": "input_audio_buffer.append",
                            "audio": base64.b64encode(silence).decode("ascii")}))

    user_text = ""
    ai_text = ""
    ws.settimeout(20)
    try:
        while True:
            ev = json.loads(ws.recv())
            t = ev.get("type")
            if t == "conversation.item.input_audio_transcription.completed":
                user_text = ev.get("transcript", "")
                print(f"  [你] {user_text}")
            elif t == "response.audio_transcript.done":
                ai_text = ev.get("transcript", "")
                print(f"  [教练] {ai_text}")
            elif t == "response.done":
                print("  ✅ response.done")
                break
            elif t == "error":
                print(f"  ❌ error: {ev}")
                break
    except websocket.WebSocketTimeoutException:
        print("  ⚠️ 等待回复超时")

    ws.close()

    # 三维判定
    lowered = ai_text.lower()
    english_only = not any('\u4e00' <= c <= '\u9fff' for c in ai_text)
    coach_tone = any(k in lowered for k in
                     ("correct", "should", "grammar", "past tense", "went", "met",
                      "talked", "try again", "coach", "good job", "well done"))
    biz_aware = any(k in lowered for k in
                    ("tire", "business", "sales", "customer", "client", "price", "marketing"))

    print(f"\n判定：英语回复={english_only} / 教练纠错={coach_tone} / 商务轮胎背景={biz_aware}")
    if english_only and coach_tone and biz_aware:
        print("✅ 阶段1 人设 + 知识库注入全部生效")
    else:
        print("⚠️ 有维度未达标，检查人设/知识库文件内容或测试音频")


if __name__ == "__main__":
    main()
