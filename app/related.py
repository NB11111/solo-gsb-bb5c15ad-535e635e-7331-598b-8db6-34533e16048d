"""审计 ``text/html`` 叶子对 ``cid:`` 内嵌资源的引用。

规则（RFC 2392 / RFC 2111）：

* 仅扫描 ``text/html`` **叶子** 节点，从其已解码文本的 ``src``、
  ``href``、``poster``、``srcset`` 属性中提取 ``cid:`` URL；
  ``srcset`` 按逗号切分，每个候选 URL 单独成一条引用。
* ``cid:`` 之后的部分按 RFC 2392 做一次百分号解码（``%40`` → ``@``），
  再与同作用域内各节点 ``Content-ID`` 头去掉 ``<...>`` 包裹后的值
  精确（区分大小写）匹配。
* 作用域 = 引用源节点**最近的 ``multipart/related`` 祖先**：
  匹配只在该作用域内进行，既不向相邻的 sibling 作用域串联，也不向
  嵌套的子作用域（或反向往父作用域）串联。找不到 related 祖先的引用
  的 ``scope_node_path`` 为 ``null``，状态必为 ``missing``。
* 同一作用域内同一个 Content-ID 出现在多个不同节点上时，所有指向它
  的引用判为 ``ambiguous``，并给出全部候选路径；``missing`` 表示
  作用域内不存在该 Content-ID。
* 每个作用域列出携带 Content-ID 却从未被任何引用**唯一解析到**的
  ``orphan`` 节点（ambiguous 的候选节点也算未引用）；不属于任何
  related 作用域的 Content-ID 节点放在 ``node_path`` 为 ``null``
  的伪作用域中列出。

模块只依赖 ``database.list_nodes`` 返回的 ``sqlite3.Row``，
不触碰原始邮件字节：HTML 文本在归档时已完成 CTE/字符集解码、
NFC 与换行规范化。
"""

from __future__ import annotations

import json
from html.parser import HTMLParser
from typing import Any, Optional
from urllib.parse import unquote

# 需要提取 cid: 引用的属性（srcset 需按逗号切分多候选）。
_CID_ATTRIBUTES = ("src", "href", "poster", "srcset")

# 类型别名：sqlite3.Row 没有静态导出，用 Any 避免硬依赖。
sqlite3_Row = Any


# ---------------------------------------------------------------------------
# HTML 叶子中的 cid: 引用提取
# ---------------------------------------------------------------------------


