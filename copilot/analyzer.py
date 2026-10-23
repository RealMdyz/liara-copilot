"""Project analyzer: understands a codebase and recommends how to run it on Liara.

Platform detection follows the official liara-cli rules (src/utils/detect-platform.ts),
so the platform we pick is exactly what Liara expects.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path

IGNORE_DIRS = {"node_modules", ".git", "__pycache__", "venv", ".venv", "env", ".next/cache",
               "dist/cache", ".idea", ".vscode", ".pytest_cache", "vendor"}
IGNORE_FILES = {".env", ".env.local", ".DS_Store"}

DEFAULT_PORT = {"static": 80, "python": 80, "react": 80, "vue": 80, "angular": 80, "laravel": 80,
                "wordpress": 80, "django": 80, "flask": 80, "php": 80, "next": 3000, "node": 3000,
                "dotnet": 80, "go": 8080, "docker": 3000}

HEAVY_DEPS = {"puppeteer", "playwright", "tensorflow", "torch", "@tensorflow/tfjs-node", "sharp",
              "opencv-python", "pandas", "scikit-learn", "transformers", "celery"}

PLATFORM_FA = {"node": "Node.js", "next": "Next.js", "react": "React (استاتیک)", "vue": "Vue (استاتیک)",
               "angular": "Angular (استاتیک)", "django": "Django", "flask": "Flask", "python": "Python",
               "laravel": "Laravel", "php": "PHP", "go": "Go", "dotnet": ".NET", "docker": "Docker",
               "static": "سایت استاتیک (HTML)", "wordpress": "WordPress"}


def fetch_repo(source: str) -> Path:
    """Accept a local folder or a git URL (cloned shallowly into a temp dir)."""
    if re.match(r"^(https?://|git@)", source):
        dest = Path(tempfile.mkdtemp(prefix="copilot-src-"))
        subprocess.run(["git", "clone", "--depth", "1", source, str(dest)], check=True,
                       capture_output=True, timeout=180)
        return dest
    p = Path(source).expanduser().resolve()
    if not p.is_dir():
        raise FileNotFoundError(f"پوشه‌ی پروژه پیدا نشد: {source}")
    return p


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def detect_platform(root: Path) -> tuple[str, list[str]]:
    """Returns (platform, reasons)."""
    has = lambda n: (root / n).exists()
    if list(root.glob("**/*.csproj"))[:1] and not has("Dockerfile"):
        return "dotnet", ["فایل ‎*.csproj‎ پیدا شد"]
    if has("composer.json"):
        cj = _read_json(root / "composer.json")
        req = cj.get("require", {})
        if "laravel/framework" in req or "laravel/lumen-framework" in req:
            return "laravel", ["composer.json شامل laravel/framework است"]
        return "php", ["composer.json پیدا شد"]
    if has("index.php"):
        return "php", ["index.php پیدا شد"]
    if has("pyproject.toml"):
        return "python", ["pyproject.toml پیدا شد"]
    if has("requirements.txt"):
        req = (root / "requirements.txt").read_text(errors="ignore").lower()
        if "django" in req:
            return "django", ["requirements.txt شامل Django است"]
        if "flask" in req:
            return "flask", ["requirements.txt شامل Flask است"]
        return "python", ["requirements.txt پیدا شد"]
    if has("package.json"):
        pj = _read_json(root / "package.json")
        deps = {**pj.get("dependencies", {}), **pj.get("devDependencies", {})}
        if has("Dockerfile"):
            return "docker", ["هم Dockerfile و هم package.json وجود دارد؛ Docker انتخاب شد"]
        if "next" in pj.get("dependencies", {}):
            return "next", ["وابستگی next در package.json"]
        if "@angular/core" in deps:
            return "angular", ["وابستگی ‎@angular/core‎"]
        if "@vue/cli-service" in deps or ("vue" in deps and "vite" in deps):
            return "vue", ["وابستگی vue"]
        if "react-scripts" in deps or ("react" in deps and "vite" in deps and "express" not in deps):
            return "react", ["پروژه‌ی React با build استاتیک"]
        return "node", ["package.json پیدا شد (Node.js)"]
    if has("go.mod"):
        return "go", ["go.mod پیدا شد"]
    if has("Dockerfile"):
        return "docker", ["Dockerfile پیدا شد"]
    if has("index.html"):
        return "static", ["فقط فایل‌های HTML/CSS/JS"]
    return "docker", ["نوع پروژه مشخص نشد؛ بهتر است Dockerfile اضافه شود"]


ENV_PATTERNS = [
    re.compile(r"process\.env\.([A-Z][A-Z0-9_]+)"),
    re.compile(r"process\.env\[['\"]([A-Z][A-Z0-9_]+)['\"]\]"),
    re.compile(r"os\.environ(?:\.get)?\(?\[?['\"]([A-Z][A-Z0-9_]+)['\"]"),
    re.compile(r"os\.getenv\(['\"]([A-Z][A-Z0-9_]+)['\"]"),
    re.compile(r"env\(['\"]([A-Z][A-Z0-9_]+)['\"]"),
    re.compile(r"os\.Getenv\(\"([A-Z][A-Z0-9_]+)\"\)"),
]
SKIP_ENVS = {"PORT", "NODE_ENV", "HOME", "PATH", "PWD", "DEBUG", "HOSTNAME"}
CODE_EXT = {".js", ".ts", ".mjs", ".cjs", ".py", ".php", ".go", ".jsx", ".tsx"}


def iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.startswith(".")]
        for fn in filenames:
            yield Path(dirpath) / fn


def find_env_vars(root: Path) -> dict[str, str]:
    """Return {ENV_NAME: 'file:line'} referenced in code."""
    found: dict[str, str] = {}
    for f in iter_files(root):
        if f.suffix not in CODE_EXT or f.stat().st_size > 400_000:
            continue
        try:
            for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                for pat in ENV_PATTERNS:
                    for m in pat.finditer(line):
                        k = m.group(1)
                        if k not in SKIP_ENVS and k not in found:
                            found[k] = f"{f.relative_to(root)}:{i}"
        except OSError:
            continue
    # values present in .env.example are documented
    return found


def _parse_env_file(p: Path) -> dict[str, str]:
    out = {}
    for line in p.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.replace("export ", "").strip()
        v = v.strip().strip('"').strip("'")
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
            out[k] = v
    return out


def dotenv_values(root: Path) -> dict[str, str]:
    """Values from the project's own .env / .env.local (never uploaded; used to pre-fill env vars)."""
    out: dict[str, str] = {}
    for name in (".env", ".env.local", ".env.production"):
        p = root / name
        if p.exists():
            out.update(_parse_env_file(p))
    return out


