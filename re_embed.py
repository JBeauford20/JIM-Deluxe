"""Re-embeds JIM_Deluxe_Live.html into main.py _FRONTEND_HTML variable."""
import re
from pathlib import Path

html_path = Path(__file__).parent / "JIM_Deluxe_Live.html"
main_path = Path(__file__).parent / "backend" / "main.py"

html = html_path.read_text(encoding='utf-8')
main = main_path.read_text(encoding='utf-8')

# Find _FRONTEND_HTML = """...""" and replace the content
pattern = re.compile(r'(_FRONTEND_HTML\s*=\s*""").*?(""")', re.DOTALL)
match = pattern.search(main)
if not match:
    raise SystemExit("ERROR: Could not find _FRONTEND_HTML in main.py")

print(f"Found _FRONTEND_HTML at chars {match.start()}-{match.end()}")
print(f"Old HTML size: {len(match.group(0))//1024}KB")

new_main = main[:match.start()] + '_FRONTEND_HTML = """' + html + '"""' + main[match.end():]
main_path.write_text(new_main, encoding='utf-8')

# Verify
verify = main_path.read_text(encoding='utf-8')
print(f"New main.py size: {len(verify)//1024}KB")
print(f"Cart Configs in main.py: {'nl-configs' in verify}")
print(f"Picker in main.py:       {'picker-items' in verify}")
print(f"setActiveShelf in main.py: {'setActiveShelf' in verify}")
print(f"cart-configs endpoint refs: {'cart-configs' in verify}")
