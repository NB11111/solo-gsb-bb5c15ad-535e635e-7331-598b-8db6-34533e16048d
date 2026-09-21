"""GET /messages/{id}/related-resources 端到端测试。

覆盖：cid: 提取（src/href/poster/srcset）、RFC 2392 百分号解码、
multipart/related 作用域（不跨相邻/嵌套作用域串联）、同域重复
Content-ID 判 ambiguous、各作用域 orphan 清单与 404。
"""

import base64

from conftest import (
    cid_part,
    html_part,
    make_multipart,
    related_message,
    text_part,
)

PNG_A = b"\x89PNG\r\n\x1a\nAAAA"
PNG_B = b"\x89PNG\r\n\x1a\nBBBB"
PNG_C = b"\x89PNG\r\n\x1a\nCCCC"


def post(client, data: bytes) -> int:
    r = client.post("/messages", content=data)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def refs(client, mid: int) -> list[dict]:
    r = client.get(f"/messages/{mid}/related-resources")
    assert r.status_code == 200, r.text
    return r.json()["references"]


def report(client, mid: int) -> dict:
    r = client.get(f"/messages/{mid}/related-resources")
    assert r.status_code == 200, r.text
    return r.json()


def ref_by_key(references: list[dict], source: str, attribute: str, cid: str) -> dict:
    hits = [
        x
        for x in references
        if x["source_node_path"] == source
        and x["attribute"] == attribute
        and x["cid"] == cid
    ]
    assert len(hits) == 1, hits
    return hits[0]


# ---------------------------------------------------------------------------
# 基本解析：resolved / missing / orphan
# ---------------------------------------------------------------------------


def test_message_not_found_404(client):
    r = client.get("/messages/999/related-resources")
    assert r.status_code == 404


def test_resolved_reference_and_orphan(client):
    html = (
        '<html><body><img src="cid:logo@x"></body></html>'
    )
    msg = related_message(
        [
            html_part(html),
            cid_part(PNG_A, "logo@x"),
            cid_part(PNG_B, "unused@x"),
        ]
    )
    mid = post(client, msg)
    body = report(client, mid)

    # 根 multipart/related 自身作用域，路径为 ""
    assert body["scopes"] == [{"node_path": "", "orphan_node_paths": ["3"]}]

    ref = body["references"][0]
    assert ref["source_node_path"] == "1"
    assert ref["attribute"] == "src"
    assert ref["cid"] == "logo@x"
    assert ref["scope_node_path"] == ""
    assert ref["target_node_path"] == "2"
    assert ref["status"] == "resolved"
    assert ref["candidate_node_paths"] == []


def test_missing_reference(client):
    msg = related_message(
        [
            html_part('<img src="cid:nope@x">'),
            cid_part(PNG_A, "logo@x"),
        ]
    )
    mid = post(client, msg)
    body = report(client, mid)
    ref = body["references"][0]
    assert ref["status"] == "missing"
    assert ref["target_node_path"] is None
    assert ref["candidate_node_paths"] == []
    # 唯一的 CID 节点从未被引用 → orphan
    assert body["scopes"] == [{"node_path": "", "orphan_node_paths": ["2"]}]


def test_href_poster_srcset_attributes(client):
    html = (
        '<a href="cid:doc@x">d</a>'
        '<video poster="cid:poster@x"></video>'
        '<img srcset="cid:a@x 1x, cid:b@x 2x">'
        '<img src="https://example.com/x.png">'
    )
    msg = related_message(
        [
            html_part(html),
            cid_part(b"d", "doc@x", ctype="application/pdf"),
            cid_part(b"p", "poster@x"),
            cid_part(b"a", "a@x"),
            cid_part(b"b", "b@x"),
        ]
    )
    mid = post(client, msg)
    references = refs(client, mid)
    # 只有 4 条 cid 引用；https 链接不计入
    assert len(references) == 4
    assert ref_by_key(references, "1", "href", "doc@x")["status"] == "resolved"
    assert ref_by_key(references, "1", "poster", "poster@x")["status"] == "resolved"
    r_a = ref_by_key(references, "1", "srcset", "a@x")
    r_b = ref_by_key(references, "1", "srcset", "b@x")
    assert r_a["target_node_path"] == "4"
    assert r_b["target_node_path"] == "5"
    assert {s["node_path"] for s in report(client, mid)["scopes"]} == {""}


