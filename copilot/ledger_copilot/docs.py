"""用户发来的文件：保存、按格式抽取摘要。老格式（doc/xls 以外的 wps/ppt/rtf 等）先用 LibreOffice 转成新格式再读。"""
import base64
import mimetypes
import re
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

from . import config

UPLOADS = config.ROOT / 'uploads'
SOFFICE = Path(r'C:\Program Files\LibreOffice\program\soffice.exe')
FILE_ID = re.compile(r'^\d{6}/[0-9a-f]{12}\.[a-z0-9]{1,8}$')
MAX_BYTES = 50 * 1024 * 1024
LIMIT = 12000
IMAGE = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'}
SHEET = {'.xlsx', '.xlsm', '.xltx', '.xltm'}
SHEET_OLD = {'.et', '.ett', '.ods', '.fods', '.numbers', '.xlsb'}
WORD = {'.docx', '.docm', '.dotx', '.dotm'}
WORD_OLD = {'.doc', '.dot', '.wps', '.wpt', '.rtf', '.odt', '.fodt', '.pages'}
SLIDES = {'.pptx', '.pptm', '.ppsx'}
SLIDES_OLD = {'.ppt', '.pps', '.pot', '.dps', '.dpt', '.odp', '.key'}
TEXT = {'.csv', '.tsv', '.txt', '.md', '.json', '.xml', '.log', '.sql', '.py', '.yaml', '.yml', '.ini', '.html', '.htm'}


def save(name, data):
    if len(data) > MAX_BYTES:
        raise ValueError('文件超过 50MB')
    folder = UPLOADS / datetime.now().strftime('%Y%m')
    folder.mkdir(parents=True, exist_ok=True)
    ext = Path(name).suffix.lower()[:9] or '.bin'
    if not re.fullmatch(r'\.[a-z0-9]{1,8}', ext):
        ext = '.bin'
    stored = f'{uuid.uuid4().hex[:12]}{ext}'
    (folder / stored).write_bytes(data)
    return _info(f'{folder.name}/{stored}', name, folder / stored)


def _info(file_id, name, path):
    ext = path.suffix.lower()
    return {'file_id': file_id, 'name': name, 'size': path.stat().st_size, 'path': str(path),
            'kind': 'image' if ext in IMAGE else 'file', 'url': f'/api/agent/uploads/{file_id}'}


def resolve(files):
    out = []
    for f in list(files or [])[:10]:
        file_id = str(f.get('file_id', ''))
        path = UPLOADS / file_id
        if FILE_ID.match(file_id) and path.is_file():
            out.append(_info(file_id, str(f.get('name') or path.name)[:200], path))
    return out


def data_url(f):
    mime = mimetypes.guess_type(f['path'])[0] or 'image/png'
    return f'data:{mime};base64,' + base64.b64encode(Path(f['path']).read_bytes()).decode()


def describe(f):
    """返回 (给模型看的摘要, 需要视觉识别的页面图片 data url 列表)。"""
    path = Path(f['path'])
    ext = path.suffix.lower()
    try:
        if ext in SHEET:
            return _cut(_xlsx(path)), []
        if ext == '.xls':
            return _cut(_xls(path)), []
        if ext in SHEET_OLD:
            return _cut(_xlsx(_convert(path, 'xlsx'))), []
        if ext in WORD:
            return _cut(_docx(path)), []
        if ext in WORD_OLD:
            return _cut(_docx(_convert(path, 'docx'))), []
        if ext == '.pdf':
            return _pdf(path)
        if ext in SLIDES:
            return _cut(_pptx(path)), []
        if ext in SLIDES_OLD:
            return _cut(_pptx(_convert(path, 'pptx'))), []
        if ext in TEXT:
            return _cut(_text(path)), []
        if ext == '.zip':
            import zipfile
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
            return _cut(f'压缩包，共 {len(names)} 个文件：\n' + '\n'.join(names[:200])), []
    except Exception as exc:
        return f'（读取失败：{type(exc).__name__}: {str(exc)[:300]}；可以用 python 工具直接读原文件再试）', []
    return f'（{ext or "无扩展名"} 格式不做自动预览，{f["size"]} 字节；需要时用 python 工具读原文件）', []


def _cut(text):
    return text if len(text) <= LIMIT else text[:LIMIT] + f'\n…（共 {len(text)} 字，只放了前面一部分，完整内容用 python 读原文件）'


def _cell(value):
    if value is None:
        return ''
    text = str(value).replace('\n', ' ').strip()
    return text[:40]


