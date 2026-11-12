#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""私人英语教练 —— 本地 WebRTC 信令代理 + 静态服务 + 知识库文件夹读取。

解决三件事：
1. 浏览器直连阿里百炼 WebRTC 信令端点有 CORS 限制，本地代理转发 SDP 交换。
2. 知识库从单文件升级为「知识库/」文件夹，自动读取 md/txt/csv/xlsx/pdf 并注入 instructions。
3. 提供浏览器界面（走 WebRTC，浏览器内核自带回声消除 + 降噪）。

运行：python3 server.py
访问：http://127.0.0.1:8766/

知识库更新后刷新浏览器页面即可热加载（/config 每次动态扫描）。
"""

import base64
import csv
import hashlib
import io
import json
import kb_index
import os
import pathlib
import random
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = pathlib.Path(__file__).resolve().parent
PERSONA_FILE = BASE_DIR / "人设.md"
PERSONA_CHAT_FILE = BASE_DIR / "人设_聊天.md"
PERSONA_SPEECH_FILE = BASE_DIR / "人设_演讲.md"
KNOWLEDGE_DIR = BASE_DIR / "知识库"
INDEX_FILE = BASE_DIR / "index.html"
HISTORY_DIR = BASE_DIR / "训练历史"
REPORT_DIR = HISTORY_DIR / "报告"
PROFILE_FILE = BASE_DIR / "个人设置.json"
TOPIC_FILE = BASE_DIR / "话题库.md"

PORT = 8766
MODEL = "qwen-audio-3.0-realtime-plus"
SIGNALING_URL = (
    "https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1/webrtc/realtime"
    "?model=" + MODEL
)
KEYCHAIN_SERVICE = "bailian-token-plan-key"
VOICE = "daniel"
ASSESS_MODEL = "qwen3.5:9b"  # 本地评估模型（思考型，需 think:false 关思考）
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
CLOUD_TEXT_URL = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions"
CLOUD_TEXT_MODEL = "qwen3.8-flash"  # 云端文本生成模型（学习资料，快且质量够）

# 空闲退出：浏览器关闭后心跳停止，空闲超时自动退出（关浏览器即关服务）
IDLE_TIMEOUT = 60  # 秒
_LAST_ACTIVITY = time.time()
_WARMUP_DONE = False  # 首次知识库索引（warmup）完成后置 True，期间不触发空闲退出


def touch():
    global _LAST_ACTIVITY
    _LAST_ACTIVITY = time.time()

def load_key():
    """从 macOS Keychain 读取 API key。缺失时返回 None（不退出，进入设置流程）。"""
    r = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    return r.stdout.strip()


DEFAULT_PROFILE = {
    "english_level": "CET-4（四级，中等水平）",
    "goal": "雅思 6.5（目标 Band）",
    "profession": "轮胎销售/市场（米其林）",
    "hobbies": "爬山、炒股",
}


def load_profile() -> dict:
    """读取个人设置 JSON，不存在或损坏则返回默认。"""
    if PROFILE_FILE.exists():
        try:
            data = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {**DEFAULT_PROFILE, **data}
        except Exception:
            pass
    return dict(DEFAULT_PROFILE)


def save_profile(data: dict) -> None:
    """保存个人设置 JSON（只保留四个已知字段）。"""
    profile = {
        "english_level": (data.get("english_level") or "").strip(),
        "goal": (data.get("goal") or "").strip(),
        "profession": (data.get("profession") or "").strip(),
        "hobbies": (data.get("hobbies") or "").strip(),
    }
    PROFILE_FILE.write_text(
        json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_topics() -> list:
    """解析 话题库.md，返回 [{topic, bullets, part3}]。

    格式：`## ` 开头 = Part 2 话题；紧跟的 `- ` = 提示点；`### Part 3` 后 `- ` = 追问。
    """
    if not TOPIC_FILE.exists():
        return []
    text = TOPIC_FILE.read_text(encoding="utf-8")
    topics = []
    current = None
    section = "bullets"
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            if current:
                topics.append(current)
            current = {"topic": line[3:].strip(), "bullets": [], "part3": []}
            section = "bullets"
        elif line.startswith("### Part 3"):
            section = "part3"
        elif line.startswith("- ") and current is not None:
            item = line[2:].strip()
            if section == "part3":
                current["part3"].append(item)
            else:
                current["bullets"].append(item)
    if current:
        topics.append(current)
    return topics


def build_instructions(mode: str = "ielts") -> str:
    """拼接基础 instructions = 人设（按模式）+ 个人设置（知识库走检索，不在此全量注入）。

    mode: ielts | chat | speech
    """
    if mode == "chat":
        persona = PERSONA_CHAT_FILE.read_text(encoding="utf-8").strip()
    elif mode == "speech":
        persona = PERSONA_SPEECH_FILE.read_text(encoding="utf-8").strip()
    else:
        persona = PERSONA_FILE.read_text(encoding="utf-8").strip()
    profile = load_profile()

    instructions = persona
    instructions += (
        "\n\n# About your student (use this to personalize every lesson)\n"
        f"- English level: {profile.get('english_level', '')}\n"
        f"- Learning goal: {profile.get('goal', '')}\n"
        f"- Profession: {profile.get('profession', '')}\n"
        f"- Hobbies: {profile.get('hobbies', '')}"
    )
    return instructions


def append_log(record: dict):
    """追加一条训练记录到 训练历史/YYYY-MM-DD.jsonl（自动补时间戳）。"""
    HISTORY_DIR.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    path = HISTORY_DIR / f"{today}.jsonl"
    record["ts"] = datetime.now().isoformat(timespec="seconds")
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


ASSESS_PROMPT_CEFR = """You are an English coach evaluating a student's spoken English.

