"""平台原件与计算表独立归档。接口分析结果绝不能冒充平台下载原件。"""

import csv
import hashlib
import io
import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import RLock
from uuid import uuid4
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile, ZIP_DEFLATED

from app.adapters.xlsx_simple import _worksheet
from app.domain.errors import DomainError

LOCK = RLock()
NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
REL = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'


def calculated_workbook(tables: list[dict]) -> bytes:
    """保留所有可识别的搜索词工作表及前言，禁止只返回第一张表。"""
    ns, rel = NS['s'], 'http://schemas.openxmlformats.org/package/2006/relationships'
    office = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    content = ET.Element('Types', xmlns='http://schemas.openxmlformats.org/package/2006/content-types')
    ET.SubElement(content, 'Default', Extension='rels', ContentType='application/vnd.openxmlformats-package.relationships+xml')
    ET.SubElement(content, 'Default', Extension='xml', ContentType='application/xml')
    ET.SubElement(content, 'Override', PartName='/xl/workbook.xml', ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml')
    workbook = ET.Element('workbook', xmlns=ns)
    sheets = ET.SubElement(workbook, 'sheets')
    relationships = ET.Element('Relationships', xmlns=rel)
    used = set()
    output = io.BytesIO()
    with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
        for number, table in enumerate(tables, 1):
            label = re.sub(r'[\[\]:*?/\\]', '_', table['name'])[:31] or '平台下载表'
            name, suffix = label, 1
            while name.casefold() in used:
                suffix += 1
                name = label[:31-len(str(suffix))-1] + '-' + str(suffix)
            used.add(name.casefold())
            ET.SubElement(sheets, 'sheet', {'name': name, 'sheetId': str(number), REL: f'rId{number}'})
            ET.SubElement(relationships, 'Relationship', Id=f'rId{number}', Type=office+'/worksheet', Target=f'worksheets/sheet{number}.xml')
            ET.SubElement(content, 'Override', PartName=f'/xl/worksheets/sheet{number}.xml', ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml')
            rows = [*table.get('prefix', []), table['headers'], *table['rows']]
            archive.writestr(f'xl/worksheets/sheet{number}.xml', _worksheet(rows, max(map(len, rows), default=1)))
        archive.writestr('[Content_Types].xml', ET.tostring(content, encoding='utf-8', xml_declaration=True))
        archive.writestr('xl/workbook.xml', ET.tostring(workbook, encoding='utf-8', xml_declaration=True))
        archive.writestr('xl/_rels/workbook.xml.rels', ET.tostring(relationships, encoding='utf-8', xml_declaration=True))
        archive.writestr('_rels/.rels', f'<Relationships xmlns="{rel}"><Relationship Id="rId1" Type="{office}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
    return output.getvalue()


def key(value: str) -> str:
    return re.sub(r'[^a-z0-9\u3400-\u9fff]', '', value.casefold())


def term_column(header: str) -> bool:
    return key(header) in {'搜索词', '关键词', '搜索词汇', 'searchterm', 'searchterms', 'keyword', 'stsearchterm'}


def _metric_position(headers: list[str], position: int, kind: str) -> int | None:
    for index, header in enumerate(headers):
        text = key(header)
        # "Top Clicked Brand #1" is a brand, not a click share. Likewise the
        # "Clicked" prefix of Conversion Share must never select the click column.
        share = ('clickshare' in text or '点击份额' in text or '点击占比' in text) if kind == 'click' else ('conversionshare' in text or '转化份额' in text or '转化贡献占比' in text or '购买份额' in text)
        if not share:
            continue
        if text == f'sttopasinclickshare{position}' and kind == 'click':
            return index
        if text == f'sttopasinconversionshare{position}' and kind != 'click':
            return index
        if re.search(rf'(?:top|第|商品|asin)?{position}(?!\d)', text) or ('一二三'[position - 1] in text) or (position == 1 and ('最高' in text or 'highest' in text)):
            return index
    return None


def enrich_table(headers: list[str], rows: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    """原列原序保留，移除旧计算列后在搜索词后插入两列；缺证据时留空并解释。"""
    metrics = {'购买能力', '购买比', '垄断程度', '垄断程度top3', '垄断程度top3累计', '前三商品转化份额合计', '指标缺失原因'}
    keep = [i for i, header in enumerate(headers) if key(header) not in metrics and not key(header).startswith(('购买能力', '垄断程度'))]
    headers = [headers[i] for i in keep]
    source = [[str(row[i]) if i < len(row) else '' for i in keep] for row in rows]
    term = next((i for i, header in enumerate(headers) if term_column(header)), None)
    if term is None:
        raise DomainError(422, 'EXPORT_SEARCH_TERM_MISSING', '下载表中没有搜索词列，无法核验和计算')
    positions = [_metric_position(headers, n, kind) for n, kind in [(1, 'click'), (1, 'conversion'), (2, 'conversion'), (3, 'conversion')]]
    output = []
    for row in source:
        values = []
        for index in positions:
            try:
                text = row[index].strip() if index is not None else ''
                value = Decimal(text.rstrip('%')) / 100
                values.append(value if value.is_finite() and 0 <= value <= 1 else None)
            except (InvalidOperation, ValueError):
                values.append(None)
        click, first, second, third = values
        purchase = first / click if click is not None and click > 0 and first is not None else None
        monopoly = first + second + third if all(v is not None for v in (first, second, third)) else None
        fmt = lambda value: '' if value is None else f'{value * 100:.2f}%'
        reason = []
        if purchase is None:
            reason.append('缺少第一名点击/转化份额或点击份额为零')
        if monopoly is None:
            reason.append('缺少前三名转化份额')
        output.append(row[:term + 1] + [fmt(purchase), fmt(monopoly)] + row[term + 1:] + ['；'.join(reason)])
    return headers[:term + 1] + ['购买能力', '垄断程度'] + headers[term + 1:] + ['指标缺失原因'], output


def read_original(filename: str, raw: bytes) -> list[dict]:
    """识别 ABA 的元数据前言和多工作表，按 XLSX 样式还原百分比，避免二次除100。"""
    if len(raw) > 40 * 1024 * 1024:
        raise DomainError(422, 'EXPORT_TOO_LARGE', '平台下载文件超过40MB限制')
    tables = []
    try:
        if raw.startswith(b'PK'):
            with ZipFile(io.BytesIO(raw)) as archive:
                if sum(info.file_size for info in archive.infolist()) > 160 * 1024 * 1024:
                    raise DomainError(422, 'EXPORT_TOO_LARGE', '平台下载文件解压后过大')
                shared = []
                if 'xl/sharedStrings.xml' in archive.namelist():
                    shared = [''.join(n.itertext()) for n in ET.fromstring(archive.read('xl/sharedStrings.xml')).findall('s:si', NS)]
                percent_styles = set()
                if 'xl/styles.xml' in archive.namelist():
                    styles = ET.fromstring(archive.read('xl/styles.xml'))
                    custom = {int(n.attrib['numFmtId']): n.attrib.get('formatCode', '') for n in styles.findall('s:numFmts/s:numFmt', NS)}
                    for i, style in enumerate(styles.findall('s:cellXfs/s:xf', NS)):
                        num = int(style.attrib.get('numFmtId', 0))
                        if num in (9, 10) or '%' in custom.get(num, ''):
                            percent_styles.add(i)
                relations = {r.attrib['Id']: r.attrib['Target'] for r in ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))}
                sheets = ET.fromstring(archive.read('xl/workbook.xml')).findall('s:sheets/s:sheet', NS)
                for sheet in sheets:
                    target = relations[sheet.attrib[REL]].lstrip('/')
                    target = target if target.startswith('xl/') else 'xl/' + target
                    records = []
                    for row in ET.fromstring(archive.read(target)).findall('s:sheetData/s:row', NS):
                        values = []
                        for cell in row.findall('s:c', NS):
                            letters = re.match('[A-Z]+', cell.attrib.get('r', 'A1')).group()
                            column = 0
                            for letter in letters:
                                column = column * 26 + ord(letter) - 64
                            values += [''] * max(0, column - len(values))
                            value = cell.findtext('s:v', '', NS)
                            if cell.attrib.get('t') == 's':
                                value = shared[int(value)]
                            elif cell.attrib.get('t') == 'inlineStr':
                                value = ''.join(cell.find('s:is', NS).itertext())
                            elif value and cell.attrib.get('t', 'n') == 'n' and int(cell.attrib.get('s', 0)) in percent_styles:
                                value = str(Decimal(value) * 100) + '%'
                            values[column - 1] = value
                        records.append(values)
                    tables.append({'name': sheet.attrib['name'], 'records': records})
        else:
            text = raw.decode('utf-16' if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig')
            try:
                dialect = csv.Sniffer().sniff(text[:32768], delimiters=',\t;')
            except csv.Error:
                dialect = csv.excel_tab if filename.lower().endswith('.tsv') else csv.excel
            tables = [{'name': '平台下载表', 'records': list(csv.reader(io.StringIO(text), dialect=dialect))}]
    except (BadZipFile, KeyError, ET.ParseError, UnicodeError, ValueError, IndexError, AttributeError, InvalidOperation):
        raise DomainError(422, 'EXPORT_PARSE_FAILED', '平台下载文件无法解析，原件已保留') from None
    output = []
    for table in tables:
        records = table['records']
        index = next((i for i, row in enumerate(records[:30]) if any(term_column(c) for c in row)), None)
        if index is not None:
            headers, rows = enrich_table(records[index], records[index + 1:])
            output.append({'name': table['name'], 'prefix': records[:index], 'headers': headers, 'rows': rows})
    if not output:
        raise DomainError(422, 'EXPORT_SEARCH_TERM_MISSING', '下载表未找到搜索词表头，原件已保留')
    return output


class SourceExportStore:
    def __init__(self, root: Path):
        self.root = root / 'source_exports'

    def _path(self, project: str, run: str, export_id: str) -> Path:
        # 所有路径分量都散列/校验，不接受任意客户端路径。
        if not re.fullmatch('[a-f0-9]{32}', export_id):
            raise DomainError(404, 'SOURCE_EXPORT_NOT_FOUND', '下载表不存在')
        scope = hashlib.sha256(f'{project}/{run}'.encode()).hexdigest()
        return self.root / scope / export_id

    def save(self, item: dict) -> None:
        path = self._path(item['project_id'], item['run_id'], item['id'])
        with LOCK:
            path.mkdir(parents=True, exist_ok=True)
            item['updated_at'] = datetime.now(UTC).isoformat()
            temporary = path / (uuid4().hex + '.tmp')
            temporary.write_text(json.dumps(item, ensure_ascii=False), encoding='utf-8')
            temporary.replace(path / 'manifest.json')

    def create(self, project: str, run: str, kind: str, owner: str, query: str, request: dict) -> dict:
        item = dict(id=uuid4().hex, project_id=str(project), run_id=str(run), kind=kind, owner=str(owner), query=query, request=request, status='queued', error_code='', error='', source_type='platform_download', tables=[], filename='', sha256='')
        self.save(item)
        return item

    def get(self, project: str, run: str, export_id: str) -> dict:
        try:
            return json.loads((self._path(str(project), str(run), export_id) / 'manifest.json').read_text(encoding='utf-8'))
        except FileNotFoundError:
            raise DomainError(404, 'SOURCE_EXPORT_NOT_FOUND', '下载表不存在') from None

    def list(self, project: str, run: str) -> list[dict]:
        scope = self._path(str(project), str(run), '0' * 32).parent
        return [json.loads(p.read_text(encoding='utf-8')) for p in sorted(scope.glob('*/manifest.json'))]

    def fail(self, item: dict, code: str, reason: str) -> None:
        item.update(status='download_failed', error_code=code, error=reason)
        self.save(item)

    def complete(self, item: dict, filename: str, raw: bytes) -> None:
        path = self._path(item['project_id'], item['run_id'], item['id'])
        path.mkdir(parents=True, exist_ok=True)
        (path / 'original.bin').write_bytes(raw)
        item.update(filename=Path(filename).name, sha256=hashlib.sha256(raw).hexdigest(), status='parsing')
        self.save(item)
        try:
            item['tables'] = read_original(filename, raw)
            item.update(status='completed', error='', error_code='')
            self.save(item)
        except DomainError as error:
            item.update(status='parse_failed', error_code=error.code, error=error.message)
            self.save(item)

    def file(self, item: dict, original: bool) -> bytes:
        if original:
            if not item.get('sha256'):
                raise DomainError(409, 'SOURCE_EXPORT_NOT_READY', '平台原件尚未下载')
            try:
                payload = (self._path(item['project_id'], item['run_id'], item['id']) / 'original.bin').read_bytes()
            except FileNotFoundError:
                raise DomainError(409, 'SOURCE_EXPORT_ORIGINAL_MISSING', '平台原件文件缺失，请重试下载') from None
            if hashlib.sha256(payload).hexdigest() != item['sha256']:
                raise DomainError(409, 'EXPORT_CHECKSUM_FAILED', '下载原件校验失败')
            return payload
        if item['status'] != 'completed' or not item['tables']:
            raise DomainError(409, 'SOURCE_EXPORT_NOT_READY', item.get('error') or '平台表仍在下载或解析')
        return calculated_workbook(item['tables'])
