"""pytest 共享夹具：临时数据库 + TestClient。"""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("MIME_DB_PATH", db_path)

    # 延迟导入；lifespan 启动时读取 MIME_DB_PATH 并建表。
    from app.main import app

    with TestClient(app) as c:
        yield c


def make_multipart(
    parts: list[bytes],
    boundary: str = "BOUNDARY123",
    *,
    crlf: bool = True,
    preamble: bytes | None = None,
    epilogue: bytes | None = None,
) -> bytes:
    nl = b"\r\n" if crlf else b"\n"
    chunks: list[bytes] = []
    if preamble is not None:
        chunks.append(preamble + nl)
    for part in parts:
        chunks.append(b"--" + boundary.encode() + nl)
        chunks.append(part)
        chunks.append(nl)
    chunks.append(b"--" + boundary.encode() + b"--")
    if epilogue is not None:
        chunks.append(nl + epilogue)
    return b"".join(chunks)


def text_part(
    body: str,
    charset: str = "utf-8",
    cte: str | None = None,
    *,
    headers: bytes = b"",
) -> bytes:
    extra = headers
    cte_header = b""
    if cte:
        cte_header = f"Content-Transfer-Encoding: {cte}\r\n".encode()
    head = (
        f"Content-Type: text/plain; charset={charset}\r\n".encode()
        + cte_header
        + extra
        + b"\r\n"
    )
    return head + body.encode(charset)


def html_part(
    body: str,
    charset: str = "utf-8",
    cte: str | None = None,
    *,
    headers: bytes = b"",
) -> bytes:
    """构造一个 text/html 部分（用法同 text_part）。"""

    extra = headers
    cte_header = b""
    if cte:
        cte_header = f"Content-Transfer-Encoding: {cte}\r\n".encode()
    head = (
        f"Content-Type: text/html; charset={charset}\r\n".encode()
        + cte_header
        + extra
        + b"\r\n"
    )
    return head + body.encode(charset)


def cid_part(
    data: bytes,
    cid: str,
    *,
    ctype: str = "image/png",
    disposition: str = "inline",
    extra_headers: bytes = b"",
) -> bytes:
    """构造一个带 Content-ID 的非文本叶子（默认 base64 inline 图片）。"""

    import base64 as _b64

    head = (
        f"Content-Type: {ctype}\r\n"
        f"Content-ID: <{cid}>\r\n"
        f"Content-Disposition: {disposition}\r\n"
        f"Content-Transfer-Encoding: base64\r\n"
    ).encode() + extra_headers + b"\r\n"
    return head + _b64.b64encode(data)


def related_message(
    parts: list[bytes],
    boundary: str = "REL",
    *,
    subtype: str = "related",
    extra_headers: bytes = b"",
) -> bytes:
    """构造一封 multipart 邮件（默认 multipart/related）。"""

    return (
        b"Subject: related test\r\n"
        + extra_headers
        + f"Content-Type: multipart/{subtype}; boundary={boundary}\r\n\r\n".encode()
        + make_multipart(parts, boundary)
    )
