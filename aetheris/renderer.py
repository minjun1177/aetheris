import re
import unicodedata
from aetheris.config import S, tw


_LATEX_SYMBOLS: list[tuple[str, str]] = [
    (r'\Gamma', 'Γ'), (r'\Delta', 'Δ'), (r'\Theta', 'Θ'), (r'\Lambda', 'Λ'),
    (r'\Xi', 'Ξ'), (r'\Pi', 'Π'), (r'\Sigma', 'Σ'), (r'\Upsilon', 'Υ'),
    (r'\Phi', 'Φ'), (r'\Psi', 'Ψ'), (r'\Omega', 'Ω'),
    (r'\alpha', 'α'), (r'\beta', 'β'), (r'\gamma', 'γ'), (r'\delta', 'δ'),
    (r'\epsilon', 'ε'), (r'\varepsilon', 'ε'), (r'\zeta', 'ζ'), (r'\eta', 'η'),
    (r'\theta', 'θ'), (r'\vartheta', 'ϑ'), (r'\iota', 'ι'), (r'\kappa', 'κ'),
    (r'\lambda', 'λ'), (r'\mu', 'μ'), (r'\nu', 'ν'), (r'\xi', 'ξ'),
    (r'\pi', 'π'), (r'\varpi', 'ϖ'), (r'\rho', 'ρ'), (r'\varrho', 'ϱ'),
    (r'\sigma', 'σ'), (r'\varsigma', 'ς'), (r'\tau', 'τ'), (r'\upsilon', 'υ'),
    (r'\phi', 'φ'), (r'\varphi', 'φ'), (r'\chi', 'χ'), (r'\psi', 'ψ'),
    (r'\omega', 'ω'),
    (r'\cdot', '·'), (r'\times', '×'), (r'\div', '÷'), (r'\pm', '±'),
    (r'\mp', '∓'), (r'\leq', '≤'), (r'\geq', '≥'), (r'\neq', '≠'),
    (r'\approx', '≈'), (r'\equiv', '≡'), (r'\sim', '∼'), (r'\propto', '∝'),
    (r'\infty', '∞'), (r'\partial', '∂'), (r'\nabla', '∇'), (r'\forall', '∀'),
    (r'\exists', '∃'), (r'\in', '∈'), (r'\notin', '∉'), (r'\subset', '⊂'),
    (r'\supset', '⊃'), (r'\subseteq', '⊆'), (r'\supseteq', '⊇'),
    (r'\cup', '∪'), (r'\cap', '∩'), (r'\emptyset', '∅'), (r'\varnothing', '∅'),
    (r'\sum', 'Σ'), (r'\prod', 'Π'), (r'\int', '∫'), (r'\oint', '∮'),
    (r'\sqrt', '√'), (r'\ldots', '…'), (r'\cdots', '⋯'), (r'\vdots', '⋮'),
    (r'\ddots', '⋱'), (r'\to', '→'), (r'\rightarrow', '→'), (r'\leftarrow', '←'),
    (r'\Rightarrow', '⇒'), (r'\Leftarrow', '⇐'), (r'\Leftrightarrow', '⇔'),
    (r'\leftrightarrow', '↔'), (r'\uparrow', '↑'), (r'\downarrow', '↓'),
    (r'\langle', '⟨'), (r'\rangle', '⟩'),
    (r'\quad', '  '), (r'\qquad', '    '), (r'\,', ' '), (r'\;', ' '),
    (r'\!', ''), (r'\\ ', ' '),
    (r'\log', 'log'), (r'\ln', 'ln'), (r'\exp', 'exp'), (r'\sin', 'sin'),
    (r'\cos', 'cos'), (r'\tan', 'tan'), (r'\arcsin', 'arcsin'),
    (r'\arccos', 'arccos'), (r'\arctan', 'arctan'), (r'\lim', 'lim'),
    (r'\max', 'max'), (r'\min', 'min'), (r'\sup', 'sup'), (r'\inf', 'inf'),
    (r'\det', 'det'), (r'\dim', 'dim'), (r'\ker', 'ker'),
    (r'\mathbb{R}', 'ℝ'), (r'\mathbb{N}', 'ℕ'), (r'\mathbb{Z}', 'ℤ'),
    (r'\mathbb{Q}', 'ℚ'), (r'\mathbb{C}', 'ℂ'),
    (r'\left(', '('), (r'\right)', ')'), (r'\left[', '['), (r'\right]', ']'),
    (r'\left\\{', '{'), (r'\right\\}', '}'), (r'\left|', '|'), (r'\right|', '|'),
    (r'\text{', ''), (r'\mathrm{', ''), (r'\mathbf{', ''), (r'\mathit{', ''),
    (r'\boldsymbol{', ''), (r'\hat{', ''), (r'\bar{', ''), (r'\tilde{', ''),
    (r'\vec{', ''), (r'\dot{', ''), (r'\ddot{', ''), (r'\textbf{', ''), (r'\textit{', ''),
]


