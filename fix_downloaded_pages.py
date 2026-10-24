from pathlib import Path


ROOT = Path(__file__).resolve().parent

FILES = [
    ROOT / "static" / "swiss.html",
    ROOT / "static" / "swiss_bracket.html",
    ROOT / "static" / "playoffs.html",
]

WORKER_API = (
    "https://nameless-grass-c2e3.1791870448.workers.dev"
    "/api/tournament"
)

for path in FILES:
    text = path.read_text(encoding="utf-8")

    # CloudBase Flask静态路径改为Pages根路径
    text = text.replace(
        "/static/avatars/",
        "/avatars/",
    )

    # API地址改为Cloudflare Worker
    text = text.replace(
        'const API_BASE = "/api/tournament";',
        f'const API_BASE = "{WORKER_API}";',
    )

    text = text.replace(
        "const API_BASE = '/api/tournament';",
        f'const API_BASE = "{WORKER_API}";',
    )

    # 如果原页面已经有旧Worker地址，也统一替换
    import re

    text = re.sub(
        r'const API_BASE\s*=\s*"https://[^"]+/api/tournament";',
        f'const API_BASE = "{WORKER_API}";',
        text,
    )

    with path.open(
        "w",
        encoding="utf-8",
        newline="\n",
    ) as file:
        file.write(text)

    print(f"已处理：{path}")