# ---------------------------------------------------------------------------
# RFC 2392 百分号解码
# ---------------------------------------------------------------------------


def test_rfc2392_percent_decoding(client):
    # %40 -> '@'；大小写混合 scheme、首尾空白也应命中
    html = '<img src="  CID:logo%40example.com  ">'
    msg = related_message(
        [
            html_part(html),
            cid_part(PNG_A, "logo@example.com"),
        ]
    )
    mid = post(client, msg)
    references = refs(client, mid)
    assert len(references) == 1
    ref = references[0]
    assert ref["cid"] == "logo@example.com"
    assert ref["status"] == "resolved"
    assert ref["target_node_path"] == "2"


def test_percent_encoded_bytes_preserved(client):
    # %E9 不是合法 ASCII cid 字符，但 RFC 2392 允许百分号编码；
    # 解码后的字节按 UTF-8 解释（unquote 默认 UTF-8，surrogateescape 兜底）
    html = '<img src="cid:r%c3%a9sum%c3%a9@x">'
    msg = related_message(
        [
            html_part(html),
            cid_part(PNG_A, "résumé@x"),
        ]
    )
    mid = post(client, msg)
    ref = refs(client, mid)[0]
    assert ref["cid"] == "résumé@x"
    assert ref["status"] == "resolved"


def test_malformed_percent_escape_does_not_crash(client):
    # 损坏的百分号转义原样保留，自然匹配不上 → missing
    msg = related_message(
        [
            html_part('<img src="cid:bad%zz@x">'),
            cid_part(PNG_A, "bad%zz@x"),
        ]
    )
    mid = post(client, msg)
    ref = refs(client, mid)[0]
    assert ref["cid"] == "bad%zz@x"
    assert ref["status"] == "resolved"  # 与字面 Content-ID 仍可匹配


# ---------------------------------------------------------------------------
# 同域重复 Content-ID → ambiguous
# ---------------------------------------------------------------------------


def test_duplicate_content_id_same_scope_is_ambiguous(client):
    msg = related_message(
        [
            html_part('<img src="cid:dup@x">'),
            cid_part(PNG_A, "dup@x"),
            cid_part(PNG_B, "dup@x"),
            cid_part(PNG_C, "solo@x"),
        ]
    )
    mid = post(client, msg)
    body = report(client, mid)
    ref = body["references"][0]
    assert ref["status"] == "ambiguous"
    assert ref["target_node_path"] is None
    assert ref["candidate_node_paths"] == ["2", "3"]
    # 两个歧义候选都算未引用；solo 也没被引用
    assert body["scopes"] == [
        {"node_path": "", "orphan_node_paths": ["2", "3", "4"]}
    ]


def test_same_node_repeated_identical_content_id_not_ambiguous(client):
    # 同一节点重复同名 Content-ID（头允许重复）不构成歧义
    dup_header = b"Content-ID: <logo@x>\r\n"
    part = cid_part(PNG_A, "logo@x", extra_headers=dup_header)
    msg = related_message(
        [
            html_part('<img src="cid:logo@x">'),
            part,
        ]
    )
    mid = post(client, msg)
    ref = refs(client, mid)[0]
    assert ref["status"] == "resolved"
    assert ref["target_node_path"] == "2"
    assert report(client, mid)["scopes"] == [
        {"node_path": "", "orphan_node_paths": []}
    ]


def test_distinct_content_ids_on_same_node_are_both_resolvable(client):
    dup_header = b"Content-ID: <second@x>\r\n"
    part = cid_part(PNG_A, "first@x", extra_headers=dup_header)
    msg = related_message(
        [
            html_part(
                '<img src="cid:first@x"><img src="cid:second@x">'
            ),
            part,
        ]
    )
    mid = post(client, msg)
    references = refs(client, mid)
    assert ref_by_key(references, "1", "src", "first@x")["target_node_path"] == "2"
    assert ref_by_key(references, "1", "src", "second@x")["target_node_path"] == "2"
    assert all(r["status"] == "resolved" for r in references)
    assert report(client, mid)["scopes"] == [
        {"node_path": "", "orphan_node_paths": []}
    ]


# ---------------------------------------------------------------------------
# 作用域：相邻 sibling multipart/related 不串联
# ---------------------------------------------------------------------------


