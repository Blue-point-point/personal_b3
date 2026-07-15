from __future__ import annotations

from pathlib import Path


DEFAULT_DATA_ROOT = Path(__file__).resolve().parents[1] / "data"


# ========== 资源限制配置 ==========
class ResourceLimits:
    """Skill资源限制配置"""
    
    # 计算限制
    MAX_EXPRESSION_LENGTH = 500      # calculator表达式最大长度
    MAX_EXPONENT = 20                # 最大指数
    
    # 文件限制
    MAX_FILE_SIZE_MB = 10            # 最大文件大小(MB)
    MAX_READ_CHARS = 10000           # 单次最大读取字符数
    
    # 搜索限制
    MAX_SEARCH_FILES = 1000          # 最大搜索文件数
    SEARCH_TIMEOUT_SECONDS = 30      # 搜索超时时间
    
    # 表格限制
    MAX_TABLE_ROWS = 100000          # 最大表格行数
    MAX_TABLE_SIZE_MB = 50           # 最大表格文件大小
    
    # 输出限制
    MAX_OUTPUT_SIZE_MB = 10          # 最大输出文件大小


# ========== 限制检查函数 ==========
def check_file_size(path: Path, max_mb: float) -> None:
    """检查文件大小是否超限"""
    if not path.exists():
        return
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > max_mb:
        raise ValueError(
            f"file too large: {size_mb:.1f}MB (max {max_mb}MB): {path}"
        )


def check_output_size(text: str, max_mb: float) -> None:
    """检查输出文本大小是否超限"""
    size_mb = len(text.encode('utf-8')) / (1024 * 1024)
    if size_mb > max_mb:
        raise ValueError(
            f"output too large: {size_mb:.1f}MB (max {max_mb}MB)"
        )


def resolve_data_path(path: str, data_root: str | None = None) -> tuple[Path, Path]:
    root = Path(data_root).resolve() if data_root else DEFAULT_DATA_ROOT.resolve()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escapes data root: {path}") from exc
    return candidate, root


# 导出复合Skill（供B3动态发现和直接调用）
try:
    from skills.composite_skills import read_and_convert, analyze_and_convert
    __all__ = [
        "resolve_data_path",
        "ResourceLimits",
        "check_file_size",
        "check_output_size",
        "read_and_convert",
        "analyze_and_convert",
    ]
except ImportError:
    __all__ = [
        "resolve_data_path",
        "ResourceLimits",
        "check_file_size",
        "check_output_size",
    ]


# 在文件末尾添加
try:
    from skills.code_executor import code_executor
    __all__.append("code_executor")
except ImportError:
    pass