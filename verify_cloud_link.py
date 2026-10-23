#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段0 云端链路验证：本地 WAV(16kHz) → WebSocket → 收 AI 语音回复 + 转写。

不依赖麦克风/扬声器，纯验证百炼实时语音的双向音频链路是否通畅。
用法：python3 verify_cloud_link.py <16k.wav>
"""

import base64
import json
import subprocess
import sys
import wave

import websocket

MODEL = "qwen-audio-3.0-realtime-plus"
WS_URL = ("wss://token-plan.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime?model=" + MODEL)
KEYCHAIN_SERVICE = "bailian-token-plan-key"
CHUNK_BYTES = 3200  # 100ms @ 16kHz 16bit mono


def load_key() -> str:
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"Keychain 无 key（service={KEYCHAIN_SERVICE}）")
    return r.stdout.strip()


def main():
    wav_path = sys.argv[1]
    with wave.open(wav_path, "rb") as w:
        assert w.getframerate() == 16000, f"需 16kHz，实际 {w.getframerate()}"
        assert w.getsampwidth() == 2, "需 16bit"
        assert w.getnchannels() == 1, "需 mono"
        pcm = w.readframes(w.getnframes())

    key = load_key()
    ws = websocket.create_connection(WS_URL, header=["Authorization: Bearer " + key], timeout=30)

    # 等 session.created
    while True:
        ev = json.loads(ws.recv())
        if ev.get("type") == "session.created":
            print("✅ session.created")
            break
        if ev.get("type") == "error":
            raise SystemExit(f"连接错误：{ev}")

    ws.send(json.dumps({"type": "session.update", "session": {
        "modalities": ["text", "audio"],
        "turn_detection": {"type": "server_vad", "threshold": 0.5, "silence_duration_ms": 800},
    }}))

    # 推送真实音频
    n = 0
    for off in range(0, len(pcm), CHUNK_BYTES):
        chunk = pcm[off:off + CHUNK_BYTES]
        ws.send(json.dumps({"type": "input_audio_buffer.append",
                            "audio": base64.b64encode(chunk).decode("ascii")}))
        n += 1
    print(f"已推送真实音频 {n} 块")

    # 补发静音，让 server_vad 检测到说话结束并触发回复
    silence = b"\x00\x00" * (CHUNK_BYTES // 2)
    for _ in range(25):  # 25 块 ≈ 2.5 秒静音
        ws.send(json.dumps({"type": "input_audio_buffer.append",
                            "audio": base64.b64encode(silence).decode("ascii")}))

    # 收事件
    audio_out = bytearray()
    user_text = ""
    ai_text = ""
    ws.settimeout(20)
    try:
        while True:
            ev = json.loads(ws.recv())
            t = ev.get("type")
            if t == "response.audio.delta":
                audio_out += base64.b64decode(ev["delta"])
            elif t == "conversation.item.input_audio_transcription.completed":
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

    print(f"\n结果：AI 语音 {len(audio_out)} 字节 / 用户转写「{user_text}」/ AI 转写「{ai_text}」")
    if audio_out:
        with wave.open("/tmp/evc_reply_24k.wav", "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
            w.writeframes(bytes(audio_out))
        print("已保存 AI 回复音频 → /tmp/evc_reply_24k.wav")
    print("✅ 云端链路验证通过" if audio_out and ai_text else "❌ 未收到完整回复")


if __name__ == "__main__":
    main()
