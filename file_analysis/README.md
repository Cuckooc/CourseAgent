# File_Analysis 文档解析层

文档解析层负责把用户上传的 txt/md/pdf 文件转为可用的纯文本：对 PDF 先做**类型自动检测**，按扫描件/图文/双栏/纯文本四种路径分别解析，产出的文本再交由 service 层脱敏与入库。

## 📁 目录结构

```
file_analysis/
├── doc_type_detector.py    # ⭐ PDF 类型检测（四类路由判定）
├── file.py                 # 文本提取总入口（按扩展名分发）
├── pdf_to_images.py        # PDF 逐页渲染 PNG（OCR/多模态的图片输入）
├── ocr_service.py          # 扫描件 OCR 主流程（qwen-vl-plus）
├── multimodal_service.py   # 图文密集型多模态提取（qwen-vl-max）
├── ocr_clean.py            # OCR 结果清洗（乱码行/乱码字符过滤）
└── two_column_handler.py   # 双栏版式检测与处理
```

## 🔀 PDF 解析路由

`doc_type_detector.py` 依据 **文本密度、乱码率、图片面积占比、双栏布局** 将 PDF 路由为四类：

```
PDF 上传 → 类型检测
├── pure_text    → 直接提取文本（PyMuPDF）
├── scanned      → 逐页渲染图片 → OCR（qwen-vl-plus）→ 文本清洗
├── image_rich   → 逐页多模态转写（qwen-vl-max：表格/图片/混合页）
└── two_column   → 双栏检测 → 中缝切分合并 → 文本提取
```

## 📄 文件详细说明

### `doc_type_detector.py` - 类型检测

判断单个字符是否乱码（非中英文/标点/空白）逐页统计文本密度与乱码率，结合图片面积与双栏布局给出类型判定，决定后续解析路径。

### `file.py` - 总入口

读取文件文本：按扩展名分发提取（txt/md 直读，pdf 走上述路由）。

### `pdf_to_images.py` - 页面渲染

将 PDF 每页渲染为临时 PNG 返回路径列表；**失败时在 finally 中清理临时图片与目录**，不留垃圾文件。

### `ocr_service.py` - 扫描件 OCR

逐页调用 `qwen-vl-plus` 多模态 OCR，支持**单页重试/降级**（单页失败不拖垮整个文档），结束后清理临时图片。

### `multimodal_service.py` - 多模态提取

`image_rich` 类型 PDF 逐页调用 `qwen-vl-max`，将表格/图片/混合页面转写为结构化文本，供后续切分与向量入库。

### `ocr_clean.py` + `two_column_handler.py`

- **ocr_clean**: 乱码行判定与过滤（三类清洗规则），提升 OCR 产出质量
- **two_column_handler**: 检测页面双栏布局并返回中缝占页宽比例，指导按栏序提取

## 🔗 依赖关系

- 被 `service/file_service.py` 调用（上传入库链路第一步）
- OCR/多模态产出的文本可进入**人工审核流**（`service/review_service.py`），审核通过后才切分入库
- 依赖 PyMuPDF（渲染/文本提取）与 DashScope（qwen-vl 系列，Key 见 [env/README.md](../env/README.md)）
