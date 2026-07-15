from __future__ import annotations

import re
import math
import time
from collections import Counter

from skills import resolve_data_path


# 内联定义资源限制（避免模块导入缓存问题）
_MAX_SEARCH_FILES = 1000           # 最大搜索文件数
_SEARCH_TIMEOUT_SECONDS = 30       # 搜索超时时间
_MAX_FILE_SIZE_MB = 10             # 单个文件最大大小(MB)


def _snippet(text: str, terms: list[str], radius: int = 60) -> str:
    lowered = text.casefold()
    positions = [lowered.find(term.casefold()) for term in terms]
    positions = [position for position in positions if position >= 0]
    start = max(0, (min(positions) if positions else 0) - radius)
    end = min(len(text), start + radius * 2)
    prefix = "..." if start else ""
    suffix = "..." if end < len(text) else ""
    return prefix + text[start:end].replace("\n", " ").strip() + suffix


def _tokenize(text: str) -> list[str]:
    """分词：按非字母数字字符分割，转为小写"""
    return [token for token in re.split(r"[^a-zA-Z0-9\u4e00-\u9fff]+", text.lower()) if token]


def _compute_tf_idf(query_terms: list[str], documents: list[dict]) -> list[tuple[int, float]]:
    """
    计算 TF-IDF 分数，返回 (文档索引, 分数) 列表

    TF = 词在文档中出现次数 / 文档总词数
    IDF = log(总文档数 / 包含该词的文档数 + 1)
    TF-IDF = TF * IDF
    """
    n_docs = len(documents)

    # 计算每个词的 IDF
    idf = {}
    for term in query_terms:
        doc_count = sum(1 for doc in documents if term in doc["tokens"])
        idf[term] = math.log(n_docs / (doc_count + 1)) + 1  # +1 平滑

    scores = []
    for idx, doc in enumerate(documents):
        score = 0.0
        doc_tokens = doc["tokens"]
        token_counts = Counter(doc_tokens)
        doc_len = len(doc_tokens) if doc_tokens else 1

        for term in query_terms:
            tf = token_counts.get(term, 0) / doc_len
            score += tf * idf.get(term, 0)

        # 额外加分：文件名匹配
        filename = doc.get("filename", "").lower()
        for term in query_terms:
            if term in filename:
                score *= 1.5  # 文件名匹配加权

        scores.append((idx, score))

    return scores


def local_file_search(
    query: str,
    root_dir: str = "docs",
    file_types: list[str] | None = None,
    top_k: int = 5,
    *,
    data_root: str | None = None,
) -> dict:
    """
    增强版本地文件搜索：支持 TF-IDF 内容检索（带资源限制）

    Args:
        query: 搜索关键词
        root_dir: 搜索根目录
        file_types: 文件类型过滤，如 ["txt", "md"]
        top_k: 返回前k个结果
        data_root: 数据根目录（B3 自动注入）

    Returns:
        业务结果字典
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k <= 0:
        raise ValueError("top_k must be a positive integer")

    search_root, data_root_path = resolve_data_path(root_dir, data_root)
    if not search_root.is_dir():
        raise FileNotFoundError(f"search directory not found: {root_dir}")

    extensions = file_types or ["txt", "md"]
    normalized_extensions = {f".{item.lower().lstrip('.')}" for item in extensions}
    if not normalized_extensions.issubset({".txt", ".md"}):
        raise ValueError("local_file_search only supports txt and md")

    # 分词处理查询
    query_terms = _tokenize(query)
    if not query_terms:
        raise ValueError("query contains no searchable terms")

    # 收集所有文档（带资源限制）
    documents = []
    file_count = 0
    start_time = time.time()
    timed_out = False

    for path in sorted(search_root.rglob("*")):
        # 超时检查
        if time.time() - start_time > _SEARCH_TIMEOUT_SECONDS:
            timed_out = True
            break

        # 文件数量限制
        file_count += 1
        if file_count > _MAX_SEARCH_FILES:
            break

        if not path.is_file() or path.suffix.lower() not in normalized_extensions:
            continue

        # 单个文件大小检查
        if path.stat().st_size > _MAX_FILE_SIZE_MB * 1024 * 1024:
            continue

        text = path.read_text(encoding="utf-8")
        tokens = _tokenize(text)

        documents.append({
            "path": path,
            "relative_path": path.relative_to(data_root_path).as_posix(),
            "filename": path.name,
            "text": text,
            "tokens": tokens,
        })

    if not documents:
        return {
            "results": [],
            "total_scanned": file_count,
            "timed_out": timed_out,
            "query_terms": query_terms
        }

    # TF-IDF 计算分数
    scores = _compute_tf_idf(query_terms, documents)
    scores.sort(key=lambda x: x[1], reverse=True)

    # 构建结果
    results = []
    for idx, score in scores[:top_k]:
        doc = documents[idx]
        results.append({
            "path": doc["relative_path"],
            "score": round(score, 4),
            "snippet": _snippet(doc["text"], query_terms),
            "filename_match": any(term in doc["filename"].lower() for term in query_terms),
        })

    return {
        "results": results,
        "total_scanned": file_count,
        "timed_out": timed_out,
        "query_terms": query_terms,
    }

