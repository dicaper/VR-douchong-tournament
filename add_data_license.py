from pathlib import Path
import shutil
from datetime import datetime


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"

FILES = [
    STATIC / "swiss.html",
    STATIC / "swiss_bracket.html",
    STATIC / "playoffs.html",
]

BACKUP_DIR = ROOT / "backup_before_license"
BACKUP_DIR.mkdir(exist_ok=True)

timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

LICENSE_CSS = r"""
.data-license {
    width: min(1100px, 94%);
    margin: 28px auto 0;
    padding: 16px 20px;
    color: #9aaabd;
    background: #101722;
    border: 1px solid #33445a;
    border-radius: 10px;
    font-size: 13px;
    line-height: 1.8;
    text-align: center;
}

.data-license strong {
    color: #eef4fb;
}

.data-license a {
    color: #38bdf8;
    text-decoration: none;
}

.data-license a:hover {
    text-decoration: underline;
}

.data-license code {
    color: #d7e3f1;
    background: #182231;
    padding: 2px 5px;
    border-radius: 4px;
}
"""

LICENSE_HTML = r"""
<div class="data-license">
    <strong>数据来源与许可声明</strong><br>

    本网站部分直播数据来自
    <a
        href="https://github.com/qianqiuzy/VR_douchong"
        target="_blank"
        rel="noopener noreferrer"
    >
        VR_douchong
    </a>
    项目及其相关数据接口
    <code>vr.qianqiuzy.cn</code> /
    <code>psp.qianqiuzy.cn</code>。<br>

    数据许可说明请参阅
    <a
        href="https://github.com/qianqiuzy/VR_douchong/blob/main/LICENSE-DATA.md"
        target="_blank"
        rel="noopener noreferrer"
    >
        LICENSE-DATA.md
    </a>。<br>

    本网站仅用于非商业兴趣展示和赛事娱乐用途。
    如需将相关数据用于商业宣传、商业服务、广告、
    付费服务或其他商业目的，请先取得数据权利人授权。
</div>
"""

FOOTER_HTML = f"""
<footer>
    <p>VR斗虫大赛 2026 · 非商业兴趣项目</p>
    {LICENSE_HTML}
</footer>
"""


def remove_old_license(text):
    start_marker = '<div class="data-license">'
    end_marker = "</div>"

    while start_marker in text:
        start = text.find(start_marker)

        depth = 0
        position = start

        while position < len(text):
            open_pos = text.find("<div", position)
            close_pos = text.find("</div>", position)

            if close_pos == -1:
                break

            if open_pos != -1 and open_pos < close_pos:
                depth += 1
                position = open_pos + 4
            else:
                depth -= 1
                position = close_pos + len("</div>")

                if depth == 0:
                    text = text[:start] + text[position:]
                    break

    return text


def replace_footer(text):
    footer_start = text.lower().find("<footer")

    if footer_start >= 0:
        footer_end = text.lower().find("</footer>", footer_start)

        if footer_end >= 0:
            footer_end += len("</footer>")
            return (
                text[:footer_start]
                + FOOTER_HTML.strip()
                + text[footer_end:]
            )

    body_end = text.lower().find("</body>")

    if body_end >= 0:
        return (
            text[:body_end]
            + FOOTER_HTML.strip()
            + "\n"
            + text[body_end:]
        )

    return text + "\n" + FOOTER_HTML.strip() + "\n"


def add_css(text):
    if ".data-license {" in text:
        return text

    style_end = text.lower().rfind("</style>")

    if style_end >= 0:
        return (
            text[:style_end]
            + "\n"
            + LICENSE_CSS.strip()
            + "\n"
            + text[style_end:]
        )

    return text


def update_swiss_subtitle(text):
    if "非商业兴趣展示" in text:
        return text

    replacements = [
        (
            "比赛详情 · 当前轮次 · 实时支持率",
            "比赛详情 · 当前轮次 · 实时支持率 · 非商业兴趣展示",
        ),
        (
            "瑞士轮阶段",
            "瑞士轮阶段 · 非商业兴趣展示",
        ),
    ]

    for old, new in replacements:
        if old in text:
            text = text.replace(old, new, 1)
            break

    return text


def process_file(file_path):
    if not file_path.exists():
        print(f"[跳过] 文件不存在：{file_path}")
        return

    backup_path = BACKUP_DIR / f"{file_path.stem}_{timestamp}.html"
    shutil.copy2(file_path, backup_path)

    text = file_path.read_text(encoding="utf-8")

    # 防止之前重复添加
    text = remove_old_license(text)

    # 添加 CSS
    text = add_css(text)

    # 替换 footer
    text = replace_footer(text)

    # 只修改 swiss.html 的标题
    if file_path.name == "swiss.html":
        text = update_swiss_subtitle(text)

    file_path.write_text(text, encoding="utf-8")

    print(f"[完成] {file_path}")
    print(f"       备份文件：{backup_path}")


def main():
    print("开始添加数据来源、署名和非商业用途声明...\n")

    for file_path in FILES:
        process_file(file_path)

    print("\n全部处理完成。")
    print(f"原文件备份在：{BACKUP_DIR}")


if __name__ == "__main__":
    main()