"""
Ограниченный Python sandbox для нод воркфлоу ботов.

По умолчанию ВЫКЛЮЧЕН (ENABLE_PYTHON_SANDBOX=true чтобы включить).
Даже с AST-валидацией exec() — это RCE-вектор, включать только в доверенной
среде, например self-hosted-инсталляции где владелец = единственный автор воркфлоу.
"""
import ast
import logging
import os

log = logging.getLogger("chatbot")


_PY_SANDBOX_FORBIDDEN_NAMES = {
    "eval", "exec", "compile", "open", "__import__",
    "globals", "locals", "vars", "getattr", "setattr", "delattr",
    "input", "breakpoint", "exit", "quit", "help",
    "memoryview", "bytearray", "bytes", "type", "object",
    "super", "classmethod", "staticmethod", "property",
}

# Whitelist разрешённых AST-узлов. Всё что НЕ в списке — отвергается.
# Намеренно убраны: ClassDef, FunctionDef, AsyncFunctionDef, Lambda
#   (можно скрыть в них escape: class X: __init_subclass__ etc.),
# While (бесконечные циклы), Yield/YieldFrom, AsyncFor/AsyncWith,
# GeneratorExp без bound, Global, Nonlocal, Try (можно поглотить ошибку
# и продолжить вредоносный код), Import*, JoinedStr/FormattedValue
# (через f-string легче проворачивать атаки), Starred (распаковка может
# взорвать память), MatchClass, MatchStar.
_PY_SANDBOX_ALLOWED_NODES = {
    ast.Module, ast.Expression, ast.Expr,
    ast.Assign, ast.AugAssign, ast.AnnAssign,
    ast.For, ast.If, ast.Pass, ast.Break, ast.Continue,
    ast.Return, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare,
    ast.Call, ast.IfExp, ast.Subscript, ast.Attribute,
    ast.Name, ast.Load, ast.Store, ast.Del,
    ast.Constant, ast.List, ast.Tuple, ast.Set, ast.Dict,
    ast.ListComp, ast.SetComp, ast.DictComp,
    ast.comprehension, ast.Slice,
    ast.And, ast.Or, ast.Not,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
    ast.LShift, ast.RShift, ast.BitOr, ast.BitAnd, ast.BitXor,
    ast.UAdd, ast.USub, ast.Invert,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.Is, ast.IsNot, ast.In, ast.NotIn,
    ast.keyword, ast.arguments, ast.arg,
}

_PY_SANDBOX_MAX_CODE_LEN = 4000          # символов исходника
_PY_SANDBOX_MAX_NODES = 250              # узлов AST
_PY_SANDBOX_MAX_INT_LITERAL = 10**6      # литерал-числа
_PY_SANDBOX_TIMEOUT_SEC = 2              # wallclock timeout (Linux only)


def _ast_validate_python(code: str) -> str | None:
    """Возвращает текст ошибки, если код содержит запрещённые конструкции."""
    if len(code) > _PY_SANDBOX_MAX_CODE_LEN:
        return f"Код слишком длинный (>{_PY_SANDBOX_MAX_CODE_LEN} символов)"
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as e:
        return f"Синтаксическая ошибка: {e}"

    nodes = list(ast.walk(tree))
    if len(nodes) > _PY_SANDBOX_MAX_NODES:
        return f"Слишком сложный код (>{_PY_SANDBOX_MAX_NODES} узлов AST)"

    for node in nodes:
        if type(node) not in _PY_SANDBOX_ALLOWED_NODES:
            return f"Запрещённая конструкция: {type(node).__name__}"
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                return f"Доступ к скрытым атрибутам ({node.attr}) запрещён"
        if isinstance(node, ast.Name) and node.id in _PY_SANDBOX_FORBIDDEN_NAMES:
            return f"Использование {node.id} запрещено"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _PY_SANDBOX_FORBIDDEN_NAMES:
                return f"Вызов {node.func.id}() запрещён"
        if isinstance(node, ast.Constant):
            if isinstance(node.value, int) and abs(node.value) > _PY_SANDBOX_MAX_INT_LITERAL:
                return f"Слишком большое число ({node.value})"
            if isinstance(node.value, str) and len(node.value) > 10000:
                return "Строковый литерал слишком длинный"
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            return "Оператор ** запрещён в sandbox"
    return None


def _run_python_sandbox(code: str, input_text: str, ctx: dict) -> str:
    """Выполняет пользовательский Python код. По умолчанию ВЫКЛЮЧЕН.

    Даже с AST-валидацией exec() не безопасен — это RCE-вектор.
    Включать только в доверенной среде."""
    import json as _j
    if os.getenv("ENABLE_PYTHON_SANDBOX", "false").lower() not in ("1", "true", "yes"):
        return "[Python sandbox выключен. Установите ENABLE_PYTHON_SANDBOX=true]"

    err = _ast_validate_python(code)
    if err:
        return f"[Python sandbox: {err}]"

    log.warning(f"[Python sandbox] executing user code (len={len(code)})")
    safe_globals = {
        "__builtins__": {
            "len": len, "range": range, "str": str, "int": int, "float": float,
            "bool": bool, "list": list, "dict": dict, "tuple": tuple, "set": set,
            "sum": sum, "min": min, "max": max, "abs": abs, "round": round,
            "sorted": sorted, "reversed": reversed, "enumerate": enumerate, "zip": zip,
            "map": map, "filter": filter, "print": lambda *a, **k: None,
            "True": True, "False": False, "None": None,
            "isinstance": isinstance, "any": any, "all": all,
        },
        "json": _j,
        "re": __import__("re"),
        "datetime": __import__("datetime"),
    }
    ctx_copy = {k: v for k, v in ctx.items() if k not in ("bot", "history")}
    safe_locals = {
        "input_text": input_text,
        "ctx": ctx_copy,
        "output": "",
    }

    # Wallclock timeout через signal.alarm — работает только на Linux,
    # только в главном потоке. На прочих платформах просто выполняется без таймера.
    import signal as _sig
    _has_alarm = hasattr(_sig, "SIGALRM")
    _old_handler = None
    if _has_alarm:
        def _on_timeout(_sig_num, _frame):
            raise TimeoutError(f"Превышен лимит {_PY_SANDBOX_TIMEOUT_SEC}с")
        try:
            _old_handler = _sig.signal(_sig.SIGALRM, _on_timeout)
            _sig.alarm(_PY_SANDBOX_TIMEOUT_SEC)
        except (ValueError, OSError):
            _has_alarm = False
            _old_handler = None
    try:
        exec(code, safe_globals, safe_locals)
        out = safe_locals.get("output", "")
        if not isinstance(out, str):
            try: out = _j.dumps(out, ensure_ascii=False)
            except Exception: out = str(out)
        return out[:10000]
    except TimeoutError as e:
        log.error(f"[Python sandbox] timeout: {e}")
        return f"[Python sandbox: {e}]"
    except Exception as e:
        log.error(f"[Python sandbox] error: {type(e).__name__}")
        return f"[Ошибка Python: {type(e).__name__}]"
    finally:
        if _has_alarm:
            try:
                _sig.alarm(0)
                if _old_handler is not None:
                    _sig.signal(_sig.SIGALRM, _old_handler)
            except (ValueError, OSError):
                pass
