# Embedding 向量嵌入层

向量嵌入层负责文本向量化与父子块切分，是 RAG 链路"文本 → 向量"的桥梁，统一基于 DashScope `text-embedding-v2` 模型。

## 📁 目录结构

```
embedding/
├── embedding_model.py   # ⭐ 嵌入模型封装（重试 + 查询缓存 + 维度一致性保障）
├── parent_child.py      # 父子块切分（自适应策略 + Chroma metadata 组装）
└── text_embedding.py    # 内置 JSON 知识库的嵌入客户端与 Chroma 构建
```

## 📄 文件详细说明

### `embedding_model.py` - 嵌入模型封装

- 包装 DashScope `text-embedding-v2`，实现 LangChain Embeddings 接口（`embed_documents` / `embed_query`）
- **保证两接口向量维度一致**，避免检索链路因模型切换失效
- 异常分类：致命错误（401/403/非法密钥）立即失败，其余按 tenacity 重试
- 查询向量缓存，减少重复 embedding 调用开销

### `parent_child.py` - 父子块切分

- **自适应策略**: 根据文本长度选择切分参数
- **父块**保留完整语境，**子块**保证检索精度，检索命中子块后返回父块内容
- 组装 Chroma 所需 metadata：`file_id / scope / version / is_latest` 等字段（供 `multi_agent/retrieval.py` 做版本过滤）

### `text_embedding.py` - 知识库构建

- 读取内置 JSON 问答知识库文件并解析为条目列表
- `text-embedding-v2` 客户端单例、分批写入 Chroma、已有向量库加载（不存在才重建）

## 🔗 依赖关系

- 被 `service/file_service.py`（上传文件入库）与 `multi_agent/rag_agent.py`（知识库构建）依赖
- 向量持久化由 `service/vector_store.py`（全局库）与 `service/temp_knowledge_store.py`（会话库）管理
