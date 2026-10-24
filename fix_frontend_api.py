from pathlib import Path


ROOT = Path(__file__).resolve().parent

WORKER_API = (
    "https://nameless-grass-c2e3.1791870448.workers.dev"
    "/api/tournament"
)

FILES = [
    ROOT / "static" / "swiss.html",
    ROOT / "static" / "swiss_bracket.html",
    ROOT / "static" / "playoffs.html",
]


def update_file(path: Path):
    text = path.read_text(encoding="utf-8")

    replacements = [
        (
            'const API_BASE = "/api/tournament";',
            f'const API_BASE = "{WORKER_API}";',
        ),
        (
            "const API_BASE = '/api/tournament';",
            f'const API_BASE = "{WORKER_API}";',
        ),
        (
            'const API_BASE = "https://nameless-grass-c2e3.1791870448.workers.dev/api/tournament";',
            f'const API_BASE = "{WORKER_API}";',
        ),
    ]

    changed = False

    for old, new in replacements:
        if old in text:
            text = text.replace(old, new)
            changed = True

    if not changed:
        print(f"[警告] 没有找到 API_BASE：{path}")
        return

    with path.open(
        "w",
        encoding="utf-8",
        newline="\n",
    ) as file:
        file.write(text)

    print(f"[完成] {path}")


for file_path in FILES:
    if not file_path.exists():
        print(f"[跳过] 文件不存在：{file_path}")
        continue

    update_file(file_path)