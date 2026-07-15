from __future__ import annotations

import ast
import os
import resource
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path


# ========== 内联资源限制配置 ==========
_MAX_CODE_LENGTH = 5000           # 最大代码长度（字符）
_MAX_EXECUTION_TIME = 10          # 最大执行时间（秒）
_MAX_MEMORY_MB = 128              # 最大内存（MB）
_ALLOWED_MODULES = {              # 允许导入的模块白名单
    "math", "random", "statistics", 
    "json", "re", "datetime", 
    "collections", "itertools", "functools",
    "typing", "decimal", "fractions",
    "string", "hashlib", "uuid",
}


# ========== 危险操作黑名单 ==========
_FORBIDDEN_IMPORTS = {
    "os", "sys", "subprocess", "shlex",
    "socket", "urllib", "http", "ftplib",
    "pickle", "marshal", "shelve",
    "ctypes", "ffi", "importlib",
    "builtins", "__builtin__",
    "pathlib", "glob", "fnmatch",
}

_FORBIDDEN_FUNCTIONS = {
    "eval", "exec", "compile", "__import__", 
    "open", "input", "raw_input",
    "breakpoint", "help", "dir", "globals", "locals",
}

_FORBIDDEN_ATTRIBUTES = {
    "read", "write", "open", "close",
    "system", "popen", "call", "run",
    "socket", "urlopen", "Request",
    "load", "loads", "dump", "dumps",
}


def _check_code_safety(code: str) -> None:
    """
    静态检查代码安全性。
    解析AST，禁止危险导入、函数调用、属性访问。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise ValueError(f"invalid python code: {exc}") from exc
    
    for node in ast.walk(tree):
        # 1. 禁止 import xxx（非白名单）
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_name = alias.name.split('.')[0]
                if module_name in _FORBIDDEN_IMPORTS:
                    raise ValueError(
                        f"import of '{module_name}' is not allowed (forbidden module)"
                    )
                if module_name not in _ALLOWED_MODULES:
                    raise ValueError(
                        f"import of '{module_name}' is not allowed (not in whitelist)"
                    )
        
        # 2. 禁止 from xxx import ...
        if isinstance(node, ast.ImportFrom):
            module_name = node.module.split('.')[0] if node.module else ''
            if module_name in _FORBIDDEN_IMPORTS:
                raise ValueError(
                    f"import from '{module_name}' is not allowed (forbidden module)"
                )
            if module_name not in _ALLOWED_MODULES:
                raise ValueError(
                    f"import from '{module_name}' is not allowed (not in whitelist)"
                )
        
        # 3. 禁止危险函数调用
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id in _FORBIDDEN_FUNCTIONS:
                    raise ValueError(
                        f"function '{node.func.id}' is not allowed"
                    )
        
        # 4. 禁止危险属性访问（如 os.system, file.read 等）
        if isinstance(node, ast.Attribute):
            if node.attr in _FORBIDDEN_ATTRIBUTES:
                raise ValueError(
                    f"attribute '{node.attr}' is not allowed"
                )
        
        # 5. 禁止 __ 开头的魔法方法/属性（防止绕过）
        if isinstance(node, ast.Attribute):
            if node.attr.startswith('__') and not node.attr.endswith('__'):
                raise ValueError(
                    f"private attribute '{node.attr}' is not allowed"
                )


def _set_resource_limits():
    """
    设置子进程资源限制（Linux only）。
    限制CPU时间和内存使用。
    """
    # 限制CPU时间（软限制和硬限制）
    resource.setrlimit(
        resource.RLIMIT_CPU, 
        (_MAX_EXECUTION_TIME, _MAX_EXECUTION_TIME + 1)
    )
    # 限制虚拟内存
    max_memory = _MAX_MEMORY_MB * 1024 * 1024
    resource.setrlimit(
        resource.RLIMIT_AS, 
        (max_memory, max_memory)
    )
    # 限制创建的文件大小
    resource.setrlimit(
        resource.RLIMIT_FSIZE,
        (1024 * 1024, 1024 * 1024)  # 最多1MB输出文件
    )
    # 限制子进程数
    resource.setrlimit(
        resource.RLIMIT_NPROC,
        (0, 0)  # 禁止创建子进程
    )


def _kill_process(proc: subprocess.Popen) -> None:
    """强制终止进程"""
    try:
        proc.kill()
    except:
        pass


def code_executor(code: str, timeout: int = 10) -> dict:
    """
    在受限沙箱中执行Python代码。
    
    Args:
        code: Python代码字符串
        timeout: 执行超时时间（秒），默认10秒
    
    Returns:
        包含执行结果的字典：
        {
            "returncode": 返回码,
            "stdout": 标准输出,
            "stderr": 标准错误,
            "execution_time_ms": 实际执行时间（毫秒）,
            "killed": 是否被强制终止,
        }
    """
    if not isinstance(code, str) or not code.strip():
        raise ValueError("code must be a non-empty string")
    if len(code) > _MAX_CODE_LENGTH:
        raise ValueError(f"code is too long (max {_MAX_CODE_LENGTH} chars)")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("timeout must be a positive integer")
    if timeout > _MAX_EXECUTION_TIME:
        timeout = _MAX_EXECUTION_TIME
    
    # 静态安全检查
    _check_code_safety(code)
    
    # 创建临时文件（隔离目录）
    temp_dir = tempfile.mkdtemp(prefix="sandbox_")
    temp_path = Path(temp_dir) / "script.py"
    
    try:
        # 写入代码到临时文件
        temp_path.write_text(code, encoding="utf-8")
        
        # 使用subprocess隔离执行
        start_time = time.time()
        
        proc = subprocess.Popen(
            ["python", str(temp_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=_set_resource_limits,  # 设置资源限制（Linux）
            cwd=temp_dir,  # 限制工作目录
        )
        
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            killed = False
        except subprocess.TimeoutExpired:
            _kill_process(proc)
            stdout, stderr = proc.communicate()
            killed = True
        
        execution_time_ms = round((time.time() - start_time) * 1000, 3)
        
        # ===== 修复：如果被强制终止，抛出异常 =====
        if killed or proc.returncode == -9:
            raise TimeoutError(
                f"code execution terminated: timeout or resource limit exceeded "
                f"(execution_time: {execution_time_ms}ms)"
            )
        # ==========================================
        
        return {
            "returncode": proc.returncode,
            "stdout": stdout[:10000] if stdout else "",  # 限制输出长度
            "stderr": stderr[:5000] if stderr else "",   # 限制错误输出长度
            "execution_time_ms": execution_time_ms,
            "killed": False,
            "limits": {
                "max_execution_time_sec": _MAX_EXECUTION_TIME,
                "max_memory_mb": _MAX_MEMORY_MB,
                "max_code_length": _MAX_CODE_LENGTH,
            }
        }
        
    except Exception as exc:
        raise RuntimeError(f"code execution failed: {exc}") from exc
        
    finally:
        # 清理临时文件和目录
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except:
            pass