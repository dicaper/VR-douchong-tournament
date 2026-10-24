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


for path in FILES:
    text = path.read_text(encoding="utf-8")

    replacements = [
        'const API_BASE = "/api/tournament";',
        "const API_BASE = '/api/tournament';",
        (
            'const API_BASE = '
            '"https://nameless-grass-c2e3.1791870448.workers.dev'
            '/api/tournament";'
        ),
    ]

    for old in replacements:
        text = text.replace(
            old,
            f'const API_BASE = "{WORKER_API}";',
        )

    with path.open(
        "w",
        encoding="utf-8",
        newline="\n",
    ) as file:
        file.write(text)

    print(f"已处理：{path}")