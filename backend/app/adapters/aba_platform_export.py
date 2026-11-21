"""通过平台页面的下载动作取得文件；不使用分页 JSON 重建原件。"""
import json
import re
import time
from contextlib import suppress
from datetime import date
from pathlib import Path
from urllib.parse import unquote, urlparse

from app.domain.errors import DomainError


def _header(headers: dict[str, str], name: str) -> str:
    """按不区分大小写的方式读取浏览器响应头。"""
    wanted = name.lower()
    return next((value for key, value in headers.items() if key.lower() == wanted), "")


def _response_filename(headers: dict[str, str], target: Path) -> str:
    """从平台响应的 Content-Disposition 提取安全的文件名。"""
    disposition = _header(headers, "content-disposition")
    encoded = re.search(r"filename\*\s*=\s*[^']*'[^']*'([^;]+)", disposition, re.I)
    quoted = re.search(r'filename\s*=\s*"([^"]+)"', disposition, re.I)
    plain = re.search(r"filename\s*=\s*([^;]+)", disposition, re.I)
    name = encoded.group(1) if encoded else quoted.group(1) if quoted else plain.group(1) if plain else target.name
    # 平台返回值不能影响目标目录；只保留文件名部分。
    return Path(unquote(name.strip())).name or target.name


def save_response_export(
    *, status: int, headers: dict[str, str], body: bytes, target: Path
) -> str | None:
    """仅把确认是 Excel/ZIP 的 ABA 下载响应保存到磁盘。

    ABA 有两种实现：部分账号直接返回 XLSX，部分账号先返回 JSON 异步任务。
    JSON（包括 202 queued）必须交给后续页面流程处理，不能误存成扩展名为 xlsx 的文件。
    """
    if not 200 <= status < 300 or not body:
        return None
    content_type = _header(headers, "content-type").split(";", 1)[0].strip().lower()
    if content_type in {"application/json", "text/json", "text/html"}:
        return None
    is_excel_type = content_type in {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
        "application/zip",
        "application/octet-stream",
    }
    # XLSX 是 ZIP 容器；魔数校验可覆盖平台未设置正确 Content-Type 的情况。
    is_zip = body[:2] == b"PK"
    if not is_excel_type and not is_zip:
        return None
    if not is_zip and content_type not in {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
    }:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return _response_filename(headers, target)


def _request_path(url: str) -> str:
    """去掉查询参数和末尾斜杠，避免平台 URL 变化导致漏拦截。"""
    return urlparse(url).path.rstrip("/")


def export_filters(request):
    try:
        day = date.fromisoformat(request['week_end'])
    except (KeyError, TypeError, ValueError):
        raise DomainError(422, 'ABA_EXPORT_FILTER_INVALID', 'ABA下载的周结束日格式不正确') from None
    if day.weekday() != 5 or request.get('country', 'us') != 'us' or not request.get('query', '').strip():
        raise DomainError(422, 'ABA_EXPORT_FILTER_INVALID', 'ABA下载必须指定美国站、查询条件和周六结束日')
    return [
        {'id': 'reporting-range', 'value': 'weekly', 'valueType': None},
        {'id': 'weekly-week', 'value': request['week_end'], 'valueType': 'weekly'},
        {'id': 'asins' if request.get('query_mode') == 'asin' else 'search-term-freeform',
         'value': request['query'], 'valueType': None},
    ]


