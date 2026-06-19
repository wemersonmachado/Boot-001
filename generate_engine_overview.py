import pathlib, re, json

BASE = pathlib.Path(r"c:/Users/welli/OneDrive/Desktop/Trade/Claude code - Bot 01/trader_001")
OUT_PATH = pathlib.Path(r"C:/Users/welli/.gemini/antigravity/brain/2d87617d-f25e-48a9-8722-19e5c91f66a9/engine_overview.md")

CLASS_PAT = re.compile(r"class\s+(\w+)(Engine|Strategy)\b")

def scan():
    overview = []
    for py in BASE.rglob("*.py"):
        if "venv" in py.parts:
            continue
        try:
            content = py.read_text(encoding="utf-8")
        except Exception:
            continue
        matches = CLASS_PAT.findall(content)
        if matches:
            classes = [f"{name}{suffix}" for name, suffix in matches]
            overview.append((py.relative_to(BASE), classes))
    return overview

def write_md(data):
    lines = ["# Engine & Strategy Overview", "", "## List of engines/strategies detected:\n"]
    for rel_path, classes in data:
        lines.append(f"* **{rel_path}**")
        for cls in classes:
            lines.append(f"  - {cls}")
        lines.append("")
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"Engine overview written to {OUT_PATH}")

if __name__ == "__main__":
    data = scan()
    write_md(data)