def test_sibling_related_scopes_do_not_leak(client):
    # 外层 mixed：两个并列的 multipart/related
    rel1 = (
        b"Content-Type: multipart/related; boundary=R1\r\n\r\n"
        + make_multipart(
            [
                html_part('<img src="cid:pic@x">'),
                cid_part(PNG_A, "pic@x"),
            ],
            "R1",
        )
    )
    rel2 = (
        b"Content-Type: multipart/related; boundary=R2\r\n\r\n"
        + make_multipart(
            [
                html_part('<img src="cid:pic@x">'),
                cid_part(PNG_B, "other@x"),
            ],
            "R2",
        )
    )
    outer = (
        b"Content-Type: multipart/mixed; boundary=OUT\r\n\r\n"
        + make_multipart([rel1, rel2], "OUT")
    )
    mid = post(client, outer)
    body = report(client, mid)

    references = body["references"]
    # 第一个 related：1/1 的 html 解析到 1/2；作用域 1
    r1 = [r for r in references if r["source_node_path"] == "1/1"][0]
    assert r1["scope_node_path"] == "1"
    assert r1["status"] == "resolved"
    assert r1["target_node_path"] == "1/2"
    # 第二个 related：2/1 的 html 在自己作用域内找不到 pic@x
    # （other@x 在 2/2，不允许跨相邻作用域借 1/2）
    r2 = [r for r in references if r["source_node_path"] == "2/1"][0]
    assert r2["scope_node_path"] == "2"
    assert r2["status"] == "missing"

    scopes = {s["node_path"]: s["orphan_node_paths"] for s in body["scopes"]}
    assert set(scopes) == {"1", "2"}
    assert scopes["1"] == []          # 1/2 被引用
    assert scopes["2"] == ["2/2"]     # other@x 从未被引用


# ---------------------------------------------------------------------------
# 作用域：嵌套 multipart/related 不向上/向下串联
# ---------------------------------------------------------------------------


def test_nested_related_scopes_do_not_cascade(client):
    # 外层 related（scope ""）：
    #   1: 外层 html，引用 outer@x
    #   2: 内层 related（scope "2"）：
    #        2/1: 内层 html，引用 inner@x 与 outer@x
    #        2/2: inner@x 资源
    #   3: outer@x 资源
    inner = (
        b"Content-Type: multipart/related; boundary=IN\r\n\r\n"
        + make_multipart(
            [
                html_part(
                    '<img src="cid:inner@x">'
                    '<img src="cid:outer@x">'
                ),
                cid_part(PNG_A, "inner@x"),
            ],
            "IN",
        )
    )
    msg = related_message(
        [
            html_part('<img src="cid:outer@x">'),
            inner,
            cid_part(PNG_C, "outer@x"),
        ]
    )
    mid = post(client, msg)
    body = report(client, mid)
    references = body["references"]

    outer_ref = ref_by_key(references, "1", "src", "outer@x")
    assert outer_ref["scope_node_path"] == ""
    assert outer_ref["status"] == "resolved"
    assert outer_ref["target_node_path"] == "3"

    inner_ok = ref_by_key(references, "2/1", "src", "inner@x")
    assert inner_ok["scope_node_path"] == "2"
    assert inner_ok["status"] == "resolved"
    assert inner_ok["target_node_path"] == "2/2"

    # 内层 html 引用 outer@x：外层 3 不在内层作用域 → missing，
    # 绝不向上串联到节点 3
    inner_outer = ref_by_key(references, "2/1", "src", "outer@x")
    assert inner_outer["scope_node_path"] == "2"
    assert inner_outer["status"] == "missing"
    assert inner_outer["target_node_path"] is None

    scopes = {s["node_path"]: s["orphan_node_paths"] for s in body["scopes"]}
    assert set(scopes) == {"", "2"}
    assert scopes["2"] == []  # 2/2 被内层引用
    assert scopes[""] == []   # 3 被外层引用


def test_related_scope_skips_other_multipart_subtypes(client):
    # multipart/alternative 不构成作用域：其内的 html 最近 related
    # 祖先是外层 related，cid 资源也作为外层兄弟时可正常解析。
    alt = (
        b"Content-Type: multipart/alternative; boundary=ALT\r\n\r\n"
        + make_multipart(
            [text_part("plain"), html_part('<img src="cid:pic@x">')],
            "ALT",
        )
    )
    msg = related_message(
        [
            alt,
            cid_part(PNG_A, "pic@x"),
        ]
    )
    mid = post(client, msg)
    body = report(client, mid)
    ref = body["references"][0]
    assert ref["source_node_path"] == "1/2"
    assert ref["scope_node_path"] == ""
    assert ref["status"] == "resolved"
    assert ref["target_node_path"] == "2"
    assert body["scopes"] == [{"node_path": "", "orphan_node_paths": []}]