_SUP_MAP = str.maketrans('0123456789+-=()', '⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾')
_SUB_MAP = str.maketrans('0123456789+-=()', '₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎')


def _render_latex(expr: str) -> str:
    s = expr.strip()

    s = re.sub(r'\\frac\{([^}]*)\}\{([^}]*)\}', r'(\1/\2)', s)
    s = re.sub(r'\\sqrt\{([^}]*)\}', r'√(\1)', s)
    s = re.sub(r'\^\{([^}]*)\}', lambda m: m.group(1).translate(_SUP_MAP), s)
    s = re.sub(r'_\{([^}]*)\}', lambda m: m.group(1).translate(_SUB_MAP), s)
    s = re.sub(r'\^([0-9a-zA-Z])', lambda m: m.group(1).translate(_SUP_MAP), s)
    s = re.sub(r'_([0-9a-zA-Z])', lambda m: m.group(1).translate(_SUB_MAP), s)

    for latex_sym, unicode_sym in sorted(_LATEX_SYMBOLS, key=lambda x: -len(x[0])):
        s = s.replace(latex_sym, unicode_sym)

    s = re.sub(r'\\[a-zA-Z]+', '', s)
    s = re.sub(r'[{}]', '', s)

    return s.strip()


def _apply_inline_md(line: str) -> str:
    """인라인 마크다운(bold, italic, code, strikethrough, latex)을 ANSI 코드로 치환한다."""
    def replace_inline_latex(m: re.Match) -> str:
        rendered = _render_latex(m.group(1))
        return f"{S.PURPLE}{rendered}{S.R}"

    line = re.sub(r'(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)', replace_inline_latex, line)

    # Bold+italic: ***...***
    line = re.sub(r'\*\*\*(.+?)\*\*\*', f'{S.BOLD}{S.ITAL}\\1{S.R}', line)
    # Bold: **...** 또는 __...__
    line = re.sub(r'\*\*(.+?)\*\*', f'{S.BOLD}\\1{S.R}', line)
    line = re.sub(r'(?<![^\W_])__(.+?)__(?![^\W_])', f'{S.BOLD}\\1{S.R}', line)
    # Italic: *...* 또는 _..._  (이미 bold 처리된 이후)
    line = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', f'{S.ITAL}\\1{S.R}', line)
    line = re.sub(r'(?<![^\W_])_([^_]+?)_(?![^\W_])', f'{S.ITAL}\\1{S.R}', line)
    # Strikethrough: ~~...~~
    line = re.sub(r'~~(.+?)~~', f'{S.DIM}{S.MUTED}\\1{S.R}', line)
    # Inline code: `...`
    line = re.sub(r'`([^`]+)`', f'{S.ACCENT}\\1{S.R}', line)

    return line


def _wrap_box_line(text: str, max_w: int) -> list[str]:
    lines = []
    curr_line = ""
    curr_w = 0
    for char in text:
        cw = 2 if unicodedata.east_asian_width(char) in 'WF' else 1
        if curr_w + cw > max_w:
            lines.append(curr_line)
            curr_line = char
            curr_w = cw
        else:
            curr_line += char
            curr_w += cw
    if curr_line or not lines:
        lines.append(curr_line)
    return lines

def _format_box_lines(text: str, border_color: str, text_color: str = "") -> str:
    max_w = max(1, tw() - 6)
    wrapped = _wrap_box_line(text, max_w)
    out = []
    for wl in wrapped:
        w = sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in wl)
        pad = " " * (max_w - w)
        out.append(f"  {border_color}│{S.R} {text_color}{wl}{S.R}{pad} {border_color}│{S.R}")
    return "\n".join(out)

