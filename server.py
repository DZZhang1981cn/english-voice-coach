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
import io
import json
import os
import pathlib
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = pathlib.Path(__file__).resolve().parent
PERSONA_FILE = BASE_DIR / "人设.md"
KNOWLEDGE_DIR = BASE_DIR / "知识库"
INDEX_FILE = BASE_DIR / "index.html"
HISTORY_DIR = BASE_DIR / "训练历史"
REPORT_DIR = HISTORY_DIR / "报告"
PROFILE_FILE = BASE_DIR / "个人设置.json"

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

# 空闲退出：浏览器关闭后心跳停止，空闲超时自动退出（关浏览器即关服务）
IDLE_TIMEOUT = 60  # 秒
_LAST_ACTIVITY = time.time()


def touch():
    global _LAST_ACTIVITY
    _LAST_ACTIVITY = time.time()

# 知识库拼接进 instructions 的长度上限（防止超长导致 session.update 失败）
KNOWLEDGE_MAX_CHARS = 20000

TEXT_EXTS = {".md", ".txt", ".markdown"}


def load_key():
    """从 macOS Keychain 读取 API key。缺失时返回 None（不退出，进入设置流程）。"""
    r = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def extract_pdf(path: pathlib.Path) -> str:
    import pdfplumber
    parts = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if text.strip():
                parts.append(text)
            for table in page.extract_tables() or []:
                for row in table:
                    cells = [("" if c is None else str(c)) for c in row]
                    parts.append(" | ".join(cells))
    return "\n".join(parts)