Below is the student's recent conversation with their coach (student + coach turns).

Evaluate the student's performance and output STRICT JSON only:
{
  "cefr_level": "<estimated CEFR level: A1/A2/B1/B2/C1/C2, optionally with +/- like B1+ or B2->",
  "grammar_score": <1-10 integer, grammar accuracy>,
  "fluency_score": <1-10 integer, fluency>,
  "vocabulary_score": <1-10 integer, vocabulary range>,
  "comment": "<one short English sentence of feedback>",
  "corrections": [{"original": "<what the student said wrong>", "corrected": "<correct version>", "error_type": "grammar|vocabulary|pronunciation"}],
  "vocabulary": [{"word": "<useful new word>", "meaning": "<meaning>", "example": "<example sentence>"}]
}
Each score is 1-10. If there are no corrections or new words, output empty arrays.

Conversation:
{dialog}"""


ASSESS_PROMPT = """You are an IELTS Speaking Examiner evaluating a student's spoken English.

Below is the student's spoken answer (the IELTS Part 2 long turn and/or Part 3 discussion).

Score the student on the four official IELTS criteria and output STRICT JSON only:
{
  "fluency_coherence": <0-9 band score, Fluency & Coherence>,
  "lexical_resource": <0-9 band score, Lexical Resource>,
  "grammatical_range_accuracy": <0-9 band score, Grammatical Range & Accuracy>,
  "pronunciation": <0-9 band score, Pronunciation — estimate from the text, e.g. spelling/rhythm cues>,
  "overall_band": <overall band = average of the four, rounded to nearest 0.5>,
  "comment": "<one short English sentence of feedback>",
  "corrections": [{"original": "<what the student said wrong>", "corrected": "<correct version>", "criterion": "fluency_coherence|lexical_resource|grammatical_range_accuracy|pronunciation"}],
  "vocabulary": [{"word": "<useful high-band word or phrase>", "meaning": "<meaning>", "example": "<example sentence>"}]
}
Each band score is 0-9, may include .5. If there are no corrections or new words, output empty arrays.

Student's answer:
{dialog}"""


def _parse_json_response(raw: str) -> dict:
    """从模型输出里安全解析 JSON，容忍 ```json 围栏等包裹。"""
    raw = (raw or "").strip()
    if not raw:
        return {}
    # 剥离 markdown 代码围栏
    if raw.startswith("```"):
        raw = raw.lstrip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
        if raw.endswith("```"):
            raw = raw[:-3].strip()
    try:
        return json.loads(raw)
    except Exception:
        pass
    # 兜底：提取第一个 { 到最后一个 } 之间的内容再试
    s = raw.find("{")
    e = raw.rfind("}")
    if s != -1 and e != -1 and e > s:
        try:
            return json.loads(raw[s:e + 1])
        except Exception:
            pass
    return {}


