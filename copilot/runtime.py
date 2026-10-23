"""Works out *how* a project must be started on Liara (start command, port, platform config)
so the first deploy comes up healthy instead of UNHEALTHY / 502.

Rules come from Liara's official docs (github.com/liara-ir/docs):
  * python platform: default command is `python3 main.py`; a custom command goes in `args`
    (liara.json "args": ["python3 app.py"]); port defaults to 3000 unless given; apps that are
    not web servers need no port. Dependencies are installed from requirements.txt only.
  * flask platform: gunicorn serves `app:app` by default; other modules need `appModule`.
  * FastAPI: run on the python platform with an ASGI server.
  * The app must listen on 0.0.0.0 (127.0.0.1/localhost is unreachable inside the container).
A project's own liara.json always wins.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

SKIP_DIRS = {"venv", ".venv", "env", "node_modules", ".git", "__pycache__", "site-packages", "dist", "build",
             "tests", "test", "migrations"}

STDLIB = {"os", "sys", "re", "json", "time", "datetime", "math", "random", "asyncio", "logging", "typing",
          "pathlib", "subprocess", "collections", "itertools", "functools", "dataclasses", "enum", "uuid",
          "hashlib", "base64", "threading", "queue", "socket", "http", "urllib", "email", "csv", "sqlite3",
          "shutil", "tempfile", "glob", "io", "traceback", "inspect", "copy", "string", "textwrap", "abc",
          "contextlib", "argparse", "configparser", "secrets", "statistics", "decimal", "fractions", "zipfile",
          "tarfile", "pickle", "pprint", "signal", "platform", "ssl", "struct", "unittest", "warnings", "weakref",
          "html", "xml", "concurrent", "multiprocessing", "operator", "heapq", "bisect", "array", "codecs",
          "locale", "gettext", "calendar", "zoneinfo", "ast", "types", "importlib", "getpass", "wave", "mimetypes",
          "select", "selectors", "ipaddress", "hmac", "binascii", "gzip", "bz2", "lzma", "shlex", "difflib", "__future__"}

IMPORT_TO_PIP = {"dotenv": "python-dotenv", "telebot": "pyTelegramBotAPI", "telegram": "python-telegram-bot",
                 "yaml": "PyYAML", "PIL": "Pillow", "cv2": "opencv-python-headless", "sklearn": "scikit-learn",
                 "bs4": "beautifulsoup4", "jwt": "PyJWT", "dateutil": "python-dateutil", "psycopg2": "psycopg2-binary",
                 "MySQLdb": "mysqlclient", "jose": "python-jose", "multipart": "python-multipart",
                 "google": "google-api-python-client", "openai": "openai", "fastapi": "fastapi", "uvicorn": "uvicorn",
                 "flask": "flask", "requests": "requests", "httpx": "httpx", "aiohttp": "aiohttp", "pydantic": "pydantic",
                 "sqlalchemy": "SQLAlchemy", "redis": "redis", "pymongo": "pymongo", "aiogram": "aiogram",
                 "pyrogram": "pyrogram", "telethon": "telethon", "langchain": "langchain", "numpy": "numpy",
                 "pandas": "pandas", "starlette": "starlette", "jinja2": "Jinja2", "boto3": "boto3"}


def _py_files(root: Path):
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
            continue
        yield p, rel


def _read(p: Path) -> str:
    try:
        return p.read_text(errors="ignore")
    except OSError:
        return ""


def _module(rel: Path) -> str:
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def read_liara_json(root: Path) -> dict:
    p = root / "liara.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def requirements(root: Path) -> list[str]:
    p = root / "requirements.txt"
    if not p.exists():
        return []
    out = []
    for l in _read(p).splitlines():
        l = l.strip()
        if l and not l.startswith(("#", "-")):
            out.append(re.split(r"[<>=!~\[; ]", l)[0].lower())
    return out


def third_party_imports(root: Path) -> set[str]:
    local = {rel.parts[0].replace(".py", "") for _, rel in _py_files(root)}
    found = set()
    for p, _ in _py_files(root):
        for m in re.finditer(r"^\s*(?:from|import)\s+([A-Za-z_][\w]*)", _read(p), re.M):
            name = m.group(1)
            if name not in STDLIB and name not in local:
                found.add(name)
    return found


def _asgi_target(root: Path, mod_app: str) -> tuple[str, str | None]:
    """Decide how uvicorn must import the app.

    `backend/app.py` that does `from config import settings` (sibling module, imported as top-level)
    only works when started *inside* backend/ — exactly how it runs on the developer's machine
    (`cd backend && uvicorn app:app`). From the project root that is a ModuleNotFoundError and the
    release goes UNHEALTHY. In that case we keep the cwd at the root and add `--app-dir backend`.
    Package-style imports (`from backend.x import` / relative `from .x import`) keep `backend.app:app`.
    """
    mod, var = mod_app.split(":")
    parts = mod.split(".")
    if len(parts) == 1:
        return mod_app, None
    pkg_dir = root.joinpath(*parts[:-1])
    siblings = {p.stem for p in pkg_dir.glob("*.py")} | {p.name for p in pkg_dir.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))}
    top = parts[0]
    txt = "\n".join(_read(p) for p in pkg_dir.rglob("*.py") if not any(x in SKIP_DIRS for x in p.parts))
    package_style = re.search(rf"^\s*(?:from|import)\s+{re.escape(top)}[\.\s]", txt, re.M) or \
        re.search(r"^\s*from\s+\.", txt, re.M)
    sibling_style = any(re.search(rf"^\s*(?:from|import)\s+{re.escape(sib)}\b", txt, re.M)
                        for sib in siblings if sib not in ("__init__", parts[-1]))
    if sibling_style and not package_style:
        return f"{parts[-1]}:{var}", "/".join(parts[:-1])
    return mod_app, None


def plan_runtime(root: Path, platform: str) -> dict:
    """Returns {port, args, platform_config, is_web, source, notes[], warnings[], inject_requirements[]}."""
    lj = read_liara_json(root)
    out = {"port": None, "args": None, "platform_config": {}, "is_web": True, "source": "auto",
           "notes": [], "warnings": [], "inject_requirements": [], "liara_json": bool(lj)}

    if platform in ("python", "flask", "django"):
        _python(root, platform, out)
    elif platform in ("node", "next"):
        _node(root, out)

    # liara.json in the project overrides everything we guessed
    if lj:
        if lj.get("port"):
            out["port"] = int(lj["port"])
        if lj.get("args"):
            out["args"] = lj["args"] if isinstance(lj["args"], list) else [str(lj["args"])]
        for key in ("python", "flask", "django", "node", "next"):
            if isinstance(lj.get(key), dict):
                out["platform_config"] = {**out["platform_config"], **lj[key]}
        out["source"] = "liara.json"
        out["notes"].append("تنظیمات اجرا از فایل ‎liara.json‎ پروژه خوانده شد.")
    return out


def _python(root: Path, platform: str, out: dict):
    reqs = requirements(root)
    imports = third_party_imports(root)
    files = list(_py_files(root))

    sub_reqs = [p for p in sorted(root.glob("*/requirements.txt")) if p.parent.name not in SKIP_DIRS]
    if not (root / "requirements.txt").exists() and sub_reqs:
        lines = [l.strip() for l in _read(sub_reqs[0]).splitlines() if l.strip() and not l.strip().startswith(("#", "-"))]
        out["inject_requirements"] += lines
        reqs = [re.split(r"[<>=!~\[; ]", l)[0].lower() for l in lines]
        rel = sub_reqs[0].relative_to(root).as_posix()
        out["warnings"].append(f"فایل وابستگی‌ها در ‎{rel}‎ است ولی لیارا فقط ‎requirements.txt‎ ریشه را نصب می‌کند؛ "
                               "محتوای آن را در ریشه‌ی بسته‌ی آپلود قرار می‌دهم.")
    if not (root / "requirements.txt").exists() and not sub_reqs and imports:
        pkgs = sorted({IMPORT_TO_PIP.get(i, i) for i in imports})
        out["inject_requirements"] += pkgs
        out["warnings"].append("فایل ‎requirements.txt‎ ندارید؛ لیارا فقط از همین فایل پکیج نصب می‌کند. "
                               f"بر اساس importها یک نسخه ساختم و داخل بسته‌ی آپلود قرار می‌دهم: {', '.join(pkgs)}")
    if (root / "requirements.txt").exists() or sub_reqs:
        missing = sorted({IMPORT_TO_PIP.get(i, i) for i in imports
                          if IMPORT_TO_PIP.get(i, i).lower() not in reqs and i.lower() not in reqs
                          and i in IMPORT_TO_PIP})
        if missing:
            out["inject_requirements"] += missing
            out["warnings"].append(f"این پکیج‌ها import شده‌اند ولی در ‎requirements.txt‎ نیستند و به بسته‌ی آپلود اضافه می‌شوند: {', '.join(missing)}")

    # bindings to localhost never work inside a container
    for p, rel in files:
        txt = _read(p)
        if re.search(r"""(?:\.run|uvicorn\.run|run_app|app\.run)\([^)]*host\s*=\s*["'](?:127\.0\.0\.1|localhost)["']""", txt):
            out["warnings"].append(f"در ‎{rel}‎ سرور روی ‎127.0.0.1‎ گوش می‌دهد؛ روی لیارا باید ‎0.0.0.0‎ باشد"
                                   + (" (با دستور اجرای پیشنهادی من این مشکل دور زده می‌شود)." if platform == "python" else "."))

    if platform == "flask":
        for p, rel in files:
            m = re.search(r"^(\w+)\s*=\s*Flask\(", _read(p), re.M)
            if m:
                mod = f"{_module(rel)}:{m.group(1)}"
                if mod != "app:app":
                    out["platform_config"]["appModule"] = mod
                    out["notes"].append(f"ماژول Flask: ‎{mod}‎ (به‌جای پیش‌فرض app:app)")
                break
        out["port"] = 80
        return
    if platform == "django":
        out["port"] = 80
        return

    # ---- generic python platform
    asgi = None
    for p, rel in files:
        txt = _read(p)
        m = re.search(r"^(\w+)\s*(?::[^=]+)?=\s*(FastAPI|Starlette|Quart)\(", txt, re.M)
        if m:
            asgi = (f"{_module(rel)}:{m.group(1)}", m.group(2))
            break
    if asgi:
        port = 8000
        target, app_dir = _asgi_target(root, asgi[0])
        cmd = f"uvicorn {target} --host 0.0.0.0 --port {port}" + (f" --app-dir {app_dir}" if app_dir else "")
        out.update(port=port, args=[cmd])
        out["notes"].append(f"برنامه‌ی {asgi[1]} است؛ با uvicorn روی ‎0.0.0.0:{port}‎ اجرا می‌شود (‎{target}‎"
                            + (f"، از داخل پوشه‌ی ‎{app_dir}‎ چون importهای آن نسبت به همین پوشه نوشته شده‌اند" if app_dir else "") + ").")
        if "uvicorn" not in reqs and "uvicorn" not in out["inject_requirements"]:
            out["inject_requirements"].append("uvicorn")
            out["warnings"].append("پکیج uvicorn در requirements نیست؛ به بسته‌ی آپلود اضافه می‌شود.")
        return

    # flask app living on the python platform
    for p, rel in files:
        m = re.search(r"^(\w+)\s*=\s*Flask\(", _read(p), re.M)
        if m:
            port = 8000
            mod = f"{_module(rel)}:{m.group(1)}"
            out.update(port=port, args=[f"gunicorn {mod} --bind 0.0.0.0:{port}"])
            out["notes"].append(f"برنامه‌ی Flask با gunicorn روی پورت {port} اجرا می‌شود (‎{mod}‎).")
            if "gunicorn" not in reqs:
                out["inject_requirements"].append("gunicorn")
            return

    # plain script: Liara runs `python3 main.py` unless told otherwise
    entry = None
    for cand in ("main.py", "app.py", "bot.py", "run.py", "server.py", "manage.py", "__main__.py"):
        if (root / cand).exists():
            entry = cand
            break
    if not entry:
        mains = [rel for p, rel in files if re.search(r"""if\s+__name__\s*==\s*["']__main__["']""", _read(p))]
        entry = str(mains[0]) if mains else None
    if entry and entry != "main.py":
        out["args"] = [f"python3 {entry}"]
        out["notes"].append(f"فایل اجرایی ‎{entry}‎ است (لیارا به‌طور پیش‌فرض main.py را اجرا می‌کند).")
    elif not entry:
        out["warnings"].append("فایل اجرایی (main.py) پیدا نشد؛ لیارا ‎python3 main.py‎ را اجرا می‌کند و برنامه بالا نمی‌آید.")

    all_txt = "\n".join(_read(p) for p, _ in files)
    web = re.search(r"(HTTPServer|web\.run_app|\.serve_forever|socketserver|uvicorn\.run|\.listen\()", all_txt)
    bot = re.search(r"(telebot|aiogram|python-telegram-bot|telegram\.ext|pyrogram|telethon|infinity_polling|run_polling|start_polling)", all_txt)
    if bot and not web:
        out["is_web"] = False
        out["notes"].append("برنامه وب‌سرور نیست (مثلاً ربات)؛ پورت لازم ندارد و تست HTTP انجام نمی‌شود.")
        return
    m = re.search(r"port\s*=\s*(?:int\()?(?:os\.\w+\.get\([^,]+,\s*)?['\"]?(\d{2,5})", all_txt)
    out["port"] = int(m.group(1)) if m else 3000


def _node(root: Path, out: dict):
    try:
        pj = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except Exception:
        return
    if "start" not in pj.get("scripts", {}):
        main = pj.get("main") or next((f for f in ("server.js", "index.js", "app.js", "main.js") if (root / f).exists()), None)
        if main:
            out["args"] = [f"node {main}"]
            out["notes"].append(f"اسکریپت start ندارید؛ برنامه با ‎node {main}‎ اجرا می‌شود.")
        else:
            out["warnings"].append("اسکریپت start در package.json نیست و فایل اصلی پیدا نشد.")
