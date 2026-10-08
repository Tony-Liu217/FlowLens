"""Same-origin HTTP transport for the local application."""
import json
import secrets
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, unquote
from .service import App, safe_filename
from jiaowopay_review.store import Conflict, ReviewError
from jiaowopay_review.bookstore import export_book
STATIC = Path(__file__).resolve().parents[2] / 'frontend'
MAX_REQUEST = 70 * 1024 * 1024

class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, app, port=0):
        self.app = app
        super().__init__(('127.0.0.1', port), Handler)
        self.origin = f'http://127.0.0.1:{self.server_port}'


class Handler(BaseHTTPRequestHandler):
    server_version = 'JiaowoPayLocal/1'

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *args):
        pass  # No request URLs, auth tokens or financial content in access logs.

    def send(self, status, data, content_type='application/json; charset=utf-8', download=False):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        if download:
            self.send_header('Content-Disposition', 'attachment; filename="effective-records.json"')
        self.end_headers()
        self.wfile.write(data)
        # Windows may discard an error response if a socket closes with unread
        # upload bytes. Flush the response, then drain a bounded amount briefly.
        if status >= 400 and getattr(self, 'unread_body', 0):
            self.wfile.flush()
            try:
                self.connection.shutdown(socket.SHUT_WR)
                self.connection.settimeout(.2)
                self.rfile.read(min(self.unread_body, 1024 * 1024))
            except OSError:
                pass
            self.close_connection = True
            self.unread_body = 0

    def guard(self, api):
        if self.headers.get('Host') != urlsplit(self.server.origin).netloc:
            self.send(403, {'error': '仅允许本机地址访问。'})
            return False
        origin = self.headers.get('Origin')
        if origin and origin != self.server.origin:
            self.send(403, {'error': '拒绝跨站请求。'})
            return False
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            self.send(403, {'error': '拒绝跨站请求。'})
            return False
        auth = self.headers.get('Authorization', '')
        if api and not secrets.compare_digest(auth, 'Bearer ' + self.server.app.token):
            self.send(401, {'error': '本地访问凭证缺失或失效，请使用启动时显示的完整链接。'})
            return False
        return True

    def do_GET(self):
        url = urlsplit(self.path)
        if not self.guard(url.path.startswith('/api/')):
            return
        try:
            if not url.path.startswith('/api/'):
                relative = 'index.html' if url.path == '/' else unquote(url.path).lstrip('/')
                path = (STATIC / relative).resolve()
                if not path.is_relative_to(STATIC.resolve()) or not path.is_file() or path.suffix not in {'.html', '.js', '.css', '.png', '.jpg'}:
                    self.send(404, {'error': '路径不存在。'})
                    return
                mime = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.png': 'image/png', '.jpg': 'image/jpeg'}[path.suffix]
                self.send(200, path.read_bytes(), mime)
                return
            params = parse_qs(url.query)
            with self.server.app.lock:
                app = self.server.app
                if url.path == '/api/state':
                    self.send(200, app.snapshot())
                elif url.path in {'/api/overview', '/api/records'}:
                    app.catalog.check({'book_id': params.get('book_id', [''])[0] or None, 'catalog_revision': int(params.get('catalog_revision', ['-1'])[0])})
                    self.send(200, app.overview() if url.path == '/api/overview' else app.records(params))
                elif url.path in {'/api/detail', '/api/evidence', '/api/export'}:
                    app.catalog.check({'book_id':params.get('book_id',[''])[0] or None,
                                       'catalog_revision':int(params.get('catalog_revision',['-1'])[0])})
                    store = app.current(params.get('workspace_id', [''])[0],params.get('batch_id',[None])[0]) if url.path != '/api/export' else None
                    if url.path == '/api/detail':
                        self.send(200, store.detail(params.get('record_id', [''])[0]))
                    elif url.path == '/api/evidence':
                        path = store.evidence(params.get('path', [''])[0])
                        if path.suffix != '.png':
                            raise ReviewError('界面仅开放已登记的 PNG 原图。')
                        self.send(200, path.read_bytes(), 'image/png')
                    else:
                        partial = params.get('allow_partial', ['false'])[0] == 'true'
                        result = export_book(app.catalog,app.catalog.context()['book_id'],allow_partial=partial)
                        self.send(200, result, download=True)
                else:
                    self.send(404, {'error': '路径不存在。'})
        except Conflict as exc:
            self.send(409, {'error': str(exc)})
        except (ReviewError, ValueError) as exc:
            self.send(400, {'error': str(exc)})
        except Exception:
            self.send(500, {'error': '读取未完成；请保留原文件并检查本地数据包。'})

    def do_POST(self):
        try:
            self.unread_body = max(0, int(self.headers.get('Content-Length', '0')))
        except ValueError:
            self.unread_body = 0
        if not self.guard(True):
            return
        try:
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise ReviewError('仅接受 JSON 请求。')
            if self.headers.get('Transfer-Encoding'):
                raise ReviewError('不支持分块请求。')
            size = int(self.headers.get('Content-Length', '0'))
            limit = MAX_REQUEST if self.path == '/api/import' else 64 * 1024
            if not 0 < size <= limit:
                raise ReviewError('请求大小超过限制或内容为空。')
            data = self.rfile.read(size)
            self.unread_body = 0
            if len(data) != size:
                raise ReviewError('请求传输不完整。')
            payload = json.loads(data)
            if self.path == '/api/import':
                result = self.server.app.start_import(payload)
                self.send(202, result)
            elif self.path == '/api/books':
                self.send(200,self.server.app.manage(payload))
            elif self.path == '/api/action':
                result = self.server.app.apply(payload)
                self.send(200, result)
            else:
                self.send(404, {'error': '路径不存在。'})
        except Conflict as exc:
            self.send(409, {'error': str(exc)})
        except (ReviewError, ValueError, TypeError) as exc:
            self.send(400, {'error': str(exc) if isinstance(exc, ReviewError) else '请求格式无效。'})
        except Exception:
            self.send(500, {'error': '操作未完成，请刷新核对状态；不要重复猜测提交。'})


