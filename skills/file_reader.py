from __future__ import annotations

from skills import resolve_data_path


# 内联定义资源限制（避免模块导入缓存问题）
_MAX_READ_CHARS = 10000
_MAX_FILE_SIZE_MB = 10


def _check_file_size(path, max_mb):
    """检查文件大小是否超限"""
    if not path.exists():
        return
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > max_mb:
        raise ValueError(
            f"file too large: {size_mb:.1f}MB (max {max_mb}MB): {path}"
        )


def file_reader(path: str, max_chars: int = 2000, *, data_root: str | None = None) -> dict:
    """读取本地 txt/md 文件（带资源限制）"""
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
        raise ValueError("max_chars must be a positive integer")
    
    # 限制max_chars上限
    if max_chars > _MAX_READ_CHARS:
        max_chars = _MAX_READ_CHARS
    
    source, root = resolve_data_path(path, data_root)
    if source.suffix.lower() not in {".txt", ".md"}:
        raise ValueError("file_reader only supports .txt and .md files")
    if not source.is_file():
        raise FileNotFoundError(f"file not found: {path}")
    
    # 检查文件大小
    _check_file_size(source, _MAX_FILE_SIZE_MB)
    
    original = source.read_text(encoding="utf-8")
    content = original[:max_chars]
    return {
        "content": content,
        "num_chars": len(content),
        "source": source.relative_to(root).as_posix(),
        "truncated": len(original) > len(content),
    }