def _table(rows, total_rows, total_cols, title):
    lines = [f'【{title}】约 {total_rows} 行 × {total_cols} 列，前 {len(rows)} 行：']
    lines += [' | '.join(_cell(v) for v in row).rstrip(' |') for row in rows]
    return '\n'.join(lines)


def _xlsx(path):
    import openpyxl
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    parts = [f'Excel 工作簿，{len(book.sheetnames)} 个工作表：' + '、'.join(book.sheetnames)]
    for sheet in book.worksheets[:10]:
        rows = []
        for row in sheet.iter_rows(values_only=True):
            if any(v is not None for v in row):
                rows.append(row[:30])
            if len(rows) >= 15:
                break
        parts.append(_table(rows, sheet.max_row or 0, sheet.max_column or 0, sheet.title))
    book.close()
    return '\n\n'.join(parts)


def _xls(path):
    import xlrd
    book = xlrd.open_workbook(str(path), on_demand=True)
    parts = [f'Excel(xls) 工作簿，{book.nsheets} 个工作表：' + '、'.join(book.sheet_names())]
    for index in range(min(book.nsheets, 10)):
        sheet = book.sheet_by_index(index)
        rows = [sheet.row_values(r)[:30] for r in range(min(sheet.nrows, 15))]
        parts.append(_table(rows, sheet.nrows, sheet.ncols, '工作表 ' + sheet.name))
    return '\n\n'.join(parts)


def _docx(path):
    import docx
    document = docx.Document(str(path))
    parts = [p.text for p in document.paragraphs if p.text.strip()]
    for number, table in enumerate(document.tables[:10], 1):
        rows = [[c.text for c in row.cells] for row in table.rows[:20]]
        parts.append(_table(rows, len(table.rows), len(table.columns), f'表格{number}'))
    return 'Word 文档：\n' + '\n'.join(parts)


def _pptx(path):
    import pptx
    deck = pptx.Presentation(str(path))
    parts = []
    for number, slide in enumerate(deck.slides, 1):
        texts = [shape.text_frame.text for shape in slide.shapes if shape.has_text_frame and shape.text_frame.text.strip()]
        parts.append(f'【第 {number} 页】' + ' / '.join(texts))
    return f'演示文稿，{len(parts)} 页：\n' + '\n'.join(parts)


def _pdf(path):
    import pypdf
    reader = pypdf.PdfReader(str(path))
    pages = [(page.extract_text() or '').strip() for page in reader.pages[:50]]
    text = '\n'.join(f'【第 {i} 页】{t}' for i, t in enumerate(pages, 1) if t)
    head = f'PDF，{len(reader.pages)} 页。\n'
    if sum(len(t) for t in pages) >= 40 * max(1, len(pages)):
        return _cut(head + text), []
    import pymupdf as fitz
    images = []
    with fitz.open(str(path)) as document:
        for page in list(document)[:4]:
            png = page.get_pixmap(matrix=fitz.Matrix(1.6, 1.6)).tobytes('png')
            images.append('data:image/png;base64,' + base64.b64encode(png).decode())
    return _cut(head + '看起来是扫描件，前几页已转成图片识别。' + (('\n' + text) if text else '')), images


def _text(path):
    raw = path.read_bytes()
    text = encoding = None
    for candidate in ('utf-8-sig', 'gb18030'):
        try:
            text, encoding = raw.decode(candidate), candidate
            break
        except UnicodeDecodeError:
            pass
    if text is None:
        from charset_normalizer import from_bytes
        best = from_bytes(raw[:2_000_000]).best()
        text, encoding = (str(best), best.encoding) if best else (raw.decode('utf-8', 'replace'), 'utf-8')
    lines = text.splitlines()
    return f'文本文件（{encoding}），{len(lines)} 行，前 40 行：\n' + '\n'.join(lines[:40])


def _convert(path, fmt):
    target = path.with_name(path.stem + '.conv.' + fmt)
    if target.exists():
        return target
    if not SOFFICE.exists():
        raise RuntimeError('本机没装 LibreOffice，老格式暂时打不开')
    profile = (config.ROOT / 'soffice-profile').resolve()
    out = config.ROOT / 'uploads' / 'converted'
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(SOFFICE), '--headless', '--norestore', f'-env:UserInstallation={profile.as_uri()}',
                    '--convert-to', fmt, '--outdir', str(out), str(path)], capture_output=True, timeout=180)
    produced = out / (path.stem + '.' + fmt)
    if not produced.exists():
        raise RuntimeError('格式转换失败')
    produced.replace(target)
    return target
