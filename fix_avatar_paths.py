from pathlib import Path


ROOT = Path(__file__).resolve().parent

FILES = [
    ROOT / "static" / "swiss.html",
    ROOT / "static" / "swiss_bracket.html",
    ROOT / "static" / "playoffs.html",
]

for path in FILES:
    text = path.read_text(encoding="utf-8")

    text = text.replace(
        "/static/avatars/",
        "/avatars/",
    )

    text = text.replace(
        "`/static/avatars/",
        "`/avatars/",
    )

    path.write_text(
        text,
        encoding="utf-8",
        newline="\n",
    )

    print(f"已修改头像路径：{path}")