def assess_dialog(dialog: str, mode: str = "ielts") -> dict:
    """调用本地 Ollama 评估一段对话，返回结构化结果。

    mode: ielts（雅思四维 0-9）| chat（CEFR + 语法/流利/词汇 1-10）
    """
    template = ASSESS_PROMPT_CEFR if mode == "chat" else ASSESS_PROMPT
    prompt = template.replace("{dialog}", dialog)
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps({
            "model": ASSESS_MODEL,
            "prompt": prompt,
            "stream": False,
            "think": False,      # 关思考，否则 content 为空
            "format": "json",    # 强制 JSON 输出
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=300) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return _parse_json_response(data.get("response"))


def warmup_ollama():
    """预热 qwen3.5:9b 评分模型并让它驻留，避免评分冷启动超时。"""
    try:
        req = urllib.request.Request(
            OLLAMA_URL,
            data=json.dumps({
                "model": ASSESS_MODEL,
                "prompt": "Reply with exactly one word: OK",
                "stream": False,
                "think": False,      # 关思考，加快预热
                "keep_alive": -1,    # 数字 -1 = 永久驻留（字符串 "-1" 会被当 duration 报错）
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=300) as resp:
            resp.read()
        print("[预热] 评分模型 qwen3.5:9b 已加载并驻留", flush=True)
    except Exception as e:
        print(f"[预热失败] {e}", flush=True)


SUMMARIZE_PROMPT = """You are a training planner for an English speaking coach.

The student wants to practice spoken English. Below is their requirement (may be in Chinese or English) and any attached file contents.

Summarize this into a clear training brief. Output in Chinese, keep concise, with these sections:
1. 训练主题
2. 训练要点（2-4 条）
3. 建议练习场景

Student's requirement:
{requirement}

Attached files:
{files}"""


def summarize_training(requirement: str, files: str) -> str:
    """调用本地 Ollama 把学生要求 + 文件整理成训练主题要点。"""
    prompt = SUMMARIZE_PROMPT.replace("{requirement}", requirement).replace("{files}", files or "（无）")
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps({
            "model": ASSESS_MODEL,
            "prompt": prompt,
            "stream": False,
            "think": False,
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("response", "")


CRITERION_CN = {
    "fluency_coherence": "流利与连贯",
    "lexical_resource": "词汇资源",
    "grammatical_range_accuracy": "语法",
    "pronunciation": "发音",
}


def build_report(data: dict) -> str:
    """根据会话数据生成雅思口语 Markdown 报告。"""
    lines = []
    lines.append("# 雅思口语训练报告\n")
    lines.append(f"- 会话时间：{data.get('start_time', '')} ~ {data.get('end_time', '')}")

    topic = data.get("topic") or {}
    if topic.get("topic"):
        lines.append(f"- Part 2 话题：{topic.get('topic')}\n")

    dialogue = data.get("dialogue", [])
    assessments = data.get("assessments", [])
    corrections = data.get("corrections", [])
    vocabulary = data.get("vocabulary", [])

    user_turns = [d for d in dialogue if d.get("role") == "user"]
    lines.append(f"- 你发言 {len(user_turns)} 句 / 对话共 {len(dialogue)} 条\n")

    if assessments:
        lines.append("## 雅思评分（四维 0-9）\n")
        for i, a in enumerate(assessments):
            if i > 0:
                lines.append("\n---\n")
            if len(assessments) > 1:
                lines.append(f"**评分 {i + 1}（{'最新' if i == 0 else '较早'}）**\n")
            lines.append(f"总 Band：{a.get('overall_band', '?')}\n")
            lines.append("| 维度 | 分数 |")
            lines.append("|------|------|")
            lines.append(f"| 流利与连贯 Fluency & Coherence | {a.get('fluency_coherence', '?')} |")
            lines.append(f"| 词汇资源 Lexical Resource | {a.get('lexical_resource', '?')} |")
            lines.append(f"| 语法多样性 Grammatical Range & Accuracy | {a.get('grammatical_range_accuracy', '?')} |")
            lines.append(f"| 发音 Pronunciation | {a.get('pronunciation', '?')} |")
            if a.get("comment"):
                lines.append(f"\n{a.get('comment')}")

    if corrections:
        lines.append("\n## 错题集（按雅思维度归类）\n")
        for i, c in enumerate(corrections, 1):
            criterion = CRITERION_CN.get(c.get("criterion", ""), c.get("criterion", ""))
            lines.append(f"{i}. ✗ {c.get('original', '')} → ✓ {c.get('corrected', '')}（{criterion}）")

    if vocabulary:
        lines.append("\n## 高分表达\n")
        for w in vocabulary:
            line = f"- **{w.get('word', '')}**：{w.get('meaning', '')}"
            if w.get("example"):
                line += f"（例：{w.get('example')}）"
            lines.append(line)

    if dialogue:
        lines.append("\n## 对话记录\n")
        for d in dialogue:
            role = "学生" if d.get("role") == "user" else "考官"
            lines.append(f"**{role}**：{d.get('text', '')}")

    return "\n".join(lines)


def save_report(md: str) -> str:
    """保存报告到 训练历史/报告/，返回绝对路径。"""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    path = REPORT_DIR / f"{ts}.md"
    path.write_text(md, encoding="utf-8")
    return str(path)


NOTES_ACCOUNT = "iCloud"
NOTES_FOLDER = "英语测试"


def extract_model_answer(dialogue: list) -> str:
    """从对话记录提取考官口述的参考答案（model answer），取最长的引号内容；无引号则截取干净正文。"""
    for d in reversed(dialogue):
        if d.get("role") != "coach":
            continue
        text = d.get("text", "")
        low = text.lower()
        if "model answer" not in low:
            continue
        idx = low.find("model answer")
        rest = text[idx:]
        # 截掉 model answer 之后的结构化反馈（"Next/Now, let's look at / discuss / talk about..."）
        cut = re.search(
            r"\b(?:Next|Now|Finally|Lastly),?\s+let'?s\s+(?:look\s+at|discuss|talk\s+about)",
            rest, re.IGNORECASE,
        )
        if cut:
            rest = rest[:cut.start()]
        quoted = re.findall(r'"([^"]{10,})"', rest)
        if quoted:
            return max(quoted, key=len).strip()
        # 无引号：去掉 "model answer for this topic." 之类的前导说明句，只留正文
        rest = re.sub(r"^[^.]*model\s+answer[^.]*\.\s*", "", rest, flags=re.IGNORECASE).strip()
        return rest.strip()
    return ""


def generate_model_answer(topic_obj: dict, dialogue: list) -> str:
    """基于学生口述内容，用本地 Ollama 润色成高分参考答案（没走到 feedback 时的兜底）。"""
    topic = topic_obj.get("topic", "") if isinstance(topic_obj, dict) else ""
    student_text = "\n".join(
        d.get("text", "").strip()
        for d in (dialogue or [])
        if d.get("role") == "user" and (d.get("text") or "").strip()
    )
    if not topic and not student_text:
        return ""
    prompt = (
        "You are an IELTS Speaking Examiner. Below is a student's spoken answer "
        "(with grammar errors and fragmented sentences).\n\n"
        f"Topic: {topic}\n\n"
        f"Student's spoken answer:\n{student_text}\n\n"
        "Rewrite the student's answer into a high-band (Band 7-8) version that:\n"
        "1. Keeps the SAME ideas, examples, and personal details the student mentioned.\n"
        "2. Fixes grammar errors and fragments into complete sentences.\n"
        "3. Upgrades vocabulary to more natural, precise expressions.\n"
        "4. Organizes the ideas into a clear, coherent structure.\n\n"
        "Output ONLY the improved answer text — no headings, no explanation."
    )
    try:
        req = urllib.request.Request(
            OLLAMA_URL,
            data=json.dumps({
                "model": ASSESS_MODEL,
                "prompt": prompt,
                "stream": False,
                "think": False,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=300) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return (data.get("response") or "").strip()
    except Exception as e:
        print(f"[参考答案生成失败] {e}", flush=True)
        return ""


def generate_study_pack(data: dict) -> str:
    """生成本次测试的学习资料包（纠错精讲 + 话题表达 + Part3 参考答案 + 四维诊断）。优先云端百炼，失败或无 key 回退本地 Ollama。"""
    topic_obj = data.get("topic") or {}
    topic = topic_obj.get("topic", "")
    part3 = topic_obj.get("part3", [])
    assessments = data.get("assessments", [])
    corrections = data.get("corrections", [])
    vocabulary = data.get("vocabulary", [])

    if not assessments and not corrections and not topic:
        return ""

    a = assessments[0] if assessments else {}
    score_lines = (
        f"FC={a.get('fluency_coherence', '?')}, LR={a.get('lexical_resource', '?')}, "
        f"GRA={a.get('grammatical_range_accuracy', '?')}, P={a.get('pronunciation', '?')}, "
        f"Overall={a.get('overall_band', '?')}"
    )

    err_lines = []
    for i, c in enumerate(corrections, 1):
        crit = CRITERION_CN.get(c.get("criterion", ""), c.get("criterion", ""))
        err_lines.append(f"{i}. ✗ {c.get('original', '')} → ✓ {c.get('corrected', '')}（{crit}）")
    err_text = "\n".join(err_lines) if err_lines else "（无纠错记录）"

    part3_text = "\n".join(f"- {q}" for q in part3) if part3 else "（无 Part 3 追问）"
    vocab_text = "\n".join(f"- {w.get('word', '')}：{w.get('meaning', '')}" for w in vocabulary) if vocabulary else "（无）"

    user_prompt = (
        "You are an expert IELTS Speaking trainer. Based on a student's recent speaking test, "
        "generate a personalized study pack with FOUR sections in Markdown.\n\n"
        f"学生评分（0-9）：{score_lines}\n\n"
        f"本次话题：{topic}\n\n"
        f"Part 3 追问：\n{part3_text}\n\n"
        f"学生错误清单（原句 → 改后，括号是雅思维度）：\n{err_text}\n\n"
        f"已学高分表达：\n{vocab_text}\n\n"
        "生成以下四个 section（讲解用中文、例句和范文用英文，简洁实用）：\n\n"
        "## 一、纠错精讲\n"
        "对每条错误用中文讲清「为什么错」（语法/用词规则），并给 2 个同类正确英文例句；"
        "末尾用一句话点出「你最该优先补的语法点」。\n\n"
        "## 二、话题表达包\n"
        f"围绕「{topic}」列 8-10 个 Band 7+ 地道词/collocation（英文词条 + 中文释义），"
        "再给 3 组「低分词 → 高分替换」；最后给一段该话题的 Band 7+ 标准范文（英文）。\n\n"
        "## 三、Part 3 参考答案\n"
        "对上面每个 Part 3 追问，写一段 2-3 句的 Band 7+ 英文参考答案。\n\n"
        "## 四、四维诊断\n"
        "指出最低分维度，给 2-3 条中文专项提升技巧；再按「当前 Band → 目标 Band（当前+1.5）」"
        "给 3 条具体的每周行动。\n\n"
        "只输出 Markdown 内容，不要任何开场白。"
    )

    # 云端优先：百炼 Token Plan（快、质量好）
    api_key = load_key()
    if api_key:
        try:
            req = urllib.request.Request(
                CLOUD_TEXT_URL,
                data=json.dumps({
                    "model": CLOUD_TEXT_MODEL,
                    "messages": [
                        {"role": "system", "content": "You are an expert IELTS Speaking trainer. Reply in Markdown; explain rules in Chinese, give examples and model answers in English."},
                        {"role": "user", "content": user_prompt},
                    ],
                    "stream": False,
                }).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
            )
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=90) as resp:
                out = json.loads(resp.read().decode("utf-8"))
            answer = (out.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
            if answer:
                return answer
        except Exception as e:
            print(f"[云端学习资料生成失败] {e}", flush=True)

    # 本地 Ollama 兜底
    try:
        req = urllib.request.Request(
            OLLAMA_URL,
            data=json.dumps({
                "model": ASSESS_MODEL,
                "prompt": user_prompt,
                "stream": False,
                "think": False,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=300) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        return (out.get("response") or "").strip()
    except Exception as e:
        print(f"[学习资料生成失败] {e}", flush=True)
        return ""


POLISH_PROMPT = """You are an expert English public speaking coach.

Below is the raw transcript of a student's impromptu speech (spoken English, likely with grammar errors, filler words, repetition, and disorganized structure).

Do TWO things and output STRICT JSON only:
{
  "polished_speech": "<the improved English speech draft — fix grammar, remove fillers and repetition, upgrade vocabulary, and organize the ideas into a clear structure (opening → main points → conclusion). KEEP the student's original ideas, examples, and personal details.>",
  "delivery_tips": "<Chinese delivery tips in Markdown — how to deliver this speech better: overall structure, pacing and pauses, transitions, emphasis and tone. Give concrete, actionable advice.>"
}

Student's raw transcript:
{transcript}"""


def polish_speech(transcript: str) -> dict:
    """把学生口述的演讲梳理成：优化稿（英文）+ 演讲技巧点评（中文）。
    优先云端百炼，失败或无 key 回退本地 Ollama。"""
    user_prompt = POLISH_PROMPT.replace("{transcript}", transcript)
    system_msg = (
        "You are an expert English public speaking coach. "
        "Output STRICT JSON with keys 'polished_speech' (English speech draft) "
        "and 'delivery_tips' (Chinese delivery tips in Markdown)."
    )

    # 云端优先：百炼 Token Plan（快、质量好）
    api_key = load_key()
    if api_key:
        try:
            req = urllib.request.Request(
                CLOUD_TEXT_URL,
                data=json.dumps({
                    "model": CLOUD_TEXT_MODEL,
                    "messages": [
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": user_prompt},
                    ],
                    "stream": False,
                }).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
            )
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=120) as resp:
                out = json.loads(resp.read().decode("utf-8"))
            answer = (out.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
            parsed = _parse_json_response(answer)
            if parsed.get("polished_speech") or parsed.get("delivery_tips"):
                return parsed
        except Exception as e:
            print(f"[云端演讲稿梳理失败] {e}", flush=True)

    # 本地 Ollama 兜底
    try:
        req = urllib.request.Request(
            OLLAMA_URL,
            data=json.dumps({
                "model": ASSESS_MODEL,
                "prompt": user_prompt,
                "stream": False,
                "think": False,
                "format": "json",
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=300) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        return _parse_json_response(out.get("response"))
    except Exception as e:
        print(f"[演讲稿梳理失败] {e}", flush=True)
        return {}


def build_notes_body(data: dict, model_answer: str = "", study_pack: str = "") -> str:
    """构建备忘录正文（评分 + 纠错 + 高分表达 + 参考答案 + 学习资料包）。"""
    lines = []
    topic = data.get("topic") or {}
    if topic.get("topic"):
        lines.append(f"话题：{topic.get('topic')}")
        lines.append("")

    assessments = data.get("assessments", [])
    if assessments:
        a = assessments[0]
        lines.append("【评分】")
        lines.append(f"总 Band：{a.get('overall_band', '?')}")
        lines.append(f"流利与连贯 FC：{a.get('fluency_coherence', '?')}")
        lines.append(f"词汇资源 LR：{a.get('lexical_resource', '?')}")
        lines.append(f"语法 GRA：{a.get('grammatical_range_accuracy', '?')}")
        lines.append(f"发音 P：{a.get('pronunciation', '?')}")
        if a.get("comment"):
            lines.append("")
            lines.append(a.get("comment"))
        lines.append("")

    corrections = data.get("corrections", [])
    if corrections:
        lines.append("【纠错】")
        for i, c in enumerate(corrections, 1):
            lines.append(f"{i}. ✗ {c.get('original', '')} → ✓ {c.get('corrected', '')}")
        lines.append("")

    vocabulary = data.get("vocabulary", [])
    if vocabulary:
        lines.append("【高分表达】")
        for w in vocabulary:
            line = f"- {w.get('word', '')}：{w.get('meaning', '')}"
            if w.get("example"):
                line += f"（例：{w.get('example')}）"
            lines.append(line)
        lines.append("")

    if model_answer:
        lines.append("【参考答案】")
        lines.append(model_answer)
        lines.append("")

    if study_pack:
        lines.append("【学习资料】")
        lines.append(study_pack)

    return "\n".join(lines)


def save_to_notes(data: dict) -> bool:
    """把测试精华 + 学习资料写入 macOS 备忘录「英语测试」文件夹，每次新建一条。"""
    model_answer = extract_model_answer(data.get("dialogue", []))
    if not model_answer:
        model_answer = generate_model_answer(data.get("topic") or {}, data.get("dialogue", []))
    study_pack = generate_study_pack(data)  # 尽力生成学习资料，失败则省略（不阻断精华写入）
    body = build_notes_body(data, model_answer, study_pack)
    if not body.strip():
        return False
    body = body.replace("\n", "<br>")  # Notes body 是 HTML，换行必须用 <br>，否则挤成一行

    topic = (data.get("topic") or {}).get("topic", "")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    overall = ""
    if data.get("assessments"):
        overall = f" ｜ Band {data.get('assessments')[0].get('overall_band', '?')}"
    title = f"雅思口语 {ts}{overall}"
    if topic:
        title += f" ｜ {topic[:30]}"

    def esc(s):
        return str(s).replace("\\", "\\\\").replace('"', '\\"')

    script = (
        'tell application "Notes"\n'
        f'tell account "{NOTES_ACCOUNT}"\n'
        f'if not (exists folder "{NOTES_FOLDER}") then\n'
        f'make new folder with properties {{name:"{NOTES_FOLDER}"}}\n'
        f'end if\n'
        f'make new note at folder "{NOTES_FOLDER}" with properties {{name:"{esc(title)}", body:"{esc(body)}"}}\n'
        f'end tell\n'
        'end tell\n'
    )
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            print(f"[备忘录写入失败] {r.stderr.strip()}", flush=True)
            return False
        print("[备忘录写入成功]", flush=True)
        return True
    except Exception as e:
        print(f"[备忘录写入异常] {e}", flush=True)
        return False


def load_progress():
    """读取训练历史所有 jsonl，汇总进步趋势数据。"""
    assessments = []
    corrections_count = 0
    vocabulary_count = 0
    total_turns = 0

    if HISTORY_DIR.is_dir():
        for f in sorted(HISTORY_DIR.glob("*.jsonl")):
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                role = rec.get("role")
                if role == "assessment":
                    assessments.append(rec)
                elif role == "correction":
                    corrections_count += 1
                elif role == "vocabulary":
                    vocabulary_count += 1
                elif role in ("user", "coach"):
                    total_turns += 1

    assessments.sort(key=lambda r: r.get("ts", ""))
    return {
        "assessments": assessments,
        "corrections_count": corrections_count,
        "vocabulary_count": vocabulary_count,
        "total_turns": total_turns,
    }


class Handler(BaseHTTPRequestHandler):
    api_key = None

    def log_message(self, *args):  # 静默，避免刷屏
        pass

    def _send(self, code, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        touch()
        if self.path == "/ping":
            self._send(200, b'{"ok":true}', "application/json; charset=utf-8")
        elif self.path in ("/", "/index.html"):
            body = INDEX_FILE.read_bytes()
            self._send(200, body, "text/html; charset=utf-8")
        elif self.path.startswith("/config"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            mode = (qs.get("mode") or ["ielts"])[0]
            instructions = build_instructions(mode)
            payload = json.dumps({
                "model": MODEL,
                "voice": VOICE,
                "instructions": instructions,
                "has_key": Handler.api_key is not None,
                "profile": load_profile(),
                "knowledge": kb_index.get_overview(),
            }, ensure_ascii=False).encode("utf-8")
            self._send(200, payload, "application/json; charset=utf-8")
        elif self.path == "/progress":
            payload = json.dumps(load_progress(), ensure_ascii=False).encode("utf-8")
            self._send(200, payload, "application/json; charset=utf-8")
        elif self.path == "/topic":
            topics = load_topics()
            if not topics:
                self._send(200, json.dumps({"topic": None, "total": 0}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            else:
                t = random.choice(topics)
                t["total"] = len(topics)
                self._send(200, json.dumps(t, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):
        touch()
        if self.path == "/log":
            self._handle_log()
            return
        if self.path == "/assess":
            self._handle_assess()
            return
        if self.path == "/report":
            self._handle_report()
            return
        if self.path == "/upload":
            self._handle_upload()
            return
        if self.path == "/summarize":
            self._handle_summarize()
            return
        if self.path == "/save-key":
            self._handle_save_key()
            return
        if self.path == "/save-profile":
            self._handle_save_profile()
            return
        if self.path == "/search":
            self._handle_search()
            return
        if self.path == "/polish":
            self._handle_polish()
            return
        if self.path != "/signal":
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            offer_sdp = self.rfile.read(length)

            req = urllib.request.Request(
                SIGNALING_URL,
                data=offer_sdp,
                headers={
                    "Content-Type": "application/sdp",
                    "Authorization": "Bearer " + Handler.api_key,
                },
                method="POST",
            )
            # 禁代理直连（对齐 websocket 阶段的直连行为，避免系统代理干扰）
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=20) as resp:
                answer_sdp = resp.read()
            self._send(200, answer_sdp, "application/sdp")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            body = json.dumps(
                {"error": f"信令失败 {e.code}", "detail": detail},
                ensure_ascii=False,
            ).encode("utf-8")
            self._send(e.code, body, "application/json; charset=utf-8")
        except Exception as e:
            body = json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8")
            self._send(500, body, "application/json; charset=utf-8")

    def _handle_log(self):
        """接收前端训练记录（对话/纠错/词汇/评估），追加到训练历史。"""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            record = json.loads(body.decode("utf-8"))
            role = record.get("role")
            if role not in ("user", "coach", "correction", "vocabulary", "assessment"):
                self._send(400, b'{"error":"bad role"}', "application/json; charset=utf-8")
                return
            if not isinstance(record, dict) or len(record) <= 1:
                self._send(400, b'{"error":"empty record"}', "application/json; charset=utf-8")
                return
            append_log(record)
            self._send(200, b'{"ok":true}', "application/json; charset=utf-8")
        except Exception as e:
            body = json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8")
            self._send(500, body, "application/json; charset=utf-8")

    def _handle_assess(self):
        """接收对话内容，调用本地 Ollama 评估，返回结构化结果。"""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            req = json.loads(body.decode("utf-8"))
            dialog = (req.get("dialog") or "").strip()
            if not dialog:
                self._send(400, b'{"error":"empty dialog"}', "application/json; charset=utf-8")
                return
            mode = (req.get("mode") or "ielts").strip()
            result = assess_dialog(dialog, mode)
            self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        except Exception as e:
            print(f"[评分失败] {e}", flush=True)
            body = json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8")
            self._send(500, body, "application/json; charset=utf-8")

    def _handle_report(self):
        """接收会话数据，生成 Markdown 报告并离线保存。"""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            data = json.loads(body.decode("utf-8"))
            md = build_report(data)
            path = save_report(md)
            save_to_notes(data)  # 同步写入 macOS 备忘录（内部 try-catch，失败不影响报告）
            self._send(200, json.dumps({"path": path}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        except Exception as e:
            body = json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8")
            self._send(500, body, "application/json; charset=utf-8")

    def _handle_upload(self):
        """接收文件（base64），保存到知识库并解析内容。"""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            req = json.loads(body.decode("utf-8"))
            filename = (req.get("filename") or "").strip()
            data_b64 = req.get("data_b64") or ""
            if not filename or not data_b64:
                self._send(400, b'{"error":"bad request"}', "application/json; charset=utf-8")
                return
            filename = pathlib.Path(filename).name  # 安全：只取文件名
            raw = base64.b64decode(data_b64)
            KNOWLEDGE_DIR.mkdir(exist_ok=True)
            dest = KNOWLEDGE_DIR / filename
            dest.write_bytes(raw)
            try:
                content = kb_index.extract_file(dest)
            except Exception as e:
                content = f"[无法解析: {e}]"
            # 知识库变了，后台刷新索引（不阻塞当前响应）
            threading.Thread(target=kb_index.refresh_index, daemon=True).start()
            self._send(200, json.dumps({"filename": filename, "content": content}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        except Exception as e:
            body = json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8")
            self._send(500, body, "application/json; charset=utf-8")

    def _handle_summarize(self):
        """接收训练要求 + 文件内容，用本地 Ollama 整理成主题要点。"""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            req = json.loads(body.decode("utf-8"))
            requirement = (req.get("requirement") or "").strip()
            files = (req.get("files") or "").strip()
            if not requirement and not files:
                self._send(400, b'{"error":"empty"}', "application/json; charset=utf-8")
                return
            summary = summarize_training(requirement, files)
            self._send(200, json.dumps({"summary": summary}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        except Exception as e:
            body = json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8")
            self._send(500, body, "application/json; charset=utf-8")

    def _handle_save_key(self):
        """接收 API key，存入 macOS Keychain（不落明文到文件）。"""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            req = json.loads(body.decode("utf-8"))
            key = (req.get("key") or "").strip()
            if not key:
                self._send(400, b'{"error":"empty key"}', "application/json; charset=utf-8")
                return
            r = subprocess.run(
                ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE,
                 "-a", os.environ.get("USER", "user"), "-w", key],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                self._send(500, json.dumps({"error": r.stderr.strip()}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
                return
            Handler.api_key = key  # 更新内存中的 key，立即生效
            self._send(200, b'{"ok":true}', "application/json; charset=utf-8")
        except Exception as e:
            body = json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8")
            self._send(500, body, "application/json; charset=utf-8")

    def _handle_save_profile(self):
        """接收个人信息，存本地 JSON。"""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            req = json.loads(body.decode("utf-8"))
            save_profile(req)
            self._send(200, b'{"ok":true}', "application/json; charset=utf-8")
        except Exception as e:
            body = json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8")
            self._send(500, body, "application/json; charset=utf-8")

    def _handle_search(self):
        """接收主题 query，检索知识库返回相关块。"""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            req = json.loads(body.decode("utf-8"))
            query = (req.get("query") or "").strip()
            results = kb_index.search(query)
            self._send(200, json.dumps({"results": results}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        except Exception as e:
            body = json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8")
            self._send(500, body, "application/json; charset=utf-8")

    def _handle_polish(self):
        """接收学生口述的演讲全文，梳理成优化演讲稿 + 演讲技巧点评。"""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            req = json.loads(body.decode("utf-8"))
            transcript = (req.get("transcript") or "").strip()
            if not transcript:
                self._send(400, b'{"error":"empty transcript"}', "application/json; charset=utf-8")
                return
            result = polish_speech(transcript)
            self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        except Exception as e:
            print(f"[演讲稿梳理失败] {e}", flush=True)
            body = json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8")
            self._send(500, body, "application/json; charset=utf-8")


def main():
    Handler.api_key = load_key()
    if Handler.api_key is None:
        print("[提示] 未检测到 API Key，打开浏览器后在设置里输入。")

    # 先 bind 端口，让服务立刻可用；知识库解析（首次可能较慢）放后台预热，
    # 避免 PDF 解析阻塞端口就绪，导致浏览器打开时"无法连接"。
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"服务启动：http://127.0.0.1:{PORT}/")
    print(f"关闭浏览器后 {IDLE_TIMEOUT} 秒无心跳将自动退出")

    def warmup():
        global _WARMUP_DONE
        try:
            stats = kb_index.refresh_index()
            print(f"知识库索引：{json.dumps(stats, ensure_ascii=False)}")
        except Exception as e:
            print(f"[警告] 知识库索引失败：{e}")
        finally:
            _WARMUP_DONE = True

    threading.Thread(target=warmup, daemon=True).start()
    threading.Thread(target=warmup_ollama, daemon=True).start()

    def idle_watchdog():
        while True:
            time.sleep(15)
            if _WARMUP_DONE and time.time() - _LAST_ACTIVITY > IDLE_TIMEOUT:
                print(f"空闲超过 {IDLE_TIMEOUT} 秒，自动退出。")
                os._exit(0)

    threading.Thread(target=idle_watchdog, daemon=True).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