def download_original(session, request: dict, target: Path):
    filters = export_filters(request)
    if session is None:
        raise DomainError(503, 'ABA_EXPORT_BROWSER_REQUIRED', '平台原件下载需要已登录的紫鸟采集器')
    from playwright.sync_api import TimeoutError as BrowserTimeout
    with session.page() as (context, existing):
        page = context.new_page()
        observed = {'report': False, 'download': False}
        def bind_query(route):
            req = route.request
            request_path = _request_path(req.url)
            if not request_path.endswith(('/reports', '/download', '/download/metadata')):
                return route.continue_()
            # 下载动作在部分页面版本是 GET，不能改写其请求体，但仍要纳入本次任务。
            if req.method != 'POST':
                if request_path.endswith('/download'):
                    observed['download'] = True
                return route.continue_()
            try:
                body = req.post_data_json
            except Exception:
                return route.abort()
            if not isinstance(body, dict):
                return route.abort()
            # 独占新标签页，所有查询/下载严格绑定同一周和同一词/ASIN集合。
            # 保留页面生成的下载格式、下载类型等协议字段，不猜测平台下载接口结构。
            body.update(filterSelections=filters, selectedCountries=['us'])
            if request_path.endswith('/reports'):
                observed['report'] = True
            elif request_path.endswith('/download') and not request_path.endswith('/metadata'):
                observed['download'] = True
            route.continue_(post_data=json.dumps(body))
        page.route('**/api/brand-analytics/v1/dashboard/top-search-terms/**', bind_query)
        try:
            url = existing.url if '/brand-analytics/' in existing.url and 'top-search-terms' in existing.url else 'https://sellercentral.amazon.com/brand-analytics/dashboard/top-search-terms'
            page.goto(url, wait_until='domcontentloaded', timeout=45000)
            if '/ap/' in page.url:
                raise DomainError(503, 'ABA_AUTH_REQUIRED', '请在紫鸟登录卖家后台后重试下载')
            button = page.get_by_role('button', name=re.compile(r'^\s*(下载|下载报告|Download|Download report)\s*$', re.I))
            button.first.wait_for(timeout=45000)
            if not observed['report']:
                raise DomainError(409, 'ABA_EXPORT_QUERY_UNVERIFIED', '未观察到已绑定报告周的查询请求，已停止下载以免混入其他查询')
            # 有些 ABA 账号点击后由浏览器直接触发 download，有些账号返回
            # /download 的 XLSX 响应。监听两种事件，避免只等原生 download 导致无谓超时。
            downloads = []
            responses = []
            def on_download(download):
                downloads.append(download)
            def on_response(response):
                if _request_path(response.url).endswith('/download'):
                    responses.append(response)
            page.on('download', on_download)
            page.on('response', on_response)
            try:
                button.first.click(timeout=15000)
                dialog = page.get_by_role('dialog')
                # 平台确认弹窗可能延迟挂载；监听器已先挂载，避免漏掉快速响应。
                with suppress(BrowserTimeout):
                    dialog.first.wait_for(state='visible', timeout=3000)
                if dialog.count() and dialog.first.is_visible():
                    # 有的页面先要求确认下载。只操作当前新建导出，不进入历史任务下载。
                    confirm = dialog.get_by_role('button', name=re.compile(r'^(下载|生成下载|生成报告|Download|Generate download|Generate report)$', re.I))
                    if confirm.count():
                        confirm.first.click(timeout=15000)

                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    if not observed['download']:
                        # 下载请求尚未经过绑定路由时，不接收页面上其他来源的文件。
                        downloads.clear()
                        responses.clear()
                    if observed['download']:
                        while responses:
                            response = responses.pop(0)
                            try:
                                filename = save_response_export(
                                    status=response.status,
                                    headers=response.headers,
                                    body=response.body(),
                                    target=target,
                                )
                            except Exception:
                                # 响应可能仍在流式传输，继续等待原生下载或后续响应。
                                filename = None
                            if filename:
                                return filename, target.read_bytes()
                        if downloads:
                            download = downloads.pop(0)
                            download.save_as(target)
                            if download.failure() or not target.is_file():
                                raise DomainError(502, 'ABA_PLATFORM_DOWNLOAD_FAILED', 'ABA平台原件下载失败')
                            return download.suggested_filename, target.read_bytes()
                    page.wait_for_timeout(250)
                raise BrowserTimeout('ABA download did not produce a file')
            finally:
                page.remove_listener('download', on_download)
                page.remove_listener('response', on_response)
        except BrowserTimeout:
            raise DomainError(504, 'ABA_PLATFORM_DOWNLOAD_TIMEOUT', 'ABA平台在2分钟内未返回文件，请检查下载弹窗、报告生成状态或登录后重试') from None
        finally:
            page.close()