def test_mixed_is_not_a_scope(client):
    # multipart/mixed 不是 related：html 与资源同处其中也不形成作用域
    msg = related_message(
        [
            html_part('<img src="cid:pic@x">'),
            cid_part(PNG_A, "pic@x"),
        ],
        subtype="mixed",
    )
    mid = post(client, msg)
    body = report(client, mid)
    assert body["references"][0]["status"] == "missing"
    assert body["references"][0]["scope_node_path"] is None
    # CID 节点不属于任何 related 作用域 → null 伪作用域 orphan
    assert body["scopes"] == [{"node_path": None, "orphan_node_paths": ["2"]}]


def test_html_inside_encapsulated_message_resolves_to_enclosing_related(client):
    # multipart/related > message/rfc822 > [html, cid]
    inner = (
        b"Content-Type: multipart/related; boundary=IN\r\n\r\n"
        + make_multipart(
            [
                html_part('<img src="cid:deep@x">'),
                cid_part(PNG_A, "deep@x"),
            ],
            "IN",
        )
    )
    envelope = b"Content-Type: message/rfc822\r\n\r\n" + inner
    msg = related_message([text_part("wrapper"), envelope])
    mid = post(client, msg)
    body = report(client, mid)
    ref = body["references"][0]
    # 最近 related 祖先是被封装报文内部的 "2/1"（message 节点是 2）
    assert ref["source_node_path"] == "2/1/1"
    assert ref["scope_node_path"] == "2/1"
    assert ref["target_node_path"] == "2/1/2"
    assert ref["status"] == "resolved"


# ---------------------------------------------------------------------------
# 其他边界
# ---------------------------------------------------------------------------


def test_non_html_text_leaves_are_not_scanned(client):
    # text/plain 里即使写着 cid: 也不提取
    msg = related_message(
        [
            text_part('<img src="cid:logo@x">'),
            cid_part(PNG_A, "logo@x"),
        ]
    )
    mid = post(client, msg)
    body = report(client, mid)
    assert body["references"] == []
    # 资源存在但从未被任何 HTML 引用 → orphan
    assert body["scopes"] == [{"node_path": "", "orphan_node_paths": ["2"]}]


def test_non_cid_urls_ignored(client):
    html = (
        '<img src="https://example.com/a.png">'
        '<a href="relative/page.html">x</a>'
        '<img src="data:image/png;base64,AAAA">'
        '<video poster="http://v/poster.jpg">'
    )
    msg = related_message(
        [
            html_part(html),
            cid_part(PNG_A, "logo@x"),
        ]
    )
    mid = post(client, msg)
    body = report(client, mid)
    assert body["references"] == []
    assert body["scopes"][0]["orphan_node_paths"] == ["2"]


def test_references_in_quoted_printable_html(client):
    # CTE 解码必须发生在 cid 审计之前：cid 被 QP 软换行切成两段。
    # 注意 QP 正文中属性自身的 '=' 必须编码为 =3D。
    html = '<html><img src=3D"cid:lo=\r\ngo@x"></html>'
    msg = related_message(
        [
            html_part(html, cte="quoted-printable"),
            cid_part(PNG_A, "logo@x"),
        ]
    )
    mid = post(client, msg)
    ref = refs(client, mid)[0]
    assert ref["cid"] == "logo@x"
    assert ref["status"] == "resolved"


def test_html_comments_and_scripts_are_not_tags(client):
    html = (
        '<!-- <img src="cid:comment@x"> -->'
        "<script>var s = '<img src=cid:script@x>';</script>"
        '<img src="cid:real@x">'
    )
    msg = related_message(
        [
            html_part(html),
            cid_part(PNG_A, "real@x"),
        ]
    )
    mid = post(client, msg)
    references = refs(client, mid)
    assert len(references) == 1
    assert references[0]["cid"] == "real@x"


