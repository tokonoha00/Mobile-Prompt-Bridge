"""index.html のJavaScript部分を抽出して構文チェック"""
import re

with open("web/index.html", "r", encoding="utf-8") as f:
    html = f.read()

# <script>...</script> の中身を取り出す
scripts = re.findall(r'<script>(.*?)</script>', html, re.DOTALL)

print(f"見つかったスクリプトブロック数: {len(scripts)}")

for i, script in enumerate(scripts):
    print(f"\n=== スクリプト {i+1} (長さ: {len(script)} 文字) ===")
    
    # 括弧のバランスチェック
    open_parens = script.count('(')
    close_parens = script.count(')')
    open_braces = script.count('{')
    close_braces = script.count('}')
    open_brackets = script.count('[')
    close_brackets = script.count(']')
    
    print(f"丸括弧 (): open={open_parens}, close={close_parens}, diff={open_parens - close_parens}")
    print(f"波括弧 {{}}: open={open_braces}, close={close_braces}, diff={open_braces - close_braces}")
    print(f"角括弧 []: open={open_brackets}, close={close_brackets}, diff={open_brackets - close_brackets}")
    
    if open_parens != close_parens:
        print("⚠️ 丸括弧の不一致あり!")
    if open_braces != close_braces:
        print("⚠️ 波括弧の不一致あり!")
    if open_brackets != close_brackets:
        print("⚠️ 角括弧の不一致あり!")
    
    # function 定義のリスト
    funcs = re.findall(r'(?:async\s+)?function\s+(\w+)', script)
    print(f"\n定義されている関数: {funcs}")
    
    # 重複チェック
    from collections import Counter
    func_counts = Counter(funcs)
    for name, count in func_counts.items():
        if count > 1:
            print(f"⚠️ 関数 '{name}' が {count} 回定義されています!")
    
    # renderActiveQuestion の出現を詳細チェック
    lines = script.split('\n')
    for j, line in enumerate(lines):
        if 'renderActiveQuestion' in line and 'function' in line:
            print(f"  renderActiveQuestion 定義: 行 {j+1}: {line.strip()}")
