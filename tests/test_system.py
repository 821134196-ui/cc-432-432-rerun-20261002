"""系统测试：位置区间、版本关联、接收竞争、放行条件、照片权限、超时提醒。

运行：python3 -m unittest discover -s tests -v
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import quote as urlquote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as store
from db import Database, DomainError
import app


class QCBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='qc_test_')
        self.db = Database(os.path.join(self.dir, 't.db'),
                           os.path.join(self.dir, 'photos'))
        self.ids = self.db.seed()

    def tearDown(self):
        self.db.conn.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    # -- 工厂：快速造一卷带初检疵点的布卷 --
    def make_rolled_defect(self, type_code='STAIN', grade='B', pos=20.0,
                           length=100.0, min_len=0, with_photo=True):
        n = len(self.db.query('SELECT id FROM fabric_roll')) + 1
        no = f'BR-T{n:03d}-{time.time_ns() % 100000}'
        rid = self.db.create_roll(no, length, 1.5, 'CB-A21', 'LM-101', min_len)
        vid = self.db.begin_inspection(rid, '王检')
        sid = self.db.add_segment(vid, 0, length, '王检')
        photo = f'{no}.svg' if with_photo else None
        if with_photo:
            with open(os.path.join(self.db.photo_dir, photo), 'w') as f:
                f.write('<svg></svg>')
        did = self.db.add_defect(sid, type_code, pos, 0.3, grade, '王检',
                                 photo, '测试疵点')
        self.db.complete_initial(rid)
        task = [t for t in self.db.review_queue() if t['roll_id'] == rid][0]
        return rid, sid, did, task['id']


class TestPositionRanges(QCBase):
    def test_segment_within_roll_length(self):
        rid = self.db.create_roll('BR-RANGE-1', 100, 1.5, 'CB-A21', 'LM-101')
        vid = self.db.begin_inspection(rid, '王检')
        self.db.add_segment(vid, 0, 50, '王检')
        with self.assertRaises(DomainError):  # 超出布卷长度
            self.db.add_segment(vid, 50, 120, '王检')
        with self.assertRaises(DomainError):  # 与已有区间重叠
            self.db.add_segment(vid, 40, 80, '王检')
        with self.assertRaises(DomainError):  # 反向区间
            self.db.add_segment(vid, 80, 70, '王检')
        # 紧邻区间允许
        sid2 = self.db.add_segment(vid, 50, 100, '王检')
        self.assertGreater(sid2, 0)

    def test_defect_position_inside_segment_and_roll(self):
        rid = self.db.create_roll('BR-RANGE-2', 60, 1.5, 'CB-A22', 'LM-102')
        vid = self.db.begin_inspection(rid, '李检')
        sid = self.db.add_segment(vid, 10, 40, '李检')
        with self.assertRaises(DomainError):  # 早于段起点
            self.db.add_defect(sid, 'STAIN', 5, 0.2, 'B', '李检', None, '')
        with self.assertRaises(DomainError):  # 超出布卷总长
            self.db.add_defect(sid, 'STAIN', 65, 0.2, 'B', '李检', None, '')
        did = self.db.add_defect(sid, 'STAIN', 39.9, 0.2, 'B', '李检', None, '')
        self.assertGreater(did, 0)

    def test_segment_only_on_initial_version(self):
        rid = self.ids['roll4']  # 已存在 v2 退修版本
        v2 = [v for v in self.db.roll_detail(rid)['versions']
              if v['version_no'] == 2][0]['id']
        with self.assertRaises(DomainError):
            self.db.add_segment(v2, 0, 10, '钱修布')


class TestVersionRelation(QCBase):
    def test_rework_generates_new_version_referencing_initial_segment(self):
        rid, sid, did, tid = self.make_rolled_defect('END_DOWN', 'A', pos=30.0)
        self.db.claim_task(tid, '复检一线', '赵复检')
        r = self.db.submit_review(tid, [
            {'defect_id': did, 'action': '转退修'}], '转退修')
        oid = r['rework_order_id']
        self.db.start_rework(oid, '钱修布')
        fixed = os.path.join(self.db.photo_dir, 'fixed.svg')
        with open(fixed, 'w') as f:
            f.write('<svg></svg>')
        vno = self.db.complete_rework(oid, [
            {'defect_id': did, 'action': '修复', 'photo_file': 'fixed.svg',
             'note': '接头修复'}], '钱修布')
        self.assertEqual(vno, 2)
        detail = self.db.roll_detail(rid)
        self.assertEqual(detail['current_version'], 2)
        self.assertEqual([v['kind'] for v in detail['versions']],
                         ['initial', 'rework'])
        v2_defects = [d for d in detail['defects'] if d['version_no'] == 2]
        self.assertEqual(len(v2_defects), 1)
        # 复检/修复记录必须引用原检验段
        self.assertEqual(v2_defects[0]['source_segment_id'], sid)
        self.assertEqual(v2_defects[0]['pos_m'], 30.0)  # 米数定位随版本快照保留
        initial_d = [d for d in detail['defects'] if d['id'] == did][0]
        self.assertEqual(initial_d['status'], '已修复')
        # v2 放行成功
        out = self.db.release(rid, '赵复检')
        self.assertIn('已修复', out['basis'])

    def test_review_only_references_initial_snapshot(self):
        rid = self.ids['roll4']
        detail = self.db.roll_detail(rid)
        v2_defect = [d for d in detail['defects'] if d['version_no'] == 2][0]
        # 构造一个 open 任务，拿 v2 缺陷提交必须被拒（复检只能引用初检段）
        rid2, sid2, did2, tid2 = self.make_rolled_defect('STAIN', 'B')
        self.db.claim_task(tid2, '复检一线', '赵复检')
        with self.assertRaises(DomainError):
            self.db.submit_review(tid2, [
                {'defect_id': v2_defect['id'], 'action': '确认',
                 'photo_file': 'x.svg'}], '直接放行')

    def test_seed_version_chain(self):
        d = self.db.roll_detail(self.ids['roll4'])
        self.assertEqual([v['version_no'] for v in d['versions']], [1, 2])
        v2 = [x for x in d['defects'] if x['version_no'] == 2][0]
        s1 = [s for s in d['segments'] if s['version_id'] ==
              [v for v in d['versions'] if v['version_no'] == 1][0]['id']]
        self.assertIn(v2['source_segment_id'], [s['id'] for s in s1])


class TestClaimCompetition(QCBase):
    def test_two_lines_concurrent_claim_only_one_wins(self):
        rid, sid, did, tid = self.make_rolled_defect('STAIN', 'B')
        barrier = threading.Barrier(2)
        results = []

        def claim(line, reviewer):
            barrier.wait()
            try:
                self.db.claim_task(tid, line, reviewer)
                results.append(('ok', line))
            except DomainError:
                results.append(('fail', line))

        t1 = threading.Thread(target=claim, args=('复检一线', '赵复检'))
        t2 = threading.Thread(target=claim, args=('复检二线', '周复检'))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(len(results), 2)
        self.assertEqual(sorted(r[0] for r in results), ['fail', 'ok'])
        row = self.db.one('SELECT * FROM review_task WHERE id=?', (tid,))
        self.assertEqual(row['status'], '进行中')
        self.assertIn(row['line'], ('复检一线', '复检二线'))

    def test_reviewer_must_differ_from_initial_inspector(self):
        rid, sid, did, tid = self.make_rolled_defect('STAIN', 'B')
        with self.assertRaises(DomainError):
            self.db.claim_task(tid, '复检一线', '王检')  # 王检是初检人

    def test_open_task_unique_per_roll(self):
        # 数据库层保证同一布卷至多一条 open 任务
        rid = self.ids['roll1']
        with self.assertRaises(Exception):
            self.db.conn.execute("""
                INSERT INTO review_task(roll_id, version_no, status, created_at, due_at)
                VALUES (?,1,'待接收',?,?)""",
                                  (rid, store.iso(), store.iso())).fetchall()

    def test_timeout_then_reassign(self):
        rid, sid, did, tid = self.make_rolled_defect('STAIN', 'B')
        # 先手工制造超时，再走重新下发
        self.db.conn.execute("UPDATE review_task SET status='超时' WHERE id=?", (tid,))
        self.db.conn.commit()
        nid = self.db.recreate_task_after_timeout(rid)
        self.assertNotEqual(nid, tid)
        row = self.db.one('SELECT status FROM review_task WHERE id=?', (nid,))
        self.assertEqual(row['status'], '待接收')
        self.db.claim_task(nid, '复检三线', '周复检')

    def test_scan_overdue_creates_reminder(self):
        rid, sid, did, tid = self.make_rolled_defect('STAIN', 'B')
        self.db.conn.execute(
            "UPDATE review_task SET due_at=? WHERE id=?",
            (store.iso(time.time() - 1), tid))
        self.db.conn.commit()
        hits = self.db.scan_overdue()
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['task_id'], tid)
        rem = self.db.list_reminders()
        self.assertTrue(any(m['ref_id'] == tid for m in rem))
        # 幂等：再扫一次不重复
        self.assertEqual(self.db.scan_overdue(), [])


class TestReleaseConditions(QCBase):
    def _review_confirm(self, rid, did, tid, reviewer='赵复检'):
        self.db.claim_task(tid, '复检一线', reviewer)
        detail = self.db.roll_detail(rid)
        photo = [d for d in detail['defects'] if d['id'] == did][0]['photo_file']
        self.db.submit_review(tid, [
            {'defect_id': did, 'action': '确认', 'photo_file': photo}],
            '直接放行')

    def test_a_grade_confirmed_cannot_release(self):
        rid, sid, did, tid = self.make_rolled_defect('END_DOWN', 'A')
        self._review_confirm(rid, did, tid)
        with self.assertRaises(DomainError) as cm:
            self.db.release(rid, '赵复检')
        self.assertIn('A 级', str(cm.exception))

    def test_end_down_must_be_reworked(self):
        rid, sid, did, tid = self.make_rolled_defect('END_DOWN', 'A')
        # 即便复检确认，断经也不放行
        self._review_confirm(rid, did, tid)
        with self.assertRaises(DomainError) as cm:
            self.db.release(rid, '赵复检')
        self.assertIn('断经', str(cm.exception))

    def test_missing_photo_blocks_confirmation(self):
        rid, sid, did, tid = self.make_rolled_defect('COLOR_DIFF', 'B',
                                                      with_photo=False)
        self.db.claim_task(tid, '复检一线', '赵复检')
        with self.assertRaises(DomainError) as cm:
            self.db.submit_review(tid, [
                {'defect_id': did, 'action': '确认'}], '直接放行')
        self.assertIn('照片缺失', str(cm.exception))
        # 补拍后可以确认并放行（B 级）
        with open(os.path.join(self.db.photo_dir, 'p.svg'), 'w') as f:
            f.write('<svg></svg>')
        self.db.submit_review(tid, [
            {'defect_id': did, 'action': '确认', 'photo_file': 'p.svg'}],
            '直接放行')
        out = self.db.release(rid, '赵复检')
        self.assertTrue(out['release_id'])

    def test_short_length_below_minimum_blocks_release(self):
        rid, sid, did, tid = self.make_rolled_defect(
            'SHORT_LEN', 'B', pos=95, length=100, min_len=90)
        self.db.claim_task(tid, '复检一线', '赵复检')
        self.db.submit_review(tid, [
            {'defect_id': did, 'action': '转退修'}], '转退修')
        oid = [o for o in self.db.rework_progress() if o['roll_id'] == rid][0]['id']
        self.db.start_rework(oid, '钱修布')
        with open(os.path.join(self.db.photo_dir, 's.svg'), 'w') as f:
            f.write('<svg></svg>')
        # 核减到 80m，低于订单最小 90m
        self.db.complete_rework(oid, [
            {'defect_id': did, 'action': '核减长度', 'new_length_m': 80,
             'photo_file': 's.svg'}], '钱修布')
        with self.assertRaises(DomainError) as cm:
            self.db.release(rid, '赵复检')
        self.assertIn('订单最小长度', str(cm.exception))

    def test_c_grade_and_clean_roll_release(self):
        rid, sid, did, tid = self.make_rolled_defect('SLUB', 'C')
        self._review_confirm(rid, did, tid)
        self.db.release(rid, '赵复检')  # C 级确认即可
        # 无疵点卷：结束初检直接进入待放行
        no = f'BR-CLEAN-{time.time_ns() % 100000}'
        rid2 = self.db.create_roll(no, 50, 1.5, 'CB-B09', 'LM-207')
        vid = self.db.begin_inspection(rid2, '李检')
        self.db.add_segment(vid, 0, 50, '李检')
        self.db.complete_initial(rid2)
        self.assertEqual(
            self.db.one('SELECT status FROM fabric_roll WHERE id=?', (rid2,))['status'],
            '待放行')
        self.db.release(rid2, '周复检')

    def test_released_roll_is_terminal(self):
        rid = self.ids['roll3']  # 种子里已放行
        with self.assertRaises(DomainError) as cm:
            self.db.begin_inspection(rid, '新人')
        self.assertIn('已放行', str(cm.exception))
        with self.assertRaises(DomainError):
            self.db.release(rid, '赵复检')

    def test_releaser_differs_from_initial_inspector(self):
        rid, sid, did, tid = self.make_rolled_defect('SLUB', 'C')
        self._review_confirm(rid, did, tid, reviewer='赵复检')
        with self.assertRaises(DomainError):
            self.db.release(rid, '王检')  # 放行人 = 初检人


class TestPhotoPermissions(QCBase):
    def test_identity_and_ownership(self):
        photo = f'r{self.ids["roll1"]}_d1.svg'
        with self.assertRaises(DomainError):  # 无身份
            self.db.photo_path_for(photo, None, self.ids['roll1'])
        with self.assertRaises(DomainError):  # 路径穿越
            self.db.photo_path_for('../../etc/passwd', '赵复检', None)
        with self.assertRaises(DomainError):  # 不属于该布卷
            self.db.photo_path_for(photo, '赵复检', self.ids['roll2'])
        path = self.db.photo_path_for(photo, '赵复检', self.ids['roll1'])
        self.assertTrue(os.path.isfile(path))


class TestHTTPEndpoints(QCBase):
    def setUp(self):
        super().setUp()
        app._database = self.db  # 让服务进程使用临时库
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()
        app._database = None

    def _url(self, path):
        return f'http://127.0.0.1:{self.port}{path}'

    def _req(self, path, method='GET', body=None, user=None, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        headers = {'Content-Type': 'application/json'}
        if user:
            sep = '&' if '?' in path else '?'
            path = f'{path}{sep}user={urlquote(user)}'
        req = Request(self._url(path), data=data, headers=headers, method=method)
        try:
            resp = urlopen(req, timeout=5)
            payload = resp.read()
            return resp.status, (payload if raw else json.loads(payload))
        except HTTPError as e:
            return e.code, json.loads(e.read())

    def test_index_and_apis(self):
        status, html = self._req('/', raw=True)
        self.assertEqual(status, 200)
        self.assertIn('布卷疵点复检与退修系统'.encode(), html)
        status, rolls = self._req('/api/rolls')
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(rolls), 5)

    def test_photo_http_permissions(self):
        photo = f'r{self.ids["roll1"]}_d1.svg'
        # 无身份 → 409
        status, body = self._req(
            f'/api/photos/{photo}?roll_id={self.ids["roll1"]}')
        self.assertEqual(status, 409)
        self.assertIn('身份', body['error'])
        # 错布卷 → 409
        status, body = self._req(
            f'/api/photos/{photo}?roll_id={self.ids["roll2"]}', user='赵复检')
        self.assertEqual(status, 409)
        # 正常 → 200 SVG
        req = Request(self._url(
            f'/api/photos/{photo}?roll_id={self.ids["roll1"]}&user={urlquote("赵复检")}'))
        resp = urlopen(req, timeout=5)
        self.assertEqual(resp.status, 200)
        self.assertIn('svg', resp.headers['Content-Type'])

    def test_claim_same_inspector_rejected_over_http(self):
        status, body = self._req(f'/api/tasks/{self.ids["task1"]}/claim',
                                 'POST', {'line': '复检一线', 'reviewer': '王检'})
        self.assertEqual(status, 409)
        self.assertIn('初检人', body['error'])

    def test_defect_out_of_range_rejected_over_http(self):
        # 卷5 正在初检，段 [0,40)；60m 越界
        seg = self.db.roll_detail(self.ids['roll5'])['segments'][0]
        status, body = self._req('/api/defects', 'POST', {
            'segment_id': seg['id'], 'type_code': 'STAIN', 'pos_m': 60,
            'length_m': 0.2, 'grade': 'B', 'inspector': '王检'}, user='王检')
        self.assertEqual(status, 409)
        self.assertIn('检验段', body['error'])


class TestDemoFlow(unittest.TestCase):
    def test_full_flow_via_app_helper(self):
        d = tempfile.mkdtemp(prefix='qc_demo_')
        try:
            database = Database(os.path.join(d, 't.db'), os.path.join(d, 'photos'))
            database.seed()
            summary = app.run_demo_flow(database)
            self.assertEqual(summary['claim']['line'], '复检二线')
            self.assertTrue(summary['release']['release_id'])
            row = database.one('SELECT * FROM fabric_roll WHERE id=?',
                               (summary['roll_id'],))
            self.assertEqual(row['status'], '已放行')
            self.assertEqual(row['released'], 1)
            database.conn.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
