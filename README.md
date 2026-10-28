# YAET: Yet Another E-Book Translator

将 EPUB、PDF 或 Markdown 书稿转换成中英对照 Markdown，并可重新导出为 EPUB。

仓库当前提供的是一组小而清晰的脚本：

- `src/yaet_cli.py`: 统一 CLI 入口，支持 `pdf2md`、`epub2md`、`md2md`、`pdf2epub`、`epub2epub`、`translate`
- `src/run_book_pipeline.py`: 一键执行 `epub -> markdown -> heading fix -> translate -> cleanup -> epub`
- `src/yaet_config.py`: YAET 的 YAML 配置加载与运行时覆盖逻辑
- `src/simple_yaml.py`: 无 PyYAML 依赖时的最小 YAML 解析后备实现


## Install

```bash
python3 -m pip install -r requirements.txt
```

在项目根目录提供 API key：

```bash
echo 'DEEPSEEK_API_KEY="your_api_key"' > .env
```

也可以直接导出环境变量：

```bash
export DEEPSEEK_API_KEY="your_api_key"
export MINERU_API_KEY="your_mineru_api_key"
```

仓库根目录还提供了一个包装脚本：

- `./yaet`: 调用 `src/yaet_cli.py` 的便捷入口

## Run From Anywhere

可以，只要调用仓库根目录下的 `yaet` 包装脚本即可。它会自动定位仓库内的 `src/` 和 `.venv/`，`.env` 则按下面的顺序查找：

1. 当前工作目录下的 `.env`
2. 仓库根目录下的 `.env`
3. 家目录下的 `~/.env`

`MINERU_API_KEY` 与 `DEEPSEEK_API_KEY` 都走这套逻辑，所以把 key 放在家目录即可在任意目录下复用。

直接使用绝对路径：

```bash
/path/to/repo/yaet translate "Hello world"
/path/to/repo/yaet epub2epub "/abs/path/book.epub" --max-workers 16
```

也可以把仓库目录加入 `PATH`：

```bash
export PATH="/path/to/repo:$PATH"
yaet translate "Hello world"
```

如果希望长期可用，更推荐加一个软链接：

```bash
mkdir -p ~/.local/bin
ln -sf /path/to/repo/yaet ~/.local/bin/yaet
export PATH="$HOME/.local/bin:$PATH"
```

跨目录执行时建议：

- 输入文件尽量使用绝对路径
- API key 放在仓库根目录 `.env`，或直接导出到环境变量
- 输出仍然默认写到输入文件旁边的 `output/<Book_Name_With_Underscores>/`

## Quick Start

使用新的分阶段子命令：

```bash
./yaet pdf2md "/path/to/paper.pdf"
./yaet pdf2md "/path/to/paper.pdf" --translate --bilingual --max-workers 16
./yaet epub2md "/path/to/book.epub"
./yaet epub2md "/path/to/book.epub" --translate --bilingual --max-workers 16
./yaet epub2md "/path/to/book.epub" --epub-parser-backend epub_translator
./yaet md2md "/path/to/book.md"
./yaet md2md "/path/to/book.md" --translate --bilingual --max-workers 16
./yaet md2md "/path/to/book.md" --translatefree_markdown_translator
./yaet pdf2epub "/path/to/paper.pdf"
./yaet pdf2epub "/path/to/paper.pdf" --translate --bilingual --max-workers 16
./yaet epub2epub "/path/to/book.epub" --max-workers 16
./yaet translate "Hello world"
./yaet translate --bilingual --input notes.txt --output notes.bilingual.txt
```

兼容的一键全流程命令仍然可用：

```bash
./.venv/bin/python src/run_book_pipeline.py "/path/to/book.epub" --max-workers 16
./.venv/bin/python src/run_book_pipeline.py "/path/to/paper.pdf" --max-workers 16
```

默认输出会放到：

```text
output/<Book_Name_With_Underscores>/
```

例如：

```text
output/The_Paper_Menagerie_and_Oth_(Z-Library)/
```

其中通常会包含：

- `<book>.md`
- `<book>.translation_cache.json`
- `<book>.bilingual.md`
- `<book>.bilingual.epub`
- `<book>_assets/`

## Config File

`yaet` 现在支持 YAML 配置文件。查找顺序：

- `--config <path>`
- 仓库根目录 `yaet.yaml`
- 仓库根目录 `yaet.yml`
- 仓库根目录 `config.yaml`
- `~/.yaet/config.yaml`

当前主要配置项：

```yaml
parsers:
  epub: yaet
  markdown: yaet

provider:
  base_url: https://api.deepseek.com
  api_key_env: DEEPSEEK_API_KEY
  model: deepseek-chat

segmentation:
  max_bundle_chars: 6000

translation:
  max_workers: 16
  resume: true

style:
  tone: academic
  audience: general readers
  preserve_terms:
    - Markdown
  instructions:
    - Keep citations stable.

prompt:
  title: ""
  summary: ""
  terms: ""

glossary:
  agent: 智能体
```

CLI 参数仍然优先于配置文件。

## Parser Backends

当前可选后端：

- EPUB: `yaet`、`epub_translator`
- Markdown: `yaet`、`free_markdown_translator`

默认值都是 `yaet`。

示例：

```bash
./yaet epub2md "/path/to/book.epub" --epub-parser-backend epub_translator
./yaet md2md "/path/to/book.md" --translate --markdown-parser-backend free_markdown_translator
./yaet epub2epub "/path/to/book.epub" --config yaet.yaml
```

## Step By Step

### 1. EPUB -> Markdown