def _render_line(line: str, in_code: bool, in_latex_block: bool = False) -> tuple[str, bool, bool]:
    stripped = line.strip()

    if stripped.startswith("```"):
        in_code = not in_code
        if in_code:
            lang = stripped[3:].strip() or "code"
            w = max(1, tw() - len(lang) - 7)
            return f"  {S.MUTED}╭─ {lang} {'─' * w}╮{S.R}", in_code, in_latex_block
        else:
            w = max(1, tw() - 4)
            return f"  {S.MUTED}╰{'─' * w}╯{S.R}", in_code, in_latex_block

    if in_code:
        return _format_box_lines(line, S.MUTED), in_code, in_latex_block

    inline_display = re.match(r'^(\$\$|\\\[)(.+?)(\$\$|\\\])$', stripped)
    if inline_display and not in_latex_block:
        rendered_math = _render_latex(inline_display.group(2))
        w = max(1, tw() - 11)
        middle = _format_box_lines(rendered_math, S.PURPLE, S.PURPLE)
        return (
            f"  {S.PURPLE}╭─ math {'─' * w}╮{S.R}\n"
            f"{middle}\n"
            f"  {S.PURPLE}╰{'─' * (w + 7)}╯{S.R}"
        ), in_code, in_latex_block

    is_latex_open = re.match(r'^(\$\$|\\\[|\\begin\{[a-zA-Z*]+\})', stripped)
    is_latex_close = re.match(r'^(\$\$|\\\]|\\end\{[a-zA-Z*]+\})', stripped)

    if not in_latex_block and is_latex_open:
        in_latex_block = True
        w = max(1, tw() - 11)
        return f"  {S.PURPLE}╭─ math {'─' * w}╮{S.R}", in_code, in_latex_block

    if in_latex_block and is_latex_close:
        in_latex_block = False
        w = max(1, tw() - 4)
        return f"  {S.PURPLE}╰{'─' * w}╯{S.R}", in_code, in_latex_block


    if in_latex_block:
        if stripped.startswith(r'\item'):
            stripped = "* " + stripped[5:].strip()
        rendered_math = _render_latex(stripped)
        return _format_box_lines(rendered_math, S.PURPLE, S.PURPLE), in_code, in_latex_block

    if stripped.startswith("###### "):
        return f"  {S.DIM}{S.GRAY}{stripped[7:]}{S.R}", in_code, in_latex_block
    if stripped.startswith("##### "):
        return f"  {S.GRAY}{stripped[6:]}{S.R}", in_code, in_latex_block
    if stripped.startswith("#### "):
        return f"  {S.BOLD}{S.MUTED}{stripped[5:]}{S.R}", in_code, in_latex_block
    if stripped.startswith("### "):
        return f"  {S.BOLD}{S.INFO}{stripped[4:]}{S.R}", in_code, in_latex_block
    if stripped.startswith("## "):
        return f"  {S.BOLD}{S.ACCENT}{stripped[3:]}{S.R}", in_code, in_latex_block
    if stripped.startswith("# "):
        return f"  {S.BOLD}{S.WHITE}{stripped[2:]}{S.R}", in_code, in_latex_block

    if re.match(r'^(---+|\*\*\*+|___+)$', stripped):
        return f"  {S.MUTED}{'─' * max(1, tw() - 4)}{S.R}", in_code, in_latex_block

    if stripped.startswith("> "):
        inner = stripped[2:]
        inner = _apply_inline_md(inner)
        return f"  {S.MUTED}┃{S.R} {S.ITAL}{S.GRAY}{inner}{S.R}", in_code, in_latex_block

    line = _apply_inline_md(line)

    # 불릿·순서 목록
    line = re.sub(r'^(\s*)[-*]\s', f'\\1{S.ACCENT}•{S.R} ', line)
    line = re.sub(r'^(\s*)(\d+\.)\s', f'\\1{S.ACCENT}\\2{S.R} ', line)

    return f"  {line}", in_code, in_latex_block


def _clean_md(text: str) -> str:
    t = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    t = re.sub(r'(?<![^\W_])__(.+?)__(?![^\W_])', r'\1', t)
    t = re.sub(r'`([^`]+)`', r'\1', t)
    return t

def _disp_width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in s)

