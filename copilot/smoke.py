"""Live smoke test against the real Liara API (read-only, changes nothing).

    python -m copilot.smoke            # checks every read endpoint
    python -m copilot.smoke my-app     # also checks logs / metrics / releases of my-app
"""
import asyncio
import sys

from . import config
from .diagnosis import probe
from .liara_client import LiaraClient


async def main(app: str | None):
    if not config.LIARA_API_TOKEN:
        print("✗ LIARA_API_TOKEN در فایل .env خالی است.")
        return
    c = LiaraClient()
    checks = [("حساب (/v1/me)", c.me()), ("لیست اپ‌ها", c.list_apps()), ("پلن‌ها", c.list_plans())]
    if app:
        checks += [("جزئیات اپ", c.get_app(app)), ("ریلیزها", c.list_releases(app, 3)),
                   ("لاگ‌ها", c.logs(app, minutes=60)), ("متریک CPU/RAM", c.metrics(app)),
                   ("دامنه‌ها", c.list_domains(app)), ("تست HTTP سایت", probe(c, app))]
    for name, coro in checks:
        try:
            r = await coro
            n = len(r) if isinstance(r, (list, dict)) else ""
            extra = ""
            if name == "لیست اپ‌ها":
                extra = " → " + ", ".join(a.get("project_id") or a.get("name") for a in r)
            if name == "متریک CPU/RAM":
                extra = f" → {r.get('metrics')}"
            if name == "لاگ‌ها" and r:
                extra = f" → آخرین خط: {r[-1]['line'][:80]}"
            print(f"✓ {name} ({n}){extra}")
        except Exception as e:  # noqa
            print(f"✗ {name}: {getattr(e, 'human', lambda: str(e))()}  [{e}]")
    await c.close()
    if not config.LIARA_TEAM_ID:
        print("ℹ️ اگر اپ‌هایتان داخل «تیم» هستند و لیست خالی آمد، LIARA_TEAM_ID را در .env تنظیم کنید.")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))
