from pathlib import Path
from datetime import datetime
import shutil
import re


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"

FILES = [
    STATIC_DIR / "swiss.html",
    STATIC_DIR / "swiss_bracket.html",
    STATIC_DIR / "playoffs.html",
]

BACKUP_DIR = ROOT / "backup_before_move_license"
BACKUP_DIR.mkdir(exist_ok=True)

TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")


LICENSE_HTML = """
<div class="data-license-top">
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


LICENSE_CSS = """
.data-license-top {
    width: min(1100px, 94%);
    margin: 22px auto 0;
    padding: 14px 20px;
    color: #9aaabd;
    background: #101722;
    border: 1px solid #33445a;
    border-radius: 10px;
    font-size: 13px;
    line-height: 1.8;
    text-align: center;
}

.data-license-top strong {
    color: #eef4fb;
}

.data-license-top a {
    color: #38bdf8;
    text-decoration: none;
}

.data-license-top a:hover {
    text-decoration: underline;
}

.data-license-top code {
    color: #d7e3f1;
    background: #182231;
    padding: 2px 5px;
    border-radius: 4px;
}
"""


def remove_license_blocks(text):
    """
    删除已有的底部或顶部声明，防止重复添加。
    当前声明内容不含嵌套 div，因此正则足够安全。
    """
    pattern = re.compile(
        r'<div\s+class="data-license(?:-top)?"[^>]*>.*?</div>',
        re.IGNORECASE | re.DOTALL,
    )

    return pattern.sub("", text)


def add_css(text):
    if ".data-license-top {" in text:
        return text

    style_end = text.lower().rfind("</style>")

    if style_end < 0:
        return text

    return (
        text[:style_end]
        + "\n"
        + LICENSE_CSS.strip()
        + "\n"
        + text[style_end:]
    )


def insert_license_into_header(text):
    """
    将声明放到 header 的最后，也就是大标题和副标题下面。
    """
    header_end = text.lower().find("</header>")

    if header_end < 0:
        print("  [警告] 没有找到 </header>，跳过顶部插入")
        return text

    return (
        text[:header_end]
        + "\n"
        + LICENSE_HTML.strip()
        + "\n"
        + text[header_end:]
    )


def clean_footer_license_text(text):
    """
    删除旧声明后，保留普通 footer。
    """
    text = text.replace(
        "VR斗虫大赛 2026 · 非商业兴趣项目",
        "VR斗虫大赛 2026 · 非商业兴趣项目",
    )

    return text


def process_file(file_path):
    if not file_path.exists():
        print(f"[跳过] 文件不存在：{file_path}")
        return

    backup_path = BACKUP_DIR / (
        f"{file_path.stem}_{TIMESTAMP}.html"
    )

    shutil.copy2(file_path, backup_path)

    text = file_path.read_text(encoding="utf-8")

    # 删除旧声明
    text = remove_license_blocks(text)

    # 添加顶部声明 CSS
    text = add_css(text)

    # 插入到 header 下面
    text = insert_license_into_header(text)

    # 清理 footer 中残留内容
    text = clean_footer_license_text(text)

    file_path.write_text(text, encoding="utf-8")

    print(f"[完成] {file_path}")
    print(f"       原文件备份：{backup_path}")


def main():
    print("开始把数据来源声明移动到页面大标题下面...\n")

    for file_path in FILES:
        process_file(file_path)

    print("\n全部处理完成。")
    print(f"备份目录：{BACKUP_DIR}")


if __name__ == "__main__":
    main()