def _wrap_plain_text(text: str, max_w: int) -> list[str]:
    words = text.split(' ')
    lines = []
    curr_line = ""
    curr_w = 0
    for word in words:
        ww = _disp_width(word)
        space_w = 1 if curr_w > 0 else 0
        if curr_w + space_w + ww <= max_w:
            if curr_w > 0:
                curr_line += " "
                curr_w += 1
            curr_line += word
            curr_w += ww
        else:
            if curr_line: lines.append(curr_line)
            curr_line = ""
            curr_w = 0
            if ww > max_w:
                for char in word:
                    cw = 2 if unicodedata.east_asian_width(char) in 'WF' else 1
                    if curr_w + cw > max_w:
                        lines.append(curr_line)
                        curr_line = char
                        curr_w = cw
                    else:
                        curr_line += char
                        curr_w += cw
            else:
                curr_line = word
                curr_w = ww
    if curr_line: lines.append(curr_line)
    return lines if lines else [""]

def _format_table(lines: list[str]) -> list[str]:
    if not lines: return []
    parsed = []
    for line in lines:
        cells = line.strip().strip('|').split('|')
        parsed.append([c.strip() for c in cells])
        
    num_cols = max(len(row) for row in parsed)
    for row in parsed:
        while len(row) < num_cols: row.append("")
            
    col_widths = [0] * num_cols
    for row in parsed:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], _disp_width(_clean_md(cell)))
            
    available_w = max(10, tw() - 6 - (num_cols * 3))
    total_w = sum(col_widths)
    
    if total_w > available_w:
        new_widths = [0] * num_cols
        remaining = available_w
        for i in range(num_cols):
            new_widths[i] = min(col_widths[i], max(3, available_w // num_cols))
            remaining -= new_widths[i]
        needs_more = [i for i in range(num_cols) if col_widths[i] > new_widths[i]]
        while remaining > 0 and needs_more:
            for i in list(needs_more):
                if remaining <= 0: break
                new_widths[i] += 1
                remaining -= 1
                if new_widths[i] == col_widths[i]: needs_more.remove(i)
        col_widths = new_widths
            
    out = []
    for r_idx, row in enumerate(parsed):
        is_sep = all(re.match(r'^:?-+:?$', c) for c in row if c)
        if r_idx == 0:
            seps = ["─" * (w + 2) for w in col_widths]
            out.append(f"  {S.MUTED}╭{'┬'.join(seps)}╮{S.R}")
        if is_sep:
            seps = ["─" * (w + 2) for w in col_widths]
            out.append(f"  {S.MUTED}├{'┼'.join(seps)}┤{S.R}")
        else:
            wrapped_cells = []
            for i, c in enumerate(row):
                tgt_w = col_widths[i]
                c_clean = _clean_md(c)
                if _disp_width(c_clean) > tgt_w:
                    wrapped_cells.append(_wrap_plain_text(c_clean, tgt_w))
                else:
                    c_styled = re.sub(r'\*\*(.+?)\*\*', f'{S.BOLD}\\1{S.R}', c)
                    c_styled = re.sub(r'`([^`]+)`', f'{S.ACCENT}\\1{S.R}', c_styled)
                    wrapped_cells.append([c_styled])
                    
            max_lines = max((len(c) for c in wrapped_cells), default=1)
            for line_idx in range(max_lines):
                fmt_cells = []
                for i in range(num_cols):
                    tgt_w = col_widths[i]
                    cell_lines = wrapped_cells[i]
                    if line_idx < len(cell_lines):
                        line_text = cell_lines[line_idx]
                        line_clean = re.sub(r'\033\[[^m]*m', '', line_text)
                        w = _disp_width(line_clean)
                        pad = " " * max(0, tgt_w - w)
                        fmt_cells.append(f" {line_text}{pad} ")
                    else:
                        pad = " " * tgt_w
                        fmt_cells.append(f" {pad} ")
                border = f"{S.MUTED}│{S.R}"
                out.append(f"  {border}{border.join(fmt_cells)}{border}")
            
    if parsed:
        seps = ["─" * (w + 2) for w in col_widths]
        out.append(f"  {S.MUTED}╰{'┴'.join(seps)}╯{S.R}")
    return out

def _render_full(text: str) -> str:
    lines = text.split('\n')
    out = []
    in_c = False
    in_lb = False
    table_buf = []

    def flush_t():
        if table_buf:
            out.extend(_format_table(table_buf))
            table_buf.clear()

    for line in lines:
        stripped = line.strip()
        if not in_c and not in_lb and stripped.startswith('|') and stripped.endswith('|'):
            table_buf.append(line)
        else:
            flush_t()
            rendered, in_c, in_lb = _render_line(line, in_c, in_lb)
            out.append(rendered)
    flush_t()
    return '\n'.join(out)