def example_env(root: Path) -> dict[str, str]:
    out = {}
    for name in (".env.example", ".env.sample", "env.example"):
        p = root / name
        if p.exists():
            for line in p.read_text(errors="ignore").splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip()
    return out


def detect_port(root: Path, platform: str) -> tuple[int, str]:
    pats = [re.compile(r"\.listen\(\s*(\d{2,5})"), re.compile(r"PORT\s*\|\|\s*(\d{2,5})"),
            re.compile(r"PORT['\"]?\s*,\s*(\d{2,5})"), re.compile(r"port\s*=\s*(\d{2,5})")]
    for f in iter_files(root):
        if f.suffix not in CODE_EXT:
            continue
        try:
            txt = f.read_text(errors="ignore")
        except OSError:
            continue
        for p in pats:
            m = p.search(txt)
            if m:
                return int(m.group(1)), f"{f.relative_to(root)}"
    return DEFAULT_PORT.get(platform, 3000), "پیش‌فرض لیارا برای این پلتفرم"


def estimate_needs(root: Path, platform: str) -> dict:
    deps: set[str] = set()
    pj = _read_json(root / "package.json")
    deps |= set(pj.get("dependencies", {}))
    if (root / "requirements.txt").exists():
        deps |= {re.split(r"[<>=\[ ;]", l.strip())[0].lower()
                 for l in (root / "requirements.txt").read_text(errors="ignore").splitlines()
                 if l.strip() and not l.startswith("#")}
    heavy = sorted(deps & HEAVY_DEPS)
    size_mb = sum(f.stat().st_size for f in iter_files(root)) / 1e6

    if platform in ("static", "react", "vue", "angular"):
        ram, reason = 0.25, "سایت استاتیک است و فقط فایل سرو می‌کند"
    elif platform in ("next", "django", "laravel", "dotnet"):
        ram, reason = 1.0, f"فریم‌ورک {PLATFORM_FA.get(platform, platform)} در زمان اجرا حافظه‌ی بیشتری می‌خواهد"
    else:
        ram, reason = 0.5, "یک سرویس سبک بک‌اند است"
    if heavy:
        ram = max(ram, 2.0)
        reason += f"؛ کتابخانه‌های سنگین ({', '.join(heavy)}) دارد"
    if len(deps) > 60:
        ram = max(ram, 1.0)
        reason += f"؛ تعداد وابستگی‌ها زیاد است ({len(deps)})"
    return {"ram_gb": ram, "reason": reason, "deps": len(deps), "heavy": heavy, "size_mb": round(size_mb, 1)}