def test_numeric_character_reference_in_attribute_decoded(client):
    # &#99; = 'c'，实体解码后才识别 cid: scheme
    html = '<img src="&#99;id:logo@x">'
    msg = related_message(
        [
            html_part(html),
            cid_part(PNG_A, "logo@x"),
        ]
    )
    mid = post(client, msg)
    references = refs(client, mid)
    assert len(references) == 1
    assert references[0]["status"] == "resolved"


def test_empty_cid_reference_is_missing(client):
    msg = related_message(
        [
            html_part('<img src="cid:">'),
            cid_part(PNG_A, "logo@x"),
        ]
    )
    mid = post(client, msg)
    body = report(client, mid)
    ref = body["references"][0]
    assert ref["cid"] == ""
    assert ref["status"] == "missing"
    assert body["scopes"][0]["orphan_node_paths"] == ["2"]


def test_content_id_without_angle_brackets_accepted(client):
    # 不规范但真实存在的写法：Content-ID: logo@x（无尖括号）
    part = (
        b"Content-Type: image/png\r\n"
        b"Content-ID: logo@x\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        + base64.b64encode(PNG_A)
    )
    msg = related_message(
        [
            html_part('<img src="cid:logo@x">'),
            part,
        ]
    )
    mid = post(client, msg)
    assert refs(client, mid)[0]["status"] == "resolved"


def test_content_id_matching_is_case_sensitive(client):
    msg = related_message(
        [
            html_part('<img src="cid:Logo@X">'),
            cid_part(PNG_A, "logo@x"),
        ]
    )
    mid = post(client, msg)
    assert refs(client, mid)[0]["status"] == "missing"


def test_scope_with_type_parameter_still_recognized(client):
    # multipart/related; type="text/html" 仍是 related 作用域
    msg = (
        b"Subject: t\r\n"
        b'Content-Type: multipart/related; boundary=REL;\r\n'
        b' type="text/html"\r\n\r\n'
        + make_multipart(
            [
                html_part('<img src="cid:logo@x">'),
                cid_part(PNG_A, "logo@x"),
            ],
            "REL",
        )
    )
    mid = post(client, msg)
    body = report(client, mid)
    assert body["references"][0]["status"] == "resolved"
    assert body["scopes"][0]["node_path"] == ""


def test_multiple_html_leaves_ordered_by_document_order(client):
    msg = related_message(
        [
            html_part('<img src="cid:one@x">'),
            html_part('<img src="cid:two@x">'),
            cid_part(PNG_A, "one@x"),
            cid_part(PNG_B, "two@x"),
        ]
    )
    mid = post(client, msg)
    references = refs(client, mid)
    assert [r["source_node_path"] for r in references] == ["1", "2"]
    assert [r["cid"] for r in references] == ["one@x", "two@x"]
    assert [r["target_node_path"] for r in references] == ["3", "4"]


def test_root_related_resource_outside_any_scope_is_null_orphan(client):
    # 外层 mixed 中放一个带 CID 的 related，related 外面再放一个 CID 资源；
    # 外层资源无 related 祖先 → null 伪作用域 orphan
    rel = (
        b"Content-Type: multipart/related; boundary=R1\r\n\r\n"
        + make_multipart(
            [
                html_part('<img src="cid:in@x">'),
                cid_part(PNG_A, "in@x"),
            ],
            "R1",
        )
    )
    outer = (
        b"Content-Type: multipart/mixed; boundary=OUT\r\n\r\n"
        + make_multipart([rel, cid_part(PNG_B, "out@x")], "OUT")
    )
    mid = post(client, outer)
    body = report(client, mid)
    scopes = {s["node_path"]: s["orphan_node_paths"] for s in body["scopes"]}
    assert scopes == {"1": [], None: ["2"]}


def test_empty_message_has_empty_report(client):
    # 纯文本邮件：无 html 叶子、无 related 作用域
    r = client.post(
        "/messages",
        content=b"Subject: plain\r\n\r\njust text\r\n",
    )
    mid = r.json()["id"]
    body = report(client, mid)
    assert body["references"] == []
    assert body["scopes"] == []


def test_fingerprint_echoed_in_response(client):
    mid = post(client, related_message([html_part("<p>x</p>")]))
    msg = client.get(f"/messages/{mid}").json()
    body = report(client, mid)
    assert body["id"] == mid
    assert body["fingerprint"] == msg["fingerprint"]
