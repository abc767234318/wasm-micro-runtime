# WebAssembly Core Specification 3.0 中英对照站

这是一个可重复生成的 WebAssembly Core Specification 中英对照项目。英文内容同步自 [WebAssembly 官方规范](https://webassembly.github.io/spec/core/)，中文是面向学习的非官方译文。

> 英文规范是唯一权威文本。当中英文存在歧义时，以官方英文为准。

## 功能

- 完整保留官方 Core Specification 的章节路径、锚点、数学公式、语法产生式和内部链接。
- 桌面端中英双栏，两栏可同步滚动；移动端可切换英文/中文。
- 侧边目录同时显示英文与中文标题。
- 全站本地双语搜索，不向搜索服务发送查询。
- 翻译按内容 hash 缓存，官方站更新时只处理新增或变化的文本。
- 自动保护 MathJax、literal、`code`、`pre`、SVG 和图片，避免翻译破坏规范符号。
- 指令、类型、语义规则和总索引页仅翻译标题与说明；`i32.add` 等规范标识符保留英文，便于与实现和测试集对照。

## 目录

```text
wasm-spec-bilingual/
├── scripts/
│   ├── build_bilingual.py   # 抓取、翻译、注入双语 UI
│   └── verify_site.py       # 页面、链接、锚点、译文覆盖率检查
├── theme/                       # 双语站的 CSS/JavaScript
├── translations/
│   ├── cache.json            # 翻译记忆，生成后保留
│   └── glossary.json         # 术语表
├── site/                        # 可直接托管的静态站点
└── requirements.txt
```

## 生成

Python 3.10–3.12 且系统需要 `wget`。首次运行会下载约 70MB 的英→中离线翻译模型：

```bash
cd notes/wasm-spec-bilingual
./scripts/bootstrap_argos.sh
.venv/bin/python scripts/build_bilingual.py
.venv/bin/python scripts/verify_site.py
python3 -m http.server 8000 --directory site
```

然后访问 <http://localhost:8000/>。

### 更新官方内容

```bash
.venv/bin/python scripts/build_bilingual.py --refresh-upstream
.venv/bin/python scripts/verify_site.py
```

`--refresh-upstream` 会重新镜像官方 Core 站点。已经存在于 `translations/cache.json` 的相同文本不会再次请求翻译。

## 翻译质量策略

默认构建器使用本地 Argos Translate 英→中模型生成学习草译，全量重建不依赖翻译 API，并通过术语表和不可翻译节点保护提高一致性。机器译文不是规范性文本；可以直接编辑 cache 中的条目进行人工校订，后续构建会保留校订结果。`--translator google` 仅作为可选在线后端。

## 授权与署名

官方规范文档使用 [W3C Software and Document License](https://www.w3.org/copyright/software-license/)。生成页面保留 WebAssembly Community Group 版权信息、官方原文链接、同步时间和版本。本项目的实现代码遵循当前 WAMR 仓库的授权条款。