def _plan_ram(plan: dict) -> float | None:
    for key in ("RAM", "ram", "memory"):
        v = plan.get(key)
        if isinstance(v, dict):
            v = v.get("amount")
        if isinstance(v, (int, float)):
            return float(v) if v < 64 else float(v) / 1024  # MB -> GB
    return None


def _plan_price(plan: dict) -> float:
    """Monthly price in Toman (app plans expose hourly `price`; liara-cli multiplies by 720)."""
    from .liara_client import plan_price_month
    return plan_price_month(plan)


def recommend_plan(plans: dict, ram_needed: float) -> dict:
    """Cheapest available plan whose RAM >= need, plus one bigger alternative."""
    rows = []
    for pid, p in plans.items():
        if not isinstance(p, dict) or p.get("available") is False:
            continue
        r = _plan_ram(p)
        if r is None:
            continue
        rows.append({"id": pid, "ram_gb": r, "disk_gb": p.get("volume"), "cpu": (p.get("CPU") or {}).get("amount") if isinstance(p.get("CPU"), dict) else p.get("CPU"),
                     "price": _plan_price(p)})
    rows.sort(key=lambda x: (x["ram_gb"], x["price"]))
    if not rows:
        return {"id": None, "note": "لیست پلن‌ها از API قابل‌خواندن نبود؛ پلن را دستی انتخاب کنید."}
    pick = next((r for r in rows if r["ram_gb"] >= ram_needed), rows[-1])
    bigger = next((r for r in rows if r["ram_gb"] > pick["ram_gb"]), None)
    return {**pick, "alternative": bigger, "all": rows}


def analyze(source: str, plans: dict | None = None) -> dict:
    root = fetch_repo(source)
    platform, reasons = detect_platform(root)
    port, port_src = detect_port(root, platform)
    envs = find_env_vars(root)
    examples = example_env(root)
    dotenv = dotenv_values(root)
    needs = estimate_needs(root, platform)
    rec = recommend_plan(plans or {}, needs["ram_gb"]) if plans is not None else None
    warnings = []
    if not (root / ".gitignore").exists():
        warnings.append("فایل ‎.gitignore‎ ندارید؛ ممکن است فایل‌های اضافه آپلود شوند.")
    if (root / ".env").exists():
        warnings.append("فایل ‎.env‎ در پروژه هست؛ آن را آپلود نمی‌کنم و مقادیرش را به‌صورت متغیر محیطی امن تنظیم می‌کنیم.")
    if platform == "node" and "start" not in _read_json(root / "package.json").get("scripts", {}):
        warnings.append("اسکریپت start در package.json تعریف نشده؛ لیارا برای اجرا به ‎npm start‎ نیاز دارد.")
    return {
        "path": str(root), "platform": platform, "platform_fa": PLATFORM_FA.get(platform, platform),
        "reasons": reasons, "port": port, "port_source": port_src,
        "env_vars": {k: {"used_at": envs.get(k, ".env.example"), "example": examples.get(k, ""),
                         "in_dotenv": k in dotenv}
                     for k in list(envs) + [k for k in examples if k not in envs and k not in SKIP_ENVS]},
        "needs": needs, "plan": rec, "warnings": warnings,
    }


def package(root: str) -> str:
    """Create a tar.gz like liara-cli does, skipping heavy / secret files."""
    root_p = Path(root)
    fd, out = tempfile.mkstemp(suffix=".tar.gz", prefix="copilot-")
    os.close(fd)
    with tarfile.open(out, "w:gz") as tar:
        for f in iter_files(root_p):
            if f.name in IGNORE_FILES:
                continue
            tar.add(f, arcname=str(f.relative_to(root_p)))
    return out
