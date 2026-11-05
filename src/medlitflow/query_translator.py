"""medlitflow query_translator.py — PubMed 检索式跨数据源转换。

支持的 PubMed 语法子集：
- AND / OR / NOT
- [ti] / [ab] / [tiab] / [mh] / [pt] / [dp] / [au]（含对应长别名）

暂不支持：[majr]、副主题词组合、截词符、邻近运算符（按自由词降级）。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Union

from medlitflow import core

__all__ = [
    "Atom",
    "AndNode",
    "OrNode",
    "NotNode",
    "ASTNode",
    "DnfClauseLimitError",
    "translate_pubmed_query",
    "parse_pubmed_query",
    "render_for_europepmc",
    "render_for_crossref",
    "render_for_openalex",
    "detect_unsupported_syntax",
]

@dataclass
class Atom:
    term: str
    field: str = ""          # "" / "ti" / "ab" / "tiab" / "mh" / "pt" / "dp" / "au"
    quoted: bool = False     # 原始是否带引号

@dataclass
class AndNode:
    children: list["ASTNode"] = field(default_factory=list)

@dataclass
class OrNode:
    children: list["ASTNode"] = field(default_factory=list)

@dataclass
class NotNode:
    left: "ASTNode" = None
    right: "ASTNode" = None


ASTNode = Union[Atom, AndNode, OrNode, NotNode]

_CN_LPAREN = "\uff08"   # （
_CN_RPAREN = "\uff09"   # ）

def _normalize_brackets(query: str) -> str:
    if not query:
        return query
    return query.replace(_CN_LPAREN, "(").replace(_CN_RPAREN, ")")

_FIELD_ALIASES = {
    "ti": "ti",
    "title": "ti",
    "ab": "ab",
    "abstract": "ab",
    "tiab": "tiab",
    "title/abstract": "tiab",
    "mh": "mh",
    "mesh terms": "mh",
    "pt": "pt",
    "publication type": "pt",
    "dp": "dp",
    "date - publication": "dp",
    "au": "au",
    "author": "au",
}

_KEYWORDS = {"AND", "OR", "NOT"}

# OpenAlex freeword 检索范围（可调）：
#   title_abstract_keywords → 标题+摘要+关键词（默认，近似网页搜索）
#   title_and_abstract      → 标题+摘要（中，排除关键词噪声）
#   fulltext                → 全文（精度优先，召回最低）
# 非法值 → 静默降级为 search 参数（相关性搜索，非精确匹配）
_OPENALEX_FREEWORD_MODE = "title_abstract_keywords"

@dataclass
class _Token:
    kind: str               # LPAREN / RPAREN / AND / OR / NOT / ATOM
    value: str = ""
    field: str = ""
    quoted: bool = False

def _normalize_field(raw: str) -> str | None:
    return _FIELD_ALIASES.get(raw.strip().lower())

def _tokenize(query: str) -> list[_Token]:
    tokens: list[_Token] = []
    i = 0
    n = len(query)

    while i < n:
        c = query[i]
        if c.isspace():
            i += 1
            continue

        if c == "(":
            tokens.append(_Token("LPAREN"))
            i += 1
            continue
        if c == ")":
            tokens.append(_Token("RPAREN"))
            i += 1
            continue

        if c == '"':
            j = i + 1
            while j < n and query[j] != '"':
                j += 1
            term = query[i + 1:j]
            i = j + 1
            field_name, i = _consume_field_tag(query, i)
            tokens.append(_Token(
                "ATOM", value=term, field=field_name, quoted=True,
            ))
            continue

        j = i
        while j < n and not query[j].isspace() and query[j] not in "()\"":
            j += 1
        word = query[i:j]

        if word.upper() in _KEYWORDS:
            tokens.append(_Token(word.upper()))
            i = j
            continue

        if word.endswith("]") and "[" in word:
            bracket = word.rfind("[")
            bare_term = word[:bracket]
            raw_field = word[bracket + 1:-1]
            normalized = _normalize_field(raw_field)
            tokens.append(_Token(
                "ATOM",
                value=bare_term,
                field=normalized if normalized else "",
                quoted=False,
            ))
            i = j
            continue

        phrase_parts: list[str] = [word]
        phrase_field = ""
        i = j

        while i < n:
            k = i
            while k < n and query[k].isspace():
                k += 1
            if k >= n:
                i = k
                break

            cc = query[k]

            if cc in "()\"":
                i = k
                break

            m = k
            while m < n and not query[m].isspace() and query[m] not in "()\"":
                m += 1
            next_word = query[k:m]

            if next_word.upper() in _KEYWORDS:
                i = k
                break

            if next_word.endswith("]") and "[" in next_word:
                bracket = next_word.rfind("[")
                bare = next_word[:bracket]
                raw = next_word[bracket + 1:-1]
                normalized = _normalize_field(raw)
                phrase_parts.append(bare)
                if normalized:
                    phrase_field = normalized
                i = m
                break

            phrase_parts.append(next_word)
            i = m

        phrase = " ".join(phrase_parts)
        tokens.append(_Token(
            "ATOM", value=phrase, field=phrase_field, quoted=False,
        ))

    return tokens

def detect_unsupported_syntax(query: str) -> list[dict]:
    if not query:
        return []
    
    query = _normalize_brackets(query)

    results: list[dict] = []
    i = 0
    n = len(query)

    while i < n:
        c = query[i]
        if c.isspace() or c in "()":
            i += 1
            continue

        if c == '"':
            j = i + 1
            while j < n and query[j] != '"':
                j += 1
            term = query[i + 1:j]
            k = j + 1
            while k < n and query[k].isspace():
                k += 1
            if k < n and query[k] == "[":
                m = k + 1
                while m < n and query[m] != "]":
                    m += 1
                if m < n:
                    raw_field = query[k + 1:m]
                    if _normalize_field(raw_field) is None:
                        results.append({
                            "field": raw_field,
                            "term": term,
                            "position": k,
                            "downgrade_to": term,
                        })
                    i = m + 1
                    continue
            i = j + 1
            continue

        j = i
        while j < n and not query[j].isspace() and query[j] not in "()":
            j += 1
        word = query[i:j]
        i = j

        if word.upper() in _KEYWORDS:
            continue

        if word.endswith("]") and "[" in word:
            bracket = word.rfind("[")
            bare_term = word[:bracket]
            raw_field = word[bracket + 1:-1]
            if _normalize_field(raw_field) is None:
                results.append({
                    "field": raw_field,
                    "term": bare_term,
                    "position": i - len(word) + bracket,
                    "downgrade_to": bare_term,
                })

    return results

def _consume_field_tag(query: str, i: int) -> tuple[str, int]:
    n = len(query)
    j = i
    while j < n and query[j].isspace():
        j += 1
    if j >= n or query[j] != "[":
        return "", i
    k = j + 1
    while k < n and query[k] != "]":
        k += 1
    if k >= n:
        return "", i
    raw_field = query[j + 1:k]
    normalized = _normalize_field(raw_field)
    if normalized is None:
        return "", i
    return normalized, k + 1

class _ParseError(ValueError):
    pass

_BOOL_OP_RE = re.compile(r"\b(?:AND|OR|NOT)\b", re.IGNORECASE)

def _auto_complete_brackets(query: str) -> str:
    if not query:
        return query

    stack: list[int] = []
    for i, ch in enumerate(query):
        if ch == "(":
            stack.append(i)
        elif ch == ")":
            if stack:
                stack.pop()

    if not stack:
        return query

    last_pos = stack[-1]
    rest = query[last_pos + 1:]
    if _BOOL_OP_RE.search(rest):
        raise _ParseError(
            "检索式存在未闭合的左括号 (，且最后一段含有逻辑运算符"
            "（AND / OR / NOT），系统无法自动确定右括号位置。"
            "请在合适位置手动补上 ) 后重试。"
        )

    return query + ")" * len(stack)

class _Parser:
    def __init__(self, tokens: list[_Token]):
        self.tokens = tokens
        self.pos = 0

    def parse(self) -> ASTNode:
        if not self.tokens:
            raise _ParseError("查询字符串为空")
        node = self._parse_or()
        if self.pos != len(self.tokens):
            next_token = self.tokens[self.pos]
            prev_token = (
                self.tokens[self.pos - 1] if self.pos > 0 else None
            )
            raise _ParseError(
                self._friendly_error(prev_token, next_token)
            )
        return node

    @staticmethod
    def _friendly_error(prev_token, next_token) -> str:
        if next_token is None:
            return "检索式解析失败（内部错误）"

        if prev_token is None:
            return (
                f"检索式格式错误：在 {next_token.value!r} 之前出现意外内容。"
                f"请检查检索式是否完整。"
            )

        if (prev_token.kind == "ATOM"
                and prev_token.quoted
                and next_token.kind == "ATOM"):
            return (
                f"引号短语 {prev_token.value!r} 后面缺少逻辑运算符"
                f"（AND / OR / NOT），不能直接跟词 {next_token.value!r}。"
                f"建议改为：{prev_token.value!r} AND {next_token.value!r}，"
                f"或用括号明确分组。"
            )

        if (prev_token.kind == "ATOM"
                and prev_token.field
                and not prev_token.quoted
                and next_token.kind == "ATOM"):
            return (
                f"字段标签 [{prev_token.field}] 后缺少逻辑运算符"
                f"（AND / OR / NOT），不能直接跟词 {next_token.value!r}。"
                f"请在 {prev_token.value!r}[{prev_token.field}] 与 "
                f"{next_token.value!r} 之间添加 AND / OR / NOT。"
            )

        if next_token.kind == "ATOM":
            return (
                f"检索式语法错误：词项 {next_token.value!r} 之前"
                f"缺少逻辑运算符（AND / OR / NOT）。"
                f"相邻的词项之间必须有 AND / OR / NOT。"
            )

        return f"未消费的 token：{next_token}"

    def _peek(self) -> _Token | None:
        if self.pos >= len(self.tokens):
            return None
        return self.tokens[self.pos]

    def _consume(self) -> _Token:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def _parse_or(self) -> ASTNode:
        left = self._parse_and()
        children = [left]
        while (self._peek() or _Token("")).kind == "OR":
            self._consume()
            children.append(self._parse_and())
        return OrNode(children) if len(children) > 1 else left

    def _parse_and(self) -> ASTNode:
        left = self._parse_not()
        children = [left]
        while (self._peek() or _Token("")).kind == "AND":
            self._consume()
            children.append(self._parse_not())
        return AndNode(children) if len(children) > 1 else left

    def _parse_not(self) -> ASTNode:
        left = self._parse_primary()
        if (self._peek() or _Token("")).kind == "NOT":
            self._consume()
            right = self._parse_primary()
            return NotNode(left=left, right=right)
        return left

    def _parse_primary(self) -> ASTNode:
        tok = self._peek()
        if tok is None:
            raise _ParseError("表达式意外结束")

        if tok.kind == "LPAREN":
            self._consume()
            node = self._parse_or()
            if (self._peek() or _Token("")).kind != "RPAREN":
                raise _ParseError("缺少右括号")
            self._consume()
            return node

        if tok.kind == "ATOM":
            self._consume()
            return Atom(term=tok.value, field=tok.field, quoted=tok.quoted)

        raise _ParseError(f"意外的 token：{tok.kind}")


def parse_pubmed_query(query: str) -> ASTNode:
    query = _normalize_brackets(query)
    query = _auto_complete_brackets(query)
    tokens = _tokenize(query)
    return _Parser(tokens).parse()

def _render_europepmc_atom(atom: Atom) -> str:
    term = atom.term
    if atom.field == "ti":
        return f'TITLE:"{term}"'
    if atom.field == "ab":
        return f'ABS:"{term}"'
    if atom.field == "tiab":
        return f'(TITLE:"{term}" OR ABS:"{term}")'
    if atom.field == "mh":
        return f'MESH:"{term}"'
    if atom.field == "pt":
        pt_value = term.strip().strip('"').lower()
        if " " in pt_value:
            return f'PUB_TYPE:"{pt_value}"'
        return f"PUB_TYPE:{pt_value}"
    if atom.field == "dp":
        return _render_europepmc_date(term)
    if atom.field == "au":
        return f'AUTHOR:"{term}"'
    if " " in term:
        return f'"{term}"'
    return term

def _render_europepmc_date(term: str) -> str:
    if ":" in term:
        start, end = term.split(":", 1)
        start = start.strip().replace("/", "-")
        end = end.strip().replace("/", "-")
        return f"FIRST_PDATE:[{start} TO {end}]"
    return f"FIRST_PDATE:{term.strip().replace('/', '-')}"

def _render_europepmc_node(node: ASTNode) -> str:
    if isinstance(node, Atom):
        return _render_europepmc_atom(node)
    if isinstance(node, AndNode):
        parts = [_render_europepmc_node(c) for c in node.children]
        return "(" + " AND ".join(parts) + ")"
    if isinstance(node, OrNode):
        parts = [_render_europepmc_node(c) for c in node.children]
        return "(" + " OR ".join(parts) + ")"
    if isinstance(node, NotNode):
        return (f"({_render_europepmc_node(node.left)}"
                f" NOT {_render_europepmc_node(node.right)})")
    return ""

def render_for_europepmc(ast: ASTNode) -> str:
    return _render_europepmc_node(ast)

MAX_DNF_CLAUSES = 64

class DnfClauseLimitError(ValueError):
    """DNF 展开超过 MAX_DNF_CLAUSES 个子句。"""

def _to_dnf(node: ASTNode) -> list[list[Atom]]:
    if isinstance(node, Atom):
        return [[node]]

    if isinstance(node, AndNode):
        result: list[list[Atom]] = [[]]
        for child in node.children:
            child_clauses = _to_dnf(child)
            new_result: list[list[Atom]] = []
            for left in result:
                for right in child_clauses:
                    new_result.append(left + right)
                    if len(new_result) > MAX_DNF_CLAUSES:
                        raise DnfClauseLimitError(
                            f"检索式 DNF 展开超过 {MAX_DNF_CLAUSES} 个子句，"
                            f"将导致 Crossref/OpenAlex 请求数过多。"
                            f"请精简 OR 子句数量，或用括号明确限定层级。"
                        )
            result = new_result
        return result

    if isinstance(node, OrNode):
        result: list[list[Atom]] = []
        for child in node.children:
            result.extend(_to_dnf(child))
            # P3-B：累计超过 MAX_DNF_CLAUSES 立即抛错
            if len(result) > MAX_DNF_CLAUSES:
                raise DnfClauseLimitError(
                    f"检索式 DNF 展开超过 {MAX_DNF_CLAUSES} 个子句，"
                    f"将导致 Crossref/OpenAlex 请求数过多。"
                    f"请精简 OR 子句数量，或用括号明确限定层级。"
                )
        return result

    if isinstance(node, NotNode):
        return _to_dnf(node.left)

    return [[]]

# PubMed Publication Type → Crossref / OpenAlex type
_PT_TO_CROSSREF_TYPE = {
    "journal article": "journal-article",
    "randomized controlled trial": "journal-article",
    "clinical trial": "journal-article",
    "comparative study": "journal-article",
    "book chapter": "book-chapter",
    "preprint": "posted-content",
}

def _map_pt_to_crossref_type(pt_term: str) -> str | None:
    key = pt_term.strip().strip('"').lower()
    return _PT_TO_CROSSREF_TYPE.get(key)

# OpenAlex 有效 type 白名单（来自 OpenAlex works API 文档）：
#   article / book / book-chapter / dataset / dissertation /
#   editorial / letter / paratext / peer-review / preprint / review / report
# 注意：OpenAlex **没有** journal-article / posted-content 等 Crossref 专有值。
# 直接复用 Crossref 映射（如 Randomized Controlled Trial → journal-article）
# 会触发 OpenAlex 返回 0 条结果（无效 filter 静默忽略）。
_PT_TO_OPENALEX_TYPE = {
    # 研究类 → article
    "journal article": "article",
    "randomized controlled trial": "article",
    "clinical trial": "article",
    "comparative study": "article",
    "research article": "article",
    # 综述类 → review
    "review": "review",
    "systematic review": "review",
    "meta-analysis": "review",
    # 其他出版类型
    "editorial": "editorial",
    "comment": "editorial",
    "letter": "letter",
    "book": "book",
    "book chapter": "book-chapter",
    "dataset": "dataset",
    "dissertation": "dissertation",
    "report": "report",
    "preprint": "preprint",
}

def _map_pt_to_openalex_type(pt_term: str) -> str | None:
    key = pt_term.strip().strip('"').lower()
    return _PT_TO_OPENALEX_TYPE.get(key)

def _parse_date_range(term: str) -> tuple[str, str] | None:
    if ":" not in term:
        year = term.strip().replace("/", "-")
        if year.isdigit() and len(year) == 4:
            return (f"{year}-01-01", f"{year}-12-31")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", year):
            return (year, year)
        return None

    start, end = (part.strip() for part in term.split(":", 1))

    def _normalize(part: str, is_start: bool) -> str | None:
        part = part.replace("/", "-")
        if part.isdigit() and len(part) == 4:
            return f"{part}-01-01" if is_start else f"{part}-12-31"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", part):
            return part
        return None

    start_iso = _normalize(start, is_start=True)
    end_iso = _normalize(end, is_start=False)
    if start_iso is None or end_iso is None:
        return None
    return (start_iso, end_iso)

def _build_crossref_query(clause: list[Atom]) -> dict:
    bibliographic: list[str] = []
    author = ""
    filters: list[str] = []
    date_range: tuple[str, str] | None = None
    pt_type: str | None = None

    for atom in clause:
        if atom.field == "pt":
            mapped = _map_pt_to_crossref_type(atom.term)
            if mapped and pt_type is None:
                pt_type = mapped
        elif atom.field == "dp":
            date_range = _parse_date_range(atom.term)
        elif atom.field == "au":
            author = atom.term
        else:
            bibliographic.append(atom.term)

    if pt_type:
        filters.append(f"type:{pt_type}")
    if date_range:
        filters.append(f"from-pub-date:{date_range[0]}")
        filters.append(f"until-pub-date:{date_range[1]}")

    result: dict = {}
    if bibliographic:
        result["query.bibliographic"] = " ".join(bibliographic)
    if author:
        result["query.author"] = author
    if filters:
        result["filter"] = ",".join(filters)
    return result

def render_for_crossref(ast: ASTNode) -> list[dict]:
    clauses = _to_dnf(ast)
    queries: list[dict] = []
    for clause in clauses:
        q = _build_crossref_query(clause)
        if q:
            queries.append(q)
    return queries

def _openalex_freeword_filter_prefix() -> str:
    """返回 freeword 走 filter 时的字段前缀；空串 = 走 search 参数。"""
    if _OPENALEX_FREEWORD_MODE == "title_abstract_keywords":
        return "title_abstract_keywords.search"
    if _OPENALEX_FREEWORD_MODE == "title_and_abstract":
        return "title_and_abstract.search"
    if _OPENALEX_FREEWORD_MODE == "fulltext":
        return "fulltext.search"
    return ""

def _has_structured_atom(node: ASTNode) -> bool:
    if isinstance(node, Atom):
        return node.field in ("au", "pt", "dp")
    if isinstance(node, (AndNode, OrNode)):
        return any(_has_structured_atom(c) for c in node.children)
    if isinstance(node, NotNode):
        return (_has_structured_atom(node.left)
                or _has_structured_atom(node.right))
    return False

def _or_contains_structured(node: ASTNode) -> bool:
    if isinstance(node, OrNode):
        for child in node.children:
            if _has_structured_atom(child):
                return True
            if _or_contains_structured(child):
                return True
        return False
    if isinstance(node, AndNode):
        return any(_or_contains_structured(c) for c in node.children)
    if isinstance(node, NotNode):
        return (_or_contains_structured(node.left)
                or _or_contains_structured(node.right))
    return False

def _format_openalex_freeword_term(term: str) -> str:
    if " " in term and not (term.startswith('"') and term.endswith('"')):
        return f'"{term}"'
    return term

def _render_openalex_freeword(node: ASTNode) -> str:
    if isinstance(node, Atom):
        if node.field in ("au", "pt", "dp"):
            return ""
        return _format_openalex_freeword_term(node.term)

    if isinstance(node, AndNode):
        parts = [_render_openalex_freeword(c) for c in node.children]
        parts = [p for p in parts if p]
        if len(parts) > 1:
            return "(" + " AND ".join(parts) + ")"
        return parts[0] if parts else ""

    if isinstance(node, OrNode):
        parts = [_render_openalex_freeword(c) for c in node.children]
        parts = [p for p in parts if p]
        if len(parts) > 1:
            return "(" + " OR ".join(parts) + ")"
        return parts[0] if parts else ""

    if isinstance(node, NotNode):
        left = _render_openalex_freeword(node.left)
        right = _render_openalex_freeword(node.right)
        if left and right:
            return f"({left} NOT {right})"
        return left or right

    return ""

def _collect_openalex_structured_filters(
    node: ASTNode, filters: list[str],
) -> None:
    if isinstance(node, Atom):
        if node.field == "au":
            filters.append(
                f"authorships.author.display_name.search:{node.term}"
            )
        elif node.field == "pt":
            mapped = _map_pt_to_openalex_type(node.term)
            if mapped:
                filters.append(f"type:{mapped}")
        elif node.field == "dp":
            date_range = _parse_date_range(node.term)
            if date_range:
                filters.append(f"from_publication_date:{date_range[0]}")
                filters.append(f"to_publication_date:{date_range[1]}")
        return
    if isinstance(node, (AndNode, OrNode)):
        for child in node.children:
            _collect_openalex_structured_filters(child, filters)
        return
    if isinstance(node, NotNode):
        _collect_openalex_structured_filters(node.left, filters)
        _collect_openalex_structured_filters(node.right, filters)

def _render_openalex_single(ast: ASTNode) -> dict:
    freeword_bool = _render_openalex_freeword(ast)
    structured_filters: list[str] = []
    _collect_openalex_structured_filters(ast, structured_filters)

    prefix = _openalex_freeword_filter_prefix()

    result: dict = {}
    filter_parts: list[str] = []

    if freeword_bool:
        if prefix:
            filter_parts.append(f"{prefix}:({freeword_bool})")
        else:
            result["search"] = freeword_bool

    filter_parts.extend(structured_filters)
    if filter_parts:
        result["filter"] = ",".join(filter_parts)

    return result

def _render_openalex_clause(clause: list[Atom]) -> dict:
    freewords: list[str] = []
    structured_filters: list[str] = []

    for atom in clause:
        if atom.field == "au":
            structured_filters.append(
                f"authorships.author.display_name.search:{atom.term}"
            )
        elif atom.field == "pt":
            mapped = _map_pt_to_openalex_type(atom.term)
            if mapped:
                structured_filters.append(f"type:{mapped}")
        elif atom.field == "dp":
            date_range = _parse_date_range(atom.term)
            if date_range:
                structured_filters.append(
                    f"from_publication_date:{date_range[0]}"
                )
                structured_filters.append(
                    f"to_publication_date:{date_range[1]}"
                )
        else:
            freewords.append(_format_openalex_freeword_term(atom.term))

    prefix = _openalex_freeword_filter_prefix()
    result: dict = {}
    filter_parts: list[str] = []

    if freewords:
        freeword_bool = " AND ".join(freewords)
        if prefix:
            filter_parts.append(f"{prefix}:({freeword_bool})")
        else:
            result["search"] = freeword_bool

    filter_parts.extend(structured_filters)
    if filter_parts:
        result["filter"] = ",".join(filter_parts)

    return result

def render_for_openalex(ast: ASTNode) -> list[dict]:
    if _or_contains_structured(ast):
        clauses = _to_dnf(ast)
        rendered = [_render_openalex_clause(c) for c in clauses if c]
        return [r for r in rendered if r]

    result = _render_openalex_single(ast)
    if not result:
        return []
    return [result]

def _has_dp_atom(node: ASTNode) -> bool:
    if isinstance(node, Atom):
        return node.field == "dp"
    if isinstance(node, (AndNode, OrNode)):
        return any(_has_dp_atom(c) for c in node.children)
    if isinstance(node, NotNode):
        return _has_dp_atom(node.left) or _has_dp_atom(node.right)
    return False

def _append_incremental_date(
    ast: ASTNode,
    incremental_dates: dict[str, str],
    source: str,
) -> ASTNode:
    start = incremental_dates.get(source)
    if not start:
        return ast
    end = core.today_iso()
    start_pubmed = start.replace("-", "/")
    end_pubmed = end.replace("-", "/")
    date_atom = Atom(term=f"{start_pubmed}:{end_pubmed}", field="dp", quoted=False)
    return AndNode(children=[ast, date_atom])

def translate_pubmed_query(
    pubmed_query: str,
    incremental_dates: dict[str, str] | None = None,
) -> dict:
    pubmed_query = _normalize_brackets(pubmed_query)
    ast = parse_pubmed_query(pubmed_query)

    effective_incremental = incremental_dates
    if incremental_dates and _has_dp_atom(ast):
        import warnings
        warnings.warn(
            "检索式已含 [dp] 日期字段，增量日期已跳过"
            "（避免双日期冲突导致空结果）。"
            "若需增量，请移除检索式中的 [dp] 限定。",
            UserWarning,
            stacklevel=2,
        )
        effective_incremental = None

    if effective_incremental:
        ast_europepmc = _append_incremental_date(
            ast, effective_incremental, "europepmc")
        ast_crossref = _append_incremental_date(
            ast, effective_incremental, "crossref")
        ast_openalex = _append_incremental_date(
            ast, effective_incremental, "openalex")
    else:
        ast_europepmc = ast_crossref = ast_openalex = ast

    pubmed_with_date = pubmed_query
    if effective_incremental:
        start = effective_incremental.get("pubmed")
        if start:
            end = core.today_iso()
            start_pubmed = start.replace("-", "/")
            end_pubmed = end.replace("-", "/")
            pubmed_with_date = (
                f"{pubmed_query} AND {start_pubmed}:{end_pubmed}[dp]"
            )

    return {
        "pubmed": pubmed_with_date,
        "europepmc": render_for_europepmc(ast_europepmc),
        "crossref": render_for_crossref(ast_crossref),
        "openalex": render_for_openalex(ast_openalex),
    }