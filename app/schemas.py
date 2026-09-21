"""API 响应模型。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class MessageSummary(BaseModel):
    id: int
    fingerprint: str
    raw_sha256: str
    content_type: str
    size_bytes: int
    created_at: str


class MessageListResponse(BaseModel):
    items: list[MessageSummary]
    total: int
    limit: int
    offset: int


class MessageCreated(BaseModel):
    id: int
    fingerprint: str
    raw_sha256: str
    content_type: str
    size_bytes: int
    created: bool  # True=新入库；False=同指纹邮件已存在（幂等）
    node_count: int


class NodeSummary(BaseModel):
    path: str
    depth: int
    position: int
    content_type: str
    kind: str
    cte: str
    disposition: Optional[str] = None
    children: list[str]


class TreeNode(BaseModel):
    """嵌套 MIME 树节点，children 为完整子树。"""

    path: str
    depth: int
    position: int
    content_type: str
    kind: str
    cte: str
    disposition: Optional[str] = None
    children: list["TreeNode"] = []


TreeNode.model_rebuild()


class TreeResponse(BaseModel):
    id: int
    fingerprint: str
    tree: TreeNode


class NodeListResponse(BaseModel):
    id: int
    fingerprint: str
    nodes: list[NodeSummary]


class NodeDetail(NodeSummary):
    headers: list[list[str]]
    text: Optional[str] = None
    blob_sha256: Optional[str] = None
    blob_length: Optional[int] = None


class NodeDetailResponse(BaseModel):
    id: int
    fingerprint: str
    node: NodeDetail


class RelatedResourceReference(BaseModel):
    """text/html 叶子中一条 ``cid:`` 引用的解析结果。

    * ``resolved``：唯一匹配到同作用域内一个 Content-ID 节点；
    * ``missing``：最近 multipart/related 作用域（或没有作用域）内
      不存在该 Content-ID；
    * ``ambiguous``：同作用域内多个节点声明了同一 Content-ID，
      ``candidate_node_paths`` 列出全部候选。
    """

    source_node_path: str
    attribute: str  # src / href / poster / srcset
    cid: str
    scope_node_path: Optional[str] = None
    target_node_path: Optional[str] = None
    status: str  # resolved / missing / ambiguous
    candidate_node_paths: list[str] = []


class RelatedResourceScope(BaseModel):
    """一个 multipart/related 作用域的未引用资源清单。

    ``node_path`` 为 ``null`` 的伪作用域收集不属于任何 related 祖先
    的 Content-ID 节点；它们永远不可能被引用。
    """

    node_path: Optional[str] = None
    orphan_node_paths: list[str] = []


class RelatedResourcesResponse(BaseModel):
    id: int
    fingerprint: str
    references: list[RelatedResourceReference]
    scopes: list[RelatedResourceScope]


class AttachmentOccurrence(BaseModel):
    """附件的一次出现：哪封邮件的哪个节点、以什么文件名/处置方式。"""

    message_id: int
    node_path: str
    filename: Optional[str] = None
    disposition: Optional[str] = None
    media_type: str


class AttachmentSummary(BaseModel):
    sha256: str
    size_bytes: int
    media_type: str  # 首次归档时的媒体类型
    reference_count: int
    created_at: str
    occurrences: list[AttachmentOccurrence]


class AttachmentListResponse(BaseModel):
    items: list[AttachmentSummary]
    total: int
    limit: int
    offset: int


class ErrorBody(BaseModel):
    code: str
    message: str
    node_path: str
    detail: dict


class ErrorResponse(BaseModel):
    error: ErrorBody
