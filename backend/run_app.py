"""Double-click launcher entry point; all application paths are self-contained."""
import argparse
import json
from pathlib import Path
import sys
import threading
import webbrowser

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.server import App, Server
from jiaowopay_review.lock import DataDirectoryLock


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description='FlowLens 本地账单导入与核验')
    parser.add_argument('--data-dir', default=str(root / '.local-data/workspace'))
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--no-ocr', action='store_true')
    parser.add_argument('--ocr-python')
    parser.add_argument('--open-browser', action='store_true')
    parser.add_argument('--connection-file', help='Local test harness only; contains temporary access token')
    args = parser.parse_args()
    lock = DataDirectoryLock(args.data_dir)
    try:
        app = App(args.data_dir, ocr=not args.no_ocr, ocr_python=args.ocr_python)
        server = Server(app, args.port)
        url = server.origin + '/#token=' + app.token
        print('FlowLens 已启动：本机导入、核验与持久保存。关闭窗口后服务停止。', flush=True)
        print(url, flush=True)
        if args.connection_file:
            Path(args.connection_file).write_text(json.dumps({'url': url, 'origin': server.origin, 'token': app.token}), encoding='utf-8')
        if args.open_browser:
            def open_page():
                if not webbrowser.open(url):
                    print('浏览器未打开，请复制上方完整链接。', flush=True)
            threading.Thread(target=open_page, daemon=True).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        lock.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        from jiaowopay_review.store import ReviewError
        print(str(exc) if isinstance(exc, ReviewError) else '启动失败：请检查运行环境、端口和数据目录访问权限。', file=sys.stderr)
        sys.exit(1)