```bash
./.venv/bin/python src/convert_epub_to_markdown.py "/path/to/book.epub"
./.venv/bin/python src/convert_epub_to_markdown.py "/path/to/book.epub" --backend epub_translator
```

默认行为：

- 按阅读顺序导出为单个 Markdown
- 自动生成目录
- 保留章节层级
- 导出图片到同目录下的 `<book>_assets/`
- 自动检测封面图片，包括 `titlepage.xhtml` 中 SVG 引用的 `cover.jpeg`
- `--backend epub_translator` 时使用从 `epub-translator` 适配过来的 spine/TOC 解析逻辑

### 1b. PDF -> Markdown

```bash
./.venv/bin/python src/convert_pdf_to_markdown.py "/path/to/paper.pdf"
```

默认行为：

- 使用 `.env` 或环境变量中的 `MINERU_API_KEY`
- 调用 MinerU 云 API 解析 PDF
- 输出到 `output/<book>/<book>.md`
- 自动把 zip 结果中的图片提取到 `<book>_assets/`
- 自动把 Markdown 中的 `images/...` 引用改写为本地资源路径

### 2. Fix Special TOC Links

有些 EPUB 的目录项不是标准 Markdown anchor，而是类似：

```text
[STATE CHANGE](index_split_007.html#filepos28997)
```

这类文件可以用：

```bash
./.venv/bin/python src/fix_special_toc_links.py output/<book>/<book>.md
```

当前规则比较保守，只会对全大写且不过长的特殊标题生效，尽量避免误伤正常标题。

### 3. Translate Markdown

```bash
./.venv/bin/python src/translate_markdown_book.py output/<book>/<book>.md --max-workers 16
```

默认会生成：

```text
output/<book>/<book>.bilingual.md
output/<book>/<book>.translation_cache.json
```

纯中文输出：

```bash
./.venv/bin/python src/translate_markdown_book.py output/<book>/<book>.md --output-mode chinese
```

可选参数：

- `--model`: 默认 `deepseek-chat`
- `--max-chars-per-chunk`: 默认 `6000`
- `--cache-path`: 指定缓存路径
- `--max-workers`: 批次级并发数
- `--no-resume`: 不使用缓存续跑

### 4. Cleanup Bilingual Markdown

```bash
./.venv/bin/python src/cleanup_bilingual_markdown.py output/<book>/<book>.bilingual.md
```

当前后处理会：

- 合并中英文相邻标题
- 更新目录和引用链接文字
- 去掉重复图片
- 去掉 `∞∞∞∞...` 这类分隔符

### 5. Markdown -> EPUB

```bash
./.venv/bin/python src/convert_markdown_to_epub.py output/<book>/<book>.bilingual.md
```

默认会：

- 按 Markdown 标题切分章节
- 生成 EPUB TOC
- 保留 `*`, `**`, `***`, `_`, `__`, `___` 的内联强调格式
- 嵌入图片资源
- 将 `cover*` 图片优先设为 EPUB cover
- 如果没有 `cover*`，回退到第一张图片作为封面

## Progress Monitor

查看翻译进度：

```bash
./.venv/bin/python src/monitor_translation_progress.py output/<book>/<book>.md --cache-path output/<book>/<book>.translation_cache.json
```

支持：

- `--once`: 只打印一次
- `--width`: 调整进度条宽度

## Text Translation CLI

```bash
./yaet translate "Hello world"
./yaet translate --verbose "Hello world"
./yaet translate --bilingual --input notes.txt --output notes.bilingual.txt
cat notes.txt | ./yaet translate --bilingual
```

兼容的旧入口仍然可用：

```bash
./.venv/bin/python src/translate_text_cli.py "Hello world"
./.venv/bin/python src/translate_text_cli.py --verbose "Hello world"
./.venv/bin/python src/translate_text_cli.py --bilingual --input notes.txt --output notes.bilingual.txt
cat notes.txt | ./.venv/bin/python src/translate_text_cli.py --bilingual
```

默认只输出译文。

`--verbose` 还会输出 pronunciation 和 example sentence。

`--bilingual` 会输出双语纯文本：

- 传入 `--input` 时读取 `.txt` 文件
- 不传 `--input` 时优先读取位置参数文本，否则读取 stdin
- 传入 `--output` 时写入双语 `.txt`
- 不传 `--output` 时直接打印到终端

## Naming Notes

默认输出目录和文件名都会做标准化：

- 书名中的空格会替换为下划线 `_`
- 连续空格或下划线会折叠成单个下划线
- 原始输入文件不会被自动重命名

## Recommended Workflow

推荐优先使用分阶段子命令：

```bash
./yaet pdf2md "/path/to/paper.pdf"
./yaet pdf2epub "/path/to/paper.pdf" --translate --bilingual --max-workers 16
./yaet epub2epub "/path/to/book.epub" --max-workers 16
```

兼容入口仍适合“直接全跑完”的场景：

`src/run_book_pipeline.py` 对 EPUB 会内置 `src/fix_special_toc_links.py` 这一步；PDF 输入不会额外跑这一步。

## Repository Notes

- 术语表内置在 `src/translate_markdown_book.py` 的 `GLOSSARY` 中
- 运行时配置入口在 `src/yaet_config.py`
- 图片缓存默认放在输出目录下的 `.epub_image_cache/`
- 翻译缓存是 JSON，可复用来续跑大书
- 翻译缓存 key 现在会带上后端/配置命名空间，避免不同解析配置互相污染
- API 调用会产生费用，建议先试跑小样本
