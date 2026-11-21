"""按研究和来源版本归档接口采集表，保留历史平台原件。"""

import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from uuid import NAMESPACE_URL, uuid4, uuid5
from zipfile import BadZipFile, ZipFile

from app.adapters.browser_tasks import BrowserTaskExecutor
from app.adapters.remote_browser import (browser_operation_context, browser_device_context,
    current_browser_device_id, submit_with_browser_operation_context)
from app.adapters.source_exports import SourceExportStore, read_original
from app.domain.errors import DomainError
from app.domain.aba import AbaQueryReport
from app.domain.sellersprite import BrowserCollectionInput


def validate_aba_platform_file(raw: bytes) -> None:
    """拒绝 JSON/HTML 等接口响应，只有真实 XLSX 工作簿才可标记为平台原件。"""
    if not raw.startswith(b'PK'):
        raise DomainError(502, 'ABA_PLATFORM_FILE_INVALID', '紫鸟返回的内容不是 ABA 平台 Excel 原件')
    try:
        with ZipFile(io.BytesIO(raw)) as archive:
            names = set(archive.namelist())
            if '[Content_Types].xml' not in names or 'xl/workbook.xml' not in names:
                raise DomainError(502, 'ABA_PLATFORM_FILE_INVALID', '紫鸟返回的内容不是有效的 ABA Excel 工作簿')
            if archive.testzip() is not None:
                raise DomainError(502, 'ABA_PLATFORM_FILE_INVALID', 'ABA 平台 Excel 原件校验失败，请重新下载')
    except BadZipFile:
        raise DomainError(502, 'ABA_PLATFORM_FILE_INVALID', 'ABA 平台 Excel 原件损坏，不能作为下载成功') from None


def export_name(item: dict) -> str:
    prefix = {'step2': '第二步热门搜索词', 'aba_forward': '第四步ABA正向',
              'aba_reverse': '第四步ABA反向', 'ss_forward': '卖家精灵正向',
              'ss_reverse': '卖家精灵反向'}[item['kind']]
    query = item['query'] if len(item['request'].get('queries', [])) <= 1 else '多ASIN'
    import re
    suffix = '-未完整' if item.get('truncated') is True else ''
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', prefix + query)[:180 - len(suffix)] + suffix + '.xlsx'


