from pathlib import Path
import re
import shutil
from datetime import datetime


ROOT = Path(__file__).resolve().parent
FILES = [
    ROOT / "static" / "swiss.html",
    ROOT / "static" / "swiss_bracket.html",
    ROOT / "static" / "playoffs.html",
]

BACKUP_DIR = ROOT / "backup_before_remove_commercial"
BACKUP_DIR.mkdir(exist_ok=True)


def remove_balanced_element(text, marker):
    """
    删除从包含 marker 的 <div ...> 开始的完整 div 元素。
    适合删除嵌套的奖金池区域。
    """
    start = text.find(marker)
    if start < 0:
        return text, False

    div_start = text.rfind("<div", 0, start)
    if div_start < 0:
        return text, False

    depth = 0
    position = div_start

    while True:
        next_open = text.find("<div", position)
        next_close = text.find("</div>", position)

        if next_close < 0:
            return text, False

        if next_open >= 0 and next_open < next_close:
            depth += 1
            position = next_open + 4
        else:
            depth -= 1
            position = next_close + len("</div>")

            if depth == 0:
                return (
                    text[:div_start] + text[position:],
                    True,
                )


def remove_section_by_heading(text, heading):
    """
    删除包含指定文字的 section/article/div 块。
    主要用于旧版页面中没有 prize-pool class 的情况。
    """
    index = text.find(heading)
    if index < 0:
        return text, False

    start = text.rfind("<section", 0, index)
    if start < 0:
        start = text.rfind("<div", 0, index)

    if start < 0:
        return text, False

    div_start = text.find("<div", start)
    section_start = start if text[start:start + 8] == "<section" else div_start

    if section_start < 0:
        return text, False

    # 只处理 div 结构，避免误删页面其他内容
    if text[section_start:section_start + 7] != "<div":
        return text, False

    depth = 0
    position = section_start

    while True:
        next_open = text.find("<div", position)
        next_close = text.find("</div>", position)

        if next_close < 0:
            return text, False

        if next_open >= 0 and next_open < next_close:
            depth += 1
            position = next_open + 4
        else:
            depth -= 1
            position = next_close + len("</div>")

            if depth == 0:
                return (
                    text[:section_start] + text[position:],
                    True,
                )


def remove_sponsor_js(text):
    patterns = [
        r"\s*document\.getElementById\(['\"]sponsor-btn['\"]\)[\s\S]*?;\s*\n",
        r"\s*document\.getElementById\(['\"]sponsor-button['\"]\)[\s\S]*?;\s*\n",
    ]

    changed = False

    for pattern in patterns:
        new_text, count = re.subn(pattern, "\n", text, count=1)
        if count:
            changed = True
            text = new_text

    return text, changed


def add_license_notice(text):
    if "数据来源与许可声明" in text:
        return text, False

    notice = """
<div class="data-license">
    <strong>数据来源与许可声明</strong><br>
    本网站部分直播数据来自
    <a href="https://github.com/qianqiuzy/VR_douchong"
       target="_blank"
       rel="noopener noreferrer">
        VR_douchong
    </a>
    项目及其相关数据接口
    <code>vr.qianqiuzy.cn</code> /
    <code>psp.qianqiuzy.cn</code>。<br>
    数据许可说明请参阅
    <a href="https://github.com/qianqiuzy/VR_douchong/blob/main/LICENSE-DATA.md"
       target="_blank"
       rel="noopener noreferrer">
        LICENSE-DATA.md
    </a>。<br>
    本网站仅用于非商业兴趣展示和赛事娱乐用途。
    如需商业使用，请先取得数据权利人授权。
</div>
"""

    footer_end = text.rfind("</footer>")

    if footer_end >= 0:
        text = text[:footer_end] + notice + "\n" + text[footer_end:]
        return text, True

    body_end = text.rfind("</body>")

    if body_end >= 0:
        text = text[:body_end] + notice + "\n" + text[body_end:]
        return text, True

    return text, False


def main():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    print("开始删除奖金池、赞助和支付入口...\n")

    for file_path in FILES:
        if not file_path.exists():
            print(f"[跳过] 文件不存在：{file_path}")
            continue

        backup_path = BACKUP_DIR / f"{file_path.stem}_{timestamp}.html"
        shutil.copy2(file_path, backup_path)

        text = file_path.read_text(encoding="utf-8")
        original = text

        # 删除新版奖金池区域
        text, _ = remove_balanced_element(text, 'class="prize-pool"')

        # 删除旧版可能存在的奖金池区域
        text, _ = remove_section_by_heading(text, "奖金池")

        # 删除赞助按钮所在的常见 div
        text, _ = remove_balanced_element(text, 'class="sponsor-button"')

        # 删除赞助相关 JS
        text, _ = remove_sponsor_js(text)

        # 保留并添加数据来源声明
        text, _ = add_license_notice(text)

        if text != original:
            file_path.write_text(text, encoding="utf-8")
            print(f"[完成] {file_path}")
            print(f"       备份：{backup_path}")
        else:
            print(f"[无需修改] {file_path}")

    print("\n处理完成。")
    print("如果页面出现问题，可以从 backup_before_remove_commercial 文件夹恢复备份。")


if __name__ == "__main__":
    main()