#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""知识库索引 + 检索模块（语义检索优先，BM25 关键词兜底）。

把 知识库/ 文件夹里的文件解析成文本 → 分块 → 建索引，持久化到 知识库索引/：
  - 语义检索：bge-m3 embedding（本地 Ollama）+ 余弦相似度，query 与块做语义匹配
  - BM25 兜底：jieba + rank_bm25，embedding 不可用时降级

索引持久化：index.pkl（分块 + BM25 语料 + 文件 mtime）+ embeddings.npy（向量矩阵）。
文件 mtime 不变则不重建；任一文件变化则整体重建（后台执行，不阻塞服务）。

图片/音频/视频不做内容解析（OCR/转写对几百 MB 不现实），跳过并记录在 overview。
"""

import json
import os
import pathlib
import pickle
import re
import threading
from datetime import datetime

import jieba
from rank_bm25 import BM25Okapi

BASE_DIR = pathlib.Path(__file__).resolve().parent
KNOWLEDGE_DIR = BASE_DIR / "知识库"
INDEX_DIR = BASE_DIR / "知识库索引"
INDEX_FILE = INDEX_DIR / "index.pkl"
EMBEDDINGS_FILE = INDEX_DIR / "embeddings.npy"

CHUNK_SIZE = 500          # 每块字符数
CHUNK_OVERLAP = 50        # 相邻块重叠字符数
MAX_QUERY_RESULTS = 8     # 检索返回的最大块数
EMBED_BATCH = 32          # embedding 批量大小

# 语义检索：本地 Ollama embedding 模型（bge-m3，中英多语，1024 维）
EMBED_MODEL = "bge-m3"
OLLAMA_EMBED_URL = "http://127.0.0.1:11434/api/embed"

TEXT_EXTS = {".md", ".txt", ".markdown"}
CSV_EXTS = {".csv"}
XLSX_EXTS = {".xlsx", ".xls"}
PDF_EXTS = {".pdf"}
DOCX_EXTS = {".docx"}
JSON_EXTS = {".json"}
SUPPORTED_EXTS = TEXT_EXTS | CSV_EXTS | XLSX_EXTS | PDF_EXTS | DOCX_EXTS | JSON_EXTS

# 不做内容解析的文件类型（图片/音频/视频/压缩包等），跳过只标注
SKIP_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico",
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg",
    ".mp4", ".mov", ".avi", ".mkv", ".webm",
    ".zip", ".rar", ".7z", ".tar", ".gz",
}

# 模块级内存缓存（server 后台 refresh 后，search 直接命中，避免每次重建）
_lock = threading.Lock()
_cache = {"chunks": [], "bm25": None, "embeddings": None, "stats": None}


# ---------- 解析 ----------

def extract_text_file(path):
    return path.read_text(encoding="utf-8", errors="replace")


def extract_csv(path):
    import csv
    import io
    text = path.read_text(encoding="utf-8", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    return "\n".join(" | ".join(cells) for cells in rows)


def extract_xlsx(path):
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


def extract_pdf(path):
    import pdfplumber
    parts = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if text.strip():
                parts.append(text)
    return "\n".join(parts)


def extract_docx(path):
    import docx
    d = docx.Document(str(path))
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for table in d.tables:
        for row in table.rows:
            parts.append(" | ".join(c.text for c in row.cells))
    return "\n".join(parts)


def extract_json(path):
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    return _json_to_text(data)


def _json_to_text(obj, depth=0):
    """把 JSON 对象递归转成可读文本（限深避免爆栈）。"""
    if depth > 6:
        return str(obj)
    if isinstance(obj, dict):
        return "\n".join(f"{k}: {_json_to_text(v, depth + 1)}" for k, v in obj.items())
    if isinstance(obj, list):
        return "\n".join(_json_to_text(v, depth + 1) for v in obj)
    return str(obj)


def extract_file(path):
    ext = path.suffix.lower()
    if ext in TEXT_EXTS:
        return extract_text_file(path)
    if ext in CSV_EXTS:
        return extract_csv(path)
    if ext in XLSX_EXTS:
        return extract_xlsx(path)
    if ext in PDF_EXTS:
        return extract_pdf(path)
    if ext in DOCX_EXTS:
        return extract_docx(path)
    if ext in JSON_EXTS:
        return extract_json(path)
    raise ValueError(f"不支持的文件类型: {ext}")


# ---------- 分块 ----------

def chunk_text(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """按标题/空行分块，尽量让每块对应一个主题（标题 + 其内容）。"""
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    # 按 Markdown 标题切 section（行首 #）；无标题则整段处理
    sections = [s.strip() for s in re.split(r"(?m)^(?=#{1,6}\s)", text) if s.strip()]
    if len(sections) <= 1:
        sections = [text]

    chunks = []
    for sec in sections:
        chunks.extend(_split_section(sec, size))
    return chunks


def _split_section(sec, size):
    if len(sec) <= size:
        return [sec]
    paras = [p.strip() for p in re.split(r"\n\s*\n", sec) if p.strip()]
    out = []
    buf = ""
    for p in paras:
        if len(p) > size:
            if buf:
                out.append(buf)
                buf = ""
            out.extend(_split_by_char(p, size))
            continue
        if buf and len(buf) + len(p) + 2 > size:
            out.append(buf)
            buf = p
        else:
            buf = (buf + "\n\n" + p).strip() if buf else p
    if buf:
        out.append(buf)
    return out


def _split_by_char(p, size):
    return [p[i:i + size].strip() for i in range(0, len(p), size) if p[i:i + size].strip()]


# ---------- embedding（语义检索） ----------

def _embed(texts):
    """调本地 Ollama embedding，返回 list[list[float]]。禁代理（本机系统代理会劫持 localhost）。"""
    import urllib.request
    payload = json.dumps({"model": EMBED_MODEL, "input": texts}).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_EMBED_URL, data=payload,
        headers={"Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=180) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    out = []
    for e in data.get("embeddings", []):
        if isinstance(e, dict):
            out.append(e.get("embedding", []))
        else:
            out.append(e)
    return out


def _embed_batch(texts, batch=EMBED_BATCH):
    """批量生成 embedding，返回 numpy (N, D) float32。失败返回 None。"""
    import numpy as np
    all_vecs = []
    for i in range(0, len(texts), batch):
        batch_texts = texts[i:i + batch]
        vecs = _embed(batch_texts)
        all_vecs.extend(vecs)
        if i // batch % 50 == 0:
            print(f"[embedding] {min(i + batch, len(texts))}/{len(texts)}")
    return np.asarray(all_vecs, dtype=np.float32)


def _cosine_sim(query_vec, matrix):
    """query_vec(D,) 与 matrix(N,D) 的余弦相似度，返回 numpy (N,)。"""
    import numpy as np
    q = np.asarray(query_vec, dtype=np.float32)
    m = np.asarray(matrix, dtype=np.float32)
    qn = q / (np.linalg.norm(q) + 1e-9)
    mn = m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-9)
    return mn @ qn


# ---------- 扫描与索引 ----------

def _scan_files():
    if not KNOWLEDGE_DIR.is_dir():
        return []
    return sorted(
        p for p in KNOWLEDGE_DIR.iterdir()
        if p.is_file() and not p.name.startswith(".")
    )


def _build_bm25(tokenized):
    if not tokenized:
        return None
    return BM25Okapi(tokenized)


def build_index(force=False):
    """扫描知识库，解析分块，建索引（BM25 + embedding），持久化。

    返回 (chunks, tokenized, embeddings, stats)。embeddings 为 numpy 数组或 None。
    """
    files = _scan_files()

    old_mtimes = {}
    old_chunks = []
    old_tokenized = []
    if INDEX_FILE.exists() and not force:
        try:
            with open(INDEX_FILE, "rb") as f:
                old = pickle.load(f)
            old_mtimes = old.get("file_mtimes", {})
            old_chunks = old.get("chunks", [])
            old_tokenized = old.get("tokenized", [])
        except Exception:
            old_mtimes, old_chunks, old_tokenized = {}, [], []

    new_mtimes = {}
    for p in files:
        try:
            new_mtimes[str(p)] = p.stat().st_mtime
        except OSError:
            pass

    changed = {p for p, mt in new_mtimes.items() if old_mtimes.get(p) != mt}
    removed = set(old_mtimes) - set(new_mtimes)

    if not changed and not removed and old_chunks and old_tokenized:
        # 无变化，复用旧索引（embedding 从磁盘读）
        embeddings = _load_embeddings_from_disk()
        stats = {
            "files": len(files), "chunks": len(old_chunks),
            "skipped": [], "failed": [], "rebuilt": False,
            "has_embeddings": embeddings is not None,
        }
        return old_chunks, old_tokenized, embeddings, stats

    # 有变化：重新解析全部文件（大文件首次较慢，但后台执行 + 持久化后不再重建）
    chunks = []
    tokenized = []
    skipped = []
    failed = []
    for p in files:
        ext = p.suffix.lower()
        if ext not in SUPPORTED_EXTS:
            if ext in SKIP_EXTS:
                skipped.append(p.name)
            continue
        try:
            content = extract_file(p)
        except Exception as e:
            failed.append({"file": p.name, "error": str(e)})
            continue
        for i, c in enumerate(chunk_text(content)):
            chunks.append({"file": p.name, "chunk_id": i, "text": c})
            tokenized.append(jieba.lcut(c))

    # embedding（语义检索）；失败则降级为纯 BM25
    embeddings = None
    if chunks:
        try:
            print(f"[embedding] 开始为 {len(chunks)} 块生成向量（模型 {EMBED_MODEL}）…")
            embeddings = _embed_batch([c["text"] for c in chunks])
            print(f"[embedding] 完成，矩阵形状 {embeddings.shape}")
        except Exception as e:
            print(f"[警告] embedding 生成失败，降级为 BM25：{e}")
            embeddings = None

    payload = {
        "chunks": chunks,
        "tokenized": tokenized,
        "file_mtimes": new_mtimes,
        "has_embeddings": embeddings is not None,
        "built_at": datetime.now().isoformat(timespec="seconds"),
    }
    INDEX_DIR.mkdir(exist_ok=True)
    tmp = INDEX_FILE.with_suffix(".pkl.tmp")
    with open(tmp, "wb") as f:
        pickle.dump(payload, f)
    os.replace(tmp, INDEX_FILE)

    if embeddings is not None:
        import numpy as np
        np.save(EMBEDDINGS_FILE, embeddings)
    elif EMBEDDINGS_FILE.exists():
        try:
            os.remove(EMBEDDINGS_FILE)
        except OSError:
            pass

    stats = {
        "files": len(files), "chunks": len(chunks),
        "skipped": skipped, "failed": failed, "rebuilt": True,
        "has_embeddings": embeddings is not None,
    }
    return chunks, tokenized, embeddings, stats


def _load_embeddings_from_disk():
    import numpy as np
    if EMBEDDINGS_FILE.exists():
        try:
            return np.load(EMBEDDINGS_FILE)
        except Exception:
            return None
    return None


def refresh_index():
    """重建索引并缓存到内存（供 server 后台调用）。"""
    chunks, tokenized, embeddings, stats = build_index()
    bm25 = _build_bm25(tokenized)
    with _lock:
        _cache["chunks"] = chunks
        _cache["bm25"] = bm25
        _cache["embeddings"] = embeddings
        _cache["stats"] = stats
    return stats


def _ensure_loaded():
    """确保内存缓存已加载（首次 search 时从磁盘 load）。"""
    with _lock:
        if _cache["bm25"] is None and _cache["chunks"] == []:
            if INDEX_FILE.exists():
                try:
                    with open(INDEX_FILE, "rb") as f:
                        payload = pickle.load(f)
                    _cache["chunks"] = payload.get("chunks", [])
                    _cache["bm25"] = _build_bm25(payload.get("tokenized", []))
                    _cache["embeddings"] = _load_embeddings_from_disk()
                except Exception:
                    _cache["chunks"] = []
                    _cache["bm25"] = None
                    _cache["embeddings"] = None


def search(query, top_k=MAX_QUERY_RESULTS):
    """检索知识库，返回 top-k 相关块 [{file, chunk_id, text, score}]。

    优先语义检索（bge-m3 embedding + 余弦相似度），失败/不可用则 BM25 兜底。
    """
    query = (query or "").strip()
    if not query:
        return []
    _ensure_loaded()
    with _lock:
        chunks = _cache["chunks"]
        bm25 = _cache["bm25"]
        embeddings = _cache.get("embeddings")
    if not chunks:
        return []

    # 1) 语义检索优先
    if embeddings is not None and len(embeddings) == len(chunks):
        try:
            qv = _embed([query])[0]
            sims = _cosine_sim(qv, embeddings)
            order = sorted(range(len(chunks)), key=lambda i: -sims[i])[:top_k]
            return [
                {
                    "file": chunks[i]["file"],
                    "chunk_id": chunks[i]["chunk_id"],
                    "text": chunks[i]["text"],
                    "score": round(float(sims[i]), 4),
                }
                for i in order
            ]
        except Exception:
            pass

    # 2) BM25 兜底
    if not bm25:
        return []
    scores = bm25.get_scores(jieba.lcut(query))
    ranked = sorted(range(len(chunks)), key=lambda i: -scores[i])[:top_k]
    return [
        {
            "file": chunks[i]["file"],
            "chunk_id": chunks[i]["chunk_id"],
            "text": chunks[i]["text"],
            "score": round(float(scores[i]), 2),
        }
        for i in ranked
    ]


def get_overview():
    """返回知识库概况（文件数、块数、是否语义检索），供 /config 显示。"""
    files = _scan_files()
    supported = []
    skipped = []
    for p in files:
        ext = p.suffix.lower()
        if ext in SUPPORTED_EXTS:
            supported.append(p.name)
        elif ext in SKIP_EXTS:
            skipped.append(p.name)
    n_chunks = 0
    has_embeddings = False
    if INDEX_FILE.exists():
        try:
            with open(INDEX_FILE, "rb") as f:
                payload = pickle.load(f)
            n_chunks = len(payload.get("chunks", []))
            has_embeddings = payload.get("has_embeddings", False)
        except Exception:
            pass
    return {
        "files": len(files),
        "chunks": n_chunks,
        "supported": supported,
        "skipped": skipped,
        "has_embeddings": has_embeddings,
    }


if __name__ == "__main__":
    stats = refresh_index()
    print("索引统计:", json.dumps(stats, ensure_ascii=False))
    print("\n--- 语义检索测试：轮胎价格 ---")
    for r in search("轮胎价格"):
        print(f"[{r['file']}#{r['chunk_id']} score={r['score']}] {r['text'][:60]}")