class SourceExportService:
    def __init__(self, repository, aba, sellersprite=None):
        self.repository, self.aba, self.sellersprite = repository, aba, sellersprite
        self.store = SourceExportStore(repository.storage_root)
        self.executor = BrowserTaskExecutor(thread_name_prefix='source-export')
        self.lock = RLock()
        # 服务重启不会伪报成功，也不会在没有设备上下文时偷偷重下。
        for path in self.store.root.glob('*/*/manifest.json'):
            import json
            item = json.loads(path.read_text(encoding='utf-8'))
            if item['status'] in ('queued', 'downloading', 'parsing'):
                self.store.fail(item, 'EXPORT_INTERRUPTED', '服务重启导致下载中断，请重试下载')

    def enqueue(self, project, run, kind, owner, query, request, *, report=None, source_file=None):
        with self.lock:
            existing = next((i for i in self.store.list(project, run)
                if i['owner'] == str(owner) and i['query'] == query and i['kind'] == kind), None)
            if existing:
                return existing
            item = self.store.create(project, run, kind, owner, query, request)
            item['source_type'] = 'api_generated'
            client = getattr(self.aba, '_client', None)
            # At submission time bind the actual selected computer, including the legacy
            # unique-online-device path. A later retry must not silently pick another PC.
            try:
                item['device_id'] = current_browser_device_id() or ''
                if not item['device_id'] and client and (
                    not kind.startswith('ss_') or (report is None and source_file is None)
                ):
                    item['device_id'] = client._resolve_auto_device()
            except DomainError as error:
                self.store.fail(item, error.code, error.message)
                return item
            self.store.save(item)
            self._submit(item, report=report, source_file=source_file)
            return item

    def _submit(self, item, *, report=None, source_file=None):
        try:
            with browser_device_context(item.get('device_id')):
                submit_with_browser_operation_context(self.executor, self.execute, item, report, source_file)
        except RuntimeError:
            self.store.fail(item, 'EXPORT_SCHEDULING_FAILED', '下载队列正在关闭，请稍后重试')

    def execute(self, item, report=None, source_file=None):
        item.update(status='downloading', error='', error_code='')
        self.store.save(item)
        try:
            # 独立 operation ID：不能覆盖工作流分析节点的持久操作结果。
            with browser_device_context(item.get('device_id')), browser_operation_context(str(uuid4())):
                with TemporaryDirectory(prefix='keyword-api-export-') as directory:
                    target = Path(directory) / 'download.xlsx'
                    if item['kind'].startswith('ss_'):
                        if source_file is None:
                            if self.sellersprite is None:
                                raise DomainError(409, 'AGENT_EXPORT_UPDATE_REQUIRED', '采集器不支持卖家精灵接口采集')
                            name, raw, total = self.sellersprite.download(BrowserCollectionInput.model_validate(item['request']), target)
                            count = sum(len(table['rows']) for table in read_original(name, raw))
                        else:
                            name, raw, total, count = source_file
                        if count > total:
                            raise DomainError(502, 'SELLERSPRITE_SOURCE_INVALID', '采集行数超过接口总数，请重试')
                        item.update(source_total=total, source_rows=count, truncated=count < total,
                                    stop_reason='row_limit' if count < total else 'complete')
                    else:
                        if not hasattr(self.aba, 'download_original'):
                            raise DomainError(409, 'AGENT_EXPORT_UPDATE_REQUIRED', '采集器版本不支持 ABA 平台原件下载，请更新采集器')
                        platform_filename, raw = self.aba.download_original(item['request'], target)
                        validate_aba_platform_file(raw)
                        tables = read_original(platform_filename, raw)
                        name = export_name(item)
                        source_rows = sum(len(table['rows']) for table in tables)
                        item.update(
                            source_type='platform_download',
                            platform_filename=Path(platform_filename).name,
                            source_total=report.total if report is not None else source_rows,
                            source_rows=source_rows,
                            truncated=report.truncated if report is not None else False,
                            stop_reason=report.stop_reason if report is not None else 'complete',
                        )
                    stem = Path(name).stem
                    self.store.complete(item, name, raw)
                    if item['kind'].startswith('ss_') and item['status'] == 'completed':
                        file_rows = sum(len(table['rows']) for table in item['tables'])
                        if file_rows > total:
                            self.store.fail(item, 'SELLERSPRITE_SOURCE_INVALID', '采集文件行数超过接口总数，请重试')
                            return
                        item.update(source_rows=file_rows, source_imported_rows=count,
                                    truncated=file_rows < total,
                                    stop_reason='row_limit' if file_rows < total else 'complete',
                                    filename=stem + ('-接口采集-未完整.xlsx' if file_rows < total else '-接口采集.xlsx'))
                        self.store.save(item)
        except DomainError as error:
            self.store.fail(item, error.code, error.message)
        except Exception:
            import logging
            logging.getLogger(__name__).exception('source generation failed: %s', item['id'])
            message = '平台原件下载或解析失败，请检查采集器、平台登录和网络后重试' if not item['kind'].startswith('ss_') else '卖家精灵来源表下载或解析失败，请检查采集器、平台登录和网络后重试'
            self.store.fail(item, 'SOURCE_EXPORT_FAILED', message)

    def _saved_source(self, item):
        """仅复用当前研究、来源 ID、查询和周期一致的已保存采集数据。"""
        if not hasattr(self.repository, 'connect'):
            return None, None
        request = item['request']
        if request.get('query') != item['query']:
            return None, None
        with self.repository.connect() as conn:
            if item['kind'].startswith('ss_'):
                row = conn.execute(
                    'SELECT i.document,f.content FROM third_party_imports i '
                    'JOIN third_party_import_files f ON f.report_id=i.id WHERE i.id=? AND i.run_id=?',
                    (item['owner'], item['run_id']),
                ).fetchone()
                if not row:
                    return None, None
                try:
                    document = json.loads(row[0])
                    metadata = document['metadata']
                    wanted = BrowserCollectionInput.model_validate(request)
                    if (metadata.get('direction') != wanted.direction
                            or metadata.get('collection_method') != wanted.method
                            or metadata.get('source_queries') != wanted.queries
                            or metadata.get('period_kind') != 'last_30_days'
                            or metadata.get('marketplace') != 'US'
                            or not isinstance(metadata.get('source_total'), int)):
                        return None, None
                    raw = bytes(row[1])
                    name = str(document['filename'])
                    count = sum(len(table['rows']) for table in read_original(name, raw))
                    return None, (name, raw, metadata['source_total'], count)
                except (KeyError, TypeError, ValueError, DomainError):
                    return None, None

            if item['kind'] == 'step2':
                row = conn.execute('SELECT document,raw_document FROM aba_reports WHERE id=? AND run_id=?',
                                   (item['owner'], item['run_id'])).fetchone()
                if row:
                    document, raw = json.loads(row[0]), json.loads(row[1])
                    if (document.get('week_end') != request.get('week_end')
                            or document.get('query_mode') != request.get('query_mode', 'root')):
                        return None, None
                    summaries = [x for x in document.get('reports', []) if x.get('query') == item['query']]
                    records = [x for x in raw if x.get('query') == item['query']]
                    if len(summaries) == len(records) == 1:
                        report = AbaQueryReport.model_validate(summaries[0])
                        report.raw_rows = records[0].get('rows', [])
                        return report, None
                # 热门搜索任务可能尚未发布 ABA 报告，来源 ID 由 job ID 确定。
                jobs = conn.execute('SELECT id,document,raw_document FROM hot_search_collection_jobs WHERE run_id=?',
                                    (item['run_id'],)).fetchall()
                for job_id, document_json, raw_json in jobs:
                    if str(uuid5(NAMESPACE_URL, f'keyword-hot-search-report:{job_id}')) != item['owner']:
                        continue
                    document = json.loads(document_json)
                    if document.get('week_end') != request.get('week_end') or request.get('query_mode') != 'product_term':
                        return None, None
                    records = [x for x in json.loads(raw_json) if x.get('query') == item['query'] and 'report' in x]
                    if len(records) == 1:
                        report = AbaQueryReport.model_validate(records[0]['report'])
                        report.raw_rows = records[0].get('rows', [])
                        return report, None
            else:
                row = conn.execute('SELECT document,results FROM aba_expansions WHERE id=? AND run_id=?',
                                   (item['owner'], item['run_id'])).fetchone()
                if row:
                    document = json.loads(row[0])
                    direction = item['kind'].removeprefix('aba_')
                    expected_mode = 'root' if direction == 'forward' else 'asin'
                    if (document.get('week_end') != request.get('week_end')
                            or document.get('direction') != direction
                            or request.get('query_mode') != expected_mode):
                        return None, None
                    records = [x for x in json.loads(row[1]) if x.get('query') == item['query']]
                    if len(records) == 1:
                        report = AbaQueryReport.model_validate(records[0])
                        report.raw_rows = records[0].get('raw_rows', [])
                        return report, None
        return None, None

    def retry(self, project, run, export_id):
        self.repository.get_run(project, run)
        with self.lock:
            item = self.store.get(project, run, export_id)
            legacy_api_export = item.get('source_type') == 'api_generated' and not item['kind'].startswith('ss_')
            if item['status'] in ('queued', 'downloading', 'parsing') or (item['status'] == 'completed' and not legacy_api_export):
                return item
            if item.get('sha256') and item.get('source_type', 'platform_download') == 'platform_download':
                raw = self.store.file(item, True)
                self.store.complete(item, item['filename'], raw)
                return item
            if item['kind'].startswith('ss_'):
                report, source_file = self._saved_source(item)
            else:
                # ABA 重试必须再次触发紫鸟平台下载，不能复用旧接口采集行。
                report, source_file = None, None
            if report is None and source_file is None and not item.get('device_id'):
                client = getattr(self.aba, '_client', None)
                try:
                    item['device_id'] = current_browser_device_id() or (client._resolve_auto_device() if client else '')
                except DomainError as error:
                    self.store.fail(item, error.code, error.message)
                    return item
            item.update(status='queued', error='', error_code='')
            self.store.save(item)
            self._submit(item, report=report, source_file=source_file)
            return item

    def public(self, item):
        return {key: item[key] for key in ('id','kind','owner','query','status','error_code','error','filename','sha256','updated_at','source_type','source_total','source_rows','truncated','stop_reason') if key in item} | {'download_name': export_name(item)}

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)
