"""布卷疵点复检与退修系统 —— HTTP 服务。

单条本地启动命令：

    python3 app.py                # 初始化演示数据并启动（http://localhost:8000）
    python3 app.py --demo         # 额外自动跑通一条“初检→复检→放行”样例
    python3 app.py --reset        # 清空数据库与照片后重新播种

仅使用 Python 标准库；SQLite 存业务数据，photos/ 存疵点照片。
后台守护线程定时扫描复检超时任务并生成提醒。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import db as store
from db import Database, DomainError, SEVERITY, RELEASE_RULES, DEFECT_HANDLING

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, 'static')
DB_PATH = os.path.join(BASE_DIR, 'textile_qc.db')
PHOTO_DIR = os.path.join(BASE_DIR, 'photos')

OVERDUE_SCAN_SEC = 15  # 进程内复检提醒扫描周期

_database: Database | None = None


def get_db() -> Database:
    global _database
    if _database is None:
        _database = Database(DB_PATH, PHOTO_DIR)
    return _database


# ---- 演示流程 ---------------------------------------------------------------

def _demo_photo(d: Database, name: str, label: str) -> str:
    path = os.path.join(d.photo_dir, name)
    if not os.path.exists(path):
        svg = f"""<svg xmlns='http://www.w3.org/2000/svg' width='320' height='240'>
<rect width='320' height='240' fill='#e8ddc7'/>
<g stroke='#c9bda2' stroke-width='1'>
{''.join(f"<line x1='{x}' y1='0' x2='{x}' y2='240'/>" for x in range(0, 321, 12))}
{''.join(f"<line x1='0' y1='{y}' x2='320' y2='{y}'/>" for y in range(0, 241, 12))}
</g>
<rect x='60' y='80' width='200' height='70' rx='6' fill='none' stroke='#9a4b32'
      stroke-width='4' stroke-dasharray='8 6'/>
<text x='160' y='122' font-size='18' text-anchor='middle' fill='#3f4a52'
      font-family='sans-serif'>{label}</text>