class _CidExtractor(HTMLParser):
    """从 HTML 属性中收集 ``cid:`` 引用，按文档序输出。

    使用标准库 ``html.parser``：它对标签错配等“标签汤”天然宽容
    （归档审计不应因为 HTML 不规范而失败），且 ``convert_charrefs``
    会先完成 ``&#...;`` / ``&amp;`` 字符引用解码，与浏览器取值一致。
    注释、``<script>``/``<style>`` 文本不会被当作标签属性扫描。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        # 每项：{"attribute": "src"|"href"|"poster"|"srcset", "cid": str}
        self.references: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        for name, value in attrs:
            if value is None or name not in _CID_ATTRIBUTES:
                continue
            if name == "srcset":
                # srcset = "url descriptor, url descriptor, ..."；
                # 合法 srcset 的 URL 内不含裸逗号（需百分号编码），
                # 按逗号切分即符合规范。
                for item in value.split(","):
                    candidate = item.strip()
                    if not candidate:
                        continue
                    url = candidate.split()[0]
                    cid = cid_from_url(url)
                    if cid is not None:
                        self.references.append({"attribute": "srcset", "cid": cid})
                continue
            cid = cid_from_url(value)
            if cid is not None:
                self.references.append({"attribute": name, "cid": cid})

    # <img .../> 这类 XHTML 自写闭合标签也走同一逻辑（默认分派即可，
    # 显式覆盖以防子类化差异）。
    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, Optional[str]]]
    ) -> None:
        self.handle_starttag(tag, attrs)


def cid_from_url(value: str) -> Optional[str]:
    """若属性值是 ``cid:`` URL，返回 RFC 2392 百分号解码后的 CID。

    scheme 大小写不敏感，允许首尾空白；非 cid URL（http/https/data、
    相对路径等）返回 ``None``。``cid:`` 后为空时返回空串，仍计为一条
    引用（审计应当暴露这种损坏数据，解析结果自然是 missing）。
    """

    v = value.strip()
    if v[:4].lower() != "cid:":
        return None
    # RFC 2392 §2: cid URL 的 CID 部分允许百分号编码，解码一次。
    # 损坏的转义（如 %zz）由 unquote 原样保留，不抛异常。
    return unquote(v[4:].strip())


def extract_cid_references(html_text: str) -> list[dict[str, str]]:
    """扫描一段已解码 HTML，返回文档序的 cid 引用列表。"""

    parser = _CidExtractor()
    parser.feed(html_text or "")
    parser.close()
    return parser.references


# ---------------------------------------------------------------------------
# Content-ID 规范化（RFC 2045 / RFC 2392）
# ---------------------------------------------------------------------------


def normalize_content_id(raw: str) -> str:
    """把 ``Content-ID`` 头值规范化为可与 cid URL 比较的形式。

    归档存储的头值已完成 RFC 2047 解码与 NFC。这里去掉包裹 msg-id 的
    一对尖括号（``<foo@bar>`` → ``foo@bar``）与外围 CFWS 空白；
    没有尖括号的值（虽不符合 RFC，真实世界偶有）也按字面接受。
    """

    cid = raw.strip()
    if len(cid) >= 2 and cid.startswith("<") and cid.endswith(">"):
        cid = cid[1:-1].strip()
    return cid


def header_content_ids(headers: list[list[str]]) -> list[str]:
    """取出节点全部 ``Content-ID`` 头（允许重复，按头出现顺序）。"""

    result: list[str] = []
    for name, value in headers:
        if name.lower() == "content-id":
            result.append(normalize_content_id(value))
    return result


# ---------------------------------------------------------------------------
# 物化路径与 multipart/related 作用域
# ---------------------------------------------------------------------------


def _parent_path(path: str) -> Optional[str]:
    if not path:
        return None
    slash = path.rfind("/")
    return path[:slash] if slash >= 0 else ""


def _is_multipart_related(row: sqlite3_Row) -> bool:
    # content_type 列只存主媒体类型（参数单独处理），已小写；
    # 比较时仍统一小写以保持防御性。
    return row["content_type"].strip().lower() == "multipart/related"


def nearest_related_ancestor(
    path: str, by_path: dict[str, sqlite3_Row]
) -> Optional[str]:
    """返回最近的 ``multipart/related`` 祖先节点路径；根为 ``""``。

    逐层上溯物化路径——这天然穿过 ``multipart/alternative`` 等其他
    容器以及 ``message/rfc822`` 封装层，只在 *最近的* related 处停止，
    因此嵌套 related 的内/外层不会互相串联。
    """

    cur = _parent_path(path)
    while cur is not None:
        row = by_path.get(cur)
        if row is not None and _is_multipart_related(row):
            return cur
        cur = _parent_path(cur)
    return None


# ---------------------------------------------------------------------------
# 报告构建
# ---------------------------------------------------------------------------


def build_related_resources(rows: list[sqlite3_Row]) -> dict[str, Any]:
    """根据一封邮件的全部节点行构建 cid 引用审计报告。

    ``rows`` 须按文档序（``database.list_nodes`` 的 ``ORDER BY id``）。
    返回 ``{"references": [...], "scopes": [...]}``。
    """

    by_path = {row["path"]: row for row in rows}

    # (作用域路径或 None, cid) -> 携带该 CID 的去重后节点路径列表。
    # 以最近 related 祖先为键，从结构上保证不跨作用域匹配。
    cid_index: dict[tuple[Optional[str], str], list[str]] = {}
    # 各作用域下携带 Content-ID 的节点（文档序、按节点去重）。
    scoped_cid_nodes: dict[Optional[str], list[str]] = {}

    for row in rows:
        cids = header_content_ids(json.loads(row["headers_json"]))
        if not cids:
            continue
        scope = nearest_related_ancestor(row["path"], by_path)
        bucket = scoped_cid_nodes.setdefault(scope, [])
        if row["path"] not in bucket:
            bucket.append(row["path"])
        for cid in cids:
            candidates = cid_index.setdefault((scope, cid), [])
            # 同一节点重复同一个 CID（如重复 Content-ID 头）不构成歧义；
            # 歧义只由“同域多个不同节点”产生。
            if row["path"] not in candidates:
                candidates.append(row["path"])

    references: list[dict[str, Any]] = []
    # 仅记录被“唯一解析到”的节点；ambiguous 的候选不算被引用。
    referenced_nodes: dict[Optional[str], set[str]] = {}

    for row in rows:
        if row["content_type"].strip().lower() != "text/html":
            continue
        scope = nearest_related_ancestor(row["path"], by_path)
        html_text = row["text_content"] or ""
        for item in extract_cid_references(html_text):
            cid = item["cid"]
            candidates = cid_index.get((scope, cid), []) if scope is not None else []
            if len(candidates) == 1:
                status = "resolved"
                target: Optional[str] = candidates[0]
                referenced_nodes.setdefault(scope, set()).add(target)
            elif len(candidates) > 1:
                status = "ambiguous"
                target = None
            else:
                status = "missing"
                target = None
            references.append(
                {
                    "source_node_path": row["path"],
                    "attribute": item["attribute"],
                    "cid": cid,
                    "scope_node_path": scope,
                    "target_node_path": target,
                    "status": status,
                    "candidate_node_paths": candidates if status == "ambiguous" else [],
                }
            )

    # 各作用域的 orphan：有 Content-ID 却未被唯一解析到的节点。
    # related 作用域按节点文档序输出；无 related 祖先的节点归入
    # node_path=None 的伪作用域（若存在才输出）。
    scopes: list[dict[str, Any]] = []
    for row in rows:
        if not _is_multipart_related(row):
            continue
        cid_nodes = scoped_cid_nodes.get(row["path"], [])
        used = referenced_nodes.get(row["path"], set())
        scopes.append(
            {
                "node_path": row["path"],
                "orphan_node_paths": [p for p in cid_nodes if p not in used],
            }
        )
    if None in scoped_cid_nodes:
        used = referenced_nodes.get(None, set())
        scopes.append(
            {
                "node_path": None,
                "orphan_node_paths": [
                    p for p in scoped_cid_nodes[None] if p not in used
                ],
            }
        )

    return {"references": references, "scopes": scopes}