def extract_xlsx(path: pathlib.Path) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    parts = []
    for ws in wb.worksheets:
        parts.append(f"[表 {ws.title}]")
        for row in ws.iter_rows(values_only=True):
            cells = [("" if c is None else str(c)) for c in row]
            if any(c.strip() for c in cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def extract_csv(path: pathlib.Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    return "\n".join(" | ".join(cells) for cells in rows)


def extract_text_file(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


DEFAULT_PROFILE = {
    "english_level": "CET-4（四级，中等水平）",
    "goal": "商务口语（销售、市场、客户沟通）",
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


def load_knowledge():
    """扫描知识库文件夹，按类型提取文本，返回 (拼接文本, 文件名列表)。"""
    if not KNOWLEDGE_DIR.is_dir():
        return "", []
    blocks = []
    files = sorted(
        p for p in KNOWLEDGE_DIR.iterdir()
        if p.is_file() and not p.name.startswith(".")
    )
    for p in files:
        ext = p.suffix.lower()
        try:
            if ext in TEXT_EXTS:
                content = extract_text_file(p)
            elif ext == ".csv":
                content = extract_csv(p)
            elif ext == ".xlsx":
                content = extract_xlsx(p)
            elif ext == ".pdf":
                content = extract_pdf(p)
            else:
                continue
        except Exception as e:
            content = f"[读取失败：{e}]"
        blocks.append(f"===== {p.name} =====\n{content.strip()}")
    return "\n\n".join(blocks), [p.name for p in files]


def build_instructions():
    """拼接 instructions = 人设 + 个人设置 + 知识库文件夹内容。返回 (instructions, meta)。"""
    persona = PERSONA_FILE.read_text(encoding="utf-8").strip()
    profile = load_profile()
    knowledge, knowledge_files = load_knowledge()
    meta = {"knowledge_files": knowledge_files, "knowledge_truncated": False}

    instructions = persona
    instructions += (
        "\n\n# About your student (use this to personalize every lesson)\n"
        f"- English level: {profile.get('english_level', '')}\n"
        f"- Learning goal: {profile.get('goal', '')}\n"
        f"- Profession: {profile.get('profession', '')}\n"
        f"- Hobbies: {profile.get('hobbies', '')}"
    )
    if knowledge.strip():
        instructions += (
            "\n\n# 学生的本地知识库（以下为用户提供的资料，"
            "可据此个性化教学、举行业例子）\n\n" + knowledge
        )

    if len(instructions) > KNOWLEDGE_MAX_CHARS:
        instructions = instructions[:KNOWLEDGE_MAX_CHARS]
        meta["knowledge_truncated"] = True

    return instructions, meta


def append_log(record: dict):
    """追加一条训练记录到 训练历史/YYYY-MM-DD.jsonl（自动补时间戳）。"""
    HISTORY_DIR.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    path = HISTORY_DIR / f"{today}.jsonl"
    record["ts"] = datetime.now().isoformat(timespec="seconds")
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


ASSESS_PROMPT = """You are an English coach evaluating a student's spoken English.

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
If there are no corrections or new words, output empty arrays.

Conversation:
{dialog}"""


def assess_dialog(dialog: str) -> dict:
    """调用本地 Ollama 评估一段对话，返回结构化结果。"""
    prompt = ASSESS_PROMPT.replace("{dialog}", dialog)
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
    with opener.open(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return json.loads(data.get("response") or "{}")


def extract_file(path: pathlib.Path) -> str:
    """按扩展名解析单个文件为文本。"""
    ext = path.suffix.lower()
    if ext in TEXT_EXTS:
        return extract_text_file(path)
    if ext == ".csv":
        return extract_csv(path)
    if ext == ".xlsx":
        return extract_xlsx(path)
    if ext == ".pdf":
        return extract_pdf(path)
    raise ValueError(f"不支持的文件类型: {ext}")


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


def build_report(data: dict) -> str:
    """根据会话数据生成 Markdown 报告。"""
    lines = []
    lines.append("# 英语训练报告\n")
    lines.append(f"- 会话时间：{data.get('start_time', '')} ~ {data.get('end_time', '')}")

    dialogue = data.get("dialogue", [])
    assessments = data.get("assessments", [])
    corrections = data.get("corrections", [])
    vocabulary = data.get("vocabulary", [])

    user_turns = [d for d in dialogue if d.get("role") == "user"]
    lines.append(f"- 你发言 {len(user_turns)} 句 / 对话共 {len(dialogue)} 条\n")

    if assessments:
        first = assessments[-1]
        last = assessments[0]
        lines.append("## 水平评估\n")
        lines.append(f"本次会话综合水平：**{first.get('cefr_level', '?')}**（起始）→ **{last.get('cefr_level', '?')}**（结束）\n")
        lines.append("| 时间 | CEFR | 语法 | 流利 | 词汇 |")
        lines.append("|------|------|------|------|------|")
        for a in reversed(assessments):
            lines.append(
                f"| {a.get('ts', '')} | {a.get('cefr_level', '?')} | "
                f"{a.get('grammar_score', '')} | {a.get('fluency_score', '')} | {a.get('vocabulary_score', '')} |"
            )

    if corrections:
        lines.append("\n## 错题集\n")
        for i, c in enumerate(corrections, 1):
            lines.append(f"{i}. ✗ {c.get('original', '')} → ✓ {c.get('corrected', '')}（{c.get('error_type', '')}）")

    if vocabulary:
        lines.append("\n## 新词\n")
        for w in vocabulary:
            line = f"- **{w.get('word', '')}**：{w.get('meaning', '')}"
            if w.get("example"):
                line += f"（例：{w.get('example')}）"
            lines.append(line)

    if dialogue:
        lines.append("\n## 对话记录\n")
        for d in dialogue:
            role = "学生" if d.get("role") == "user" else "教练"
            lines.append(f"**{role}**：{d.get('text', '')}")

    return "\n".join(lines)


def save_report(md: str) -> str:
    """保存报告到 训练历史/报告/，返回绝对路径。"""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    path = REPORT_DIR / f"{ts}.md"
    path.write_text(md, encoding="utf-8")
    return str(path)


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
        elif self.path == "/config":
            instructions, meta = build_instructions()
            payload = json.dumps({
                "model": MODEL,
                "voice": VOICE,
                "instructions": instructions,
                "has_key": Handler.api_key is not None,
                "profile": load_profile(),
                **meta,
            }, ensure_ascii=False).encode("utf-8")
            self._send(200, payload, "application/json; charset=utf-8")
        elif self.path == "/progress":
            payload = json.dumps(load_progress(), ensure_ascii=False).encode("utf-8")
            self._send(200, payload, "application/json; charset=utf-8")
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
            result = assess_dialog(dialog)
            self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        except Exception as e:
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
                content = extract_file(dest)
            except Exception as e:
                content = f"[无法解析: {e}]"
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


def main():
    Handler.api_key = load_key()
    if Handler.api_key is None:
        print("[提示] 未检测到 API Key，打开浏览器后在设置里输入。")
    instructions, meta = build_instructions()
    print(f"知识库文件：{meta['knowledge_files']}")
    if meta["knowledge_truncated"]:
        print(f"[警告] instructions 超过 {KNOWLEDGE_MAX_CHARS} 字符，已截断。")
    print(f"instructions 长度：{len(instructions)} 字符")
    print(f"服务启动：http://127.0.0.1:{PORT}/")
    print(f"关闭浏览器后 {IDLE_TIMEOUT} 秒无心跳将自动退出")

    def idle_watchdog():
        while True:
            time.sleep(15)
            if time.time() - _LAST_ACTIVITY > IDLE_TIMEOUT:
                print(f"空闲超过 {IDLE_TIMEOUT} 秒，自动退出。")
                os._exit(0)

    threading.Thread(target=idle_watchdog, daemon=True).start()

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