</svg>"""
        with open(path, 'w', encoding='utf-8') as f:
            f.write(svg)
    return name


def run_demo_flow(d: Database) -> dict:
    """创建一条全新布卷并走通：初检录入 → 复检接收 → 复检提交 → 放行。"""
    summary: dict = {}
    roll_no = f"BR-DEMO-{time.strftime('%H%M%S')}"
    rid = d.create_roll(roll_no, 105.0, 1.50, 'CB-A21', 'LM-101', min_length_m=90)
    summary['roll_id'] = rid
    summary['roll_no'] = roll_no

    version_id = d.begin_inspection(rid, inspector='王检')
    seg_id = d.add_segment(version_id, 0, 105, '王检')
    p1 = _demo_photo(d, f'demo_{rid}_stain.svg', f'{roll_no} 污渍 22m')
    p2 = _demo_photo(d, f'demo_{rid}_slub.svg', f'{roll_no} 竹节 70m')
    defect_stain = d.add_defect(seg_id, 'STAIN', 22.0, 0.4, 'B', '王检', p1,
                                '演示：边部污渍')
    defect_slub = d.add_defect(seg_id, 'SLUB', 70.0, 0.1, 'C', '王检', p2,
                               '演示：轻微竹节')
    d.complete_initial(rid)
    q = [t for t in d.review_queue() if t['roll_id'] == rid]
    task_id = q[0]['id']
    summary['initial'] = {'version_id': version_id, 'segment_id': seg_id,
                          'defects': [defect_stain, defect_slub]}

    # 另一条产线先抢，演示竞争失败；再由合法复检人成功接收
    d.claim_task(task_id, line='复检二线', reviewer='赵复检')
    summary['claim'] = {'task_id': task_id, 'line': '复检二线', 'reviewer': '赵复检'}

    d.submit_review(task_id, [
        {'defect_id': defect_stain, 'action': '确认', 'photo_file': p1,
         'note': '污渍可接受，已复检确认'},
        {'defect_id': defect_slub, 'action': '确认', 'photo_file': p2,
         'note': 'C 级轻微，确认接收'},
    ], disposition='直接放行')

    result = d.release(rid, reviewer='赵复检')
    summary['release'] = result
    return summary


# ---- HTTP 处理 --------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = 'TextileQC/1.0'

    def log_message(self, fmt, *args):  # 精简日志
        sys.stderr.write("[%s] %s\n" % (time.strftime('%H:%M:%S'), fmt % args))

    # --- 工具 ---
    def _send_json(self, obj, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        n = int(self.headers.get('Content-Length') or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode('utf-8'))
        except json.JSONDecodeError:
            raise DomainError('请求体不是合法 JSON')

    def _user(self) -> str:
        # X-User 头优先（fetch 可用）；查询参数 ?user= 用于 <img>/简单客户端
        qs = parse_qs(urlparse(self.path).query)
        return self.headers.get('X-User', '').strip() or \
            (qs['user'][0].strip() if qs.get('user') else '')

    def _serve_file(self, path: str, content_type: str):
        try:
            with open(path, 'rb') as f:
                body = f.read()
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND, '文件不存在')
            return
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --- 路由 ---
    def do_GET(self):
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)
        try:
            if path == '/' or path == '/index.html':
                return self._serve_file(os.path.join(STATIC_DIR, 'index.html'),
                                        'text/html; charset=utf-8')
            if path.startswith('/static/'):
                return self._static(path[len('/static/'):])
            if path == '/api/rolls':
                return self._send_json(get_db().list_rolls())
            if path == '/api/queue':
                return self._send_json(get_db().review_queue())
            if path == '/api/rework':
                return self._send_json(get_db().rework_progress())
            if path == '/api/releases':
                return self._send_json(get_db().releases())
            if path == '/api/map':
                return self._send_json(get_db().defects_map())
            if path == '/api/reminders':
                return self._send_json(get_db().list_reminders())
            if path == '/api/meta':
                return self._send_json({
                    'severity': SEVERITY,
                    'release_rules': RELEASE_RULES,
                    'defect_handling': DEFECT_HANDLING,
                    'roll_statuses': store.ROLL_STATUSES,
                    'defect_types': get_db().rows_to_dicts(get_db().query(
                        'SELECT * FROM defect_type ORDER BY id')),
                    'looms': get_db().rows_to_dicts(get_db().query(
                        'SELECT * FROM loom ORDER BY code')),
                    'color_batches': get_db().rows_to_dicts(get_db().query(
                        'SELECT * FROM color_batch ORDER BY code')),
                })
            if path.startswith('/api/rolls/'):
                rid = self._int(path.rsplit('/', 1)[-1])
                return self._send_json(get_db().roll_detail(rid))
            if path.startswith('/api/photos/'):
                return self._photo(path.rsplit('/', 1)[-1], qs)
            self.send_error(HTTPStatus.NOT_FOUND, '未知路径')
        except DomainError as e:
            self._send_json({'error': str(e)}, 409)
        except Exception as e:  # noqa: BLE001
            self._send_json({'error': f'服务器错误: {e}'}, 500)

    def do_POST(self):
        path = urlparse(self.path).path.rstrip('/')
        body = self._read_json()
        d = get_db()
        try:
            if path == '/api/rolls':
                rid = d.create_roll(body['roll_no'], float(body['length_m']),
                                    float(body['width_m']), body['color_batch_code'],
                                    body['loom_code'], float(body.get('min_length_m') or 0))
                return self._send_json({'roll_id': rid}, 201)
            if path == '/api/inspections/begin':
                vid = d.begin_inspection(int(body['roll_id']), body['inspector'].strip())
                return self._send_json({'version_id': vid})
            if path == '/api/segments':
                sid = d.add_segment(int(body['version_id']), float(body['start_m']),
                                    float(body['end_m']), body['inspector'].strip())
                return self._send_json({'segment_id': sid})
            if path == '/api/defects':
                photo_file = self._save_photo(body) if body.get('photo_data_base64') \
                    else (body.get('photo_file') or None)
                did = d.add_defect(int(body['segment_id']), body['type_code'],
                                   float(body['pos_m']), float(body.get('length_m') or 0),
                                   body['grade'], body['inspector'].strip(),
                                   photo_file, body.get('note'))
                return self._send_json({'defect_id': did, 'photo_file': photo_file}, 201)
            if path == '/api/inspections/complete':
                d.complete_initial(int(body['roll_id']))
                return self._send_json({'ok': True})
            if path.startswith('/api/tasks/') and path.endswith('/claim'):
                tid = self._int(path.split('/')[3])
                return self._send_json(d.claim_task(tid, body['line'].strip(),
                                                    body['reviewer'].strip()))
            if path.startswith('/api/tasks/') and path.endswith('/submit'):
                tid = self._int(path.split('/')[3])
                return self._send_json(d.submit_review(
                    tid, body.get('decisions', []), body['disposition']))
            if path == '/api/tasks/recreate':
                tid = d.recreate_task_after_timeout(int(body['roll_id']))
                return self._send_json({'task_id': tid})
            if path.startswith('/api/rework/') and path.endswith('/start'):
                oid = self._int(path.split('/')[3])
                d.start_rework(oid, body['assignee'].strip())
                return self._send_json({'ok': True})
            if path.startswith('/api/rework/') and path.endswith('/complete'):
                oid = self._int(path.split('/')[3])
                for item in body.get('results', []):
                    if item.get('photo_data_base64'):
                        item['photo_file'] = self._save_inline_photo(
                            item.pop('photo_data_base64'),
                            item.pop('photo_filename', 'rework.svg'))
                vno = d.complete_rework(oid, body['results'], body['inspector'].strip())
                return self._send_json({'new_version': vno})
            if path.startswith('/api/rolls/') and path.endswith('/release'):
                rid = self._int(path.split('/')[3])
                return self._send_json(d.release(rid, body['reviewer'].strip()))
            self.send_error(HTTPStatus.NOT_FOUND, '未知路径')
        except DomainError as e:
            self._send_json({'error': str(e)}, 409)
        except KeyError as e:
            self._send_json({'error': f'缺少字段: {e}'}, 400)
        except Exception as e:  # noqa: BLE001
            self._send_json({'error': f'服务器错误: {e}'}, 500)

    # --- 照片 ---
    def _save_photo(self, body: dict) -> str:
        data = base64.b64decode(body['photo_data_base64'])
        fname = body.get('photo_filename') or 'photo.svg'
        return self._write_photo_bytes(fname, data)

    def _save_inline_photo(self, b64: str, fname: str) -> str:
        return self._write_photo_bytes(fname, base64.b64decode(b64))

    def _write_photo_bytes(self, fname: str, data: bytes) -> str:
        # 仅保留文件基名，防路径穿越
        fname = os.path.basename(fname).replace('..', '')
        stem, ext = os.path.splitext(fname)
        ext = (ext or '.svg').lower()
        if ext not in ('.svg', '.png', '.jpg', '.jpeg', '.gif', '.webp'):
            raise DomainError('不支持的照片格式')
        name = f'up{int(time.time()*1000)}_{stem}{ext}'
        with open(os.path.join(get_db().photo_dir, name), 'wb') as f:
            f.write(data)
        return name

    def _photo(self, file_name: str, qs: dict):
        # 照片以 <img>/blob 方式访问、无法自定义请求头，身份用查询参数 user 传递；
        # JSON 接口仍可使用 X-User 头。
        user = self._user() or (qs['user'][0].strip() if qs.get('user') else '')
        roll_id = int(qs['roll_id'][0]) if qs.get('roll_id') else None
        full = get_db().photo_path_for(file_name, user, roll_id)
        ext = os.path.splitext(full)[1].lower()
        ctype = {'.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg',
                 '.jpeg': 'image/jpeg', '.gif': 'image/gif',
                 '.webp': 'image/webp'}.get(ext, 'application/octet-stream')
        self._serve_file(full, ctype)

    def _static(self, rel: str):
        rel = rel.lstrip('/')
        if '..' in rel.split('/'):
            self.send_error(HTTPStatus.FORBIDDEN, '非法路径')
            return
        ctypes = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
                  '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml'}
        ext = os.path.splitext(rel)[1].lower()
        self._serve_file(os.path.join(STATIC_DIR, rel), ctypes.get(ext, 'application/octet-stream'))

    @staticmethod
    def _int(s: str) -> int:
        return int(s)


# ---- 后台复检提醒 -----------------------------------------------------------

def start_overdue_scanner(d: Database) -> threading.Thread:
    def loop():
        while True:
            try:
                hits = d.scan_overdue()
                for h in hits:
                    sys.stderr.write(f"[提醒] {h['message']}\n")
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"[扫描器] {e}\n")
            time.sleep(OVERDUE_SCAN_SEC)
    t = threading.Thread(target=loop, daemon=True, name='overdue-scanner')
    t.start()
    return t


def main():
    ap = argparse.ArgumentParser(description='布卷疵点复检与退修系统')
    ap.add_argument('--port', type=int, default=8000)
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--reset', action='store_true', help='清空数据库与照片后重新播种')
    ap.add_argument('--demo', action='store_true',
                    help='启动前自动创建一条 初检→复检→放行 样例')
    args = ap.parse_args()

    if args.reset and os.path.exists(DB_PATH):
        for suffix in ('', '-wal', '-shm'):
            p = DB_PATH + suffix
            if os.path.exists(p):
                os.remove(p)
        if os.path.isdir(PHOTO_DIR):
            for f in os.listdir(PHOTO_DIR):
                os.remove(os.path.join(PHOTO_DIR, f))

    d = Database(DB_PATH, PHOTO_DIR)
    seeded = d.seed()
    if seeded.get('skipped'):
        print('· 数据库已有数据，跳过播种')
    else:
        print('· 已写入演示主数据与 5 个布卷（含待复检、超时、退修放行、已放行样例）')

    if args.demo:
        s = run_demo_flow(d)
        print('· 一键样例已创建：')
        print(json.dumps(s, ensure_ascii=False, indent=2))

    start_overdue_scanner(d)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f'· 服务已启动：http://localhost:{args.port}  （Ctrl+C 停止）')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n· 已停止')


if __name__ == '__main__':
    main()
