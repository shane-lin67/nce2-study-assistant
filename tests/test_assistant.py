import concurrent.futures
import importlib.util
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('assistant', Path(__file__).resolve().parents[1] / 'assistant.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = app.Store(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def event(self, **kwargs):
        return self.store.mutate(dict(id=uuid.uuid4().hex, stage='listening', **kwargs))

    def test_full_workflow_and_weighted_progress(self):
        self.assertEqual(sum(s['minutes'] for s in app.LESSON['stages']), 120)
        for s in app.LESSON['stages']:
            self.assertEqual(sum(t['minutes'] for t in s['tasks']), s['minutes'])
        self.assertEqual(self.store.read()['summary']['next']['stage'], 'listening')
        self.event(action='task', task='listening-1', checked=True)
        self.assertEqual(self.store.read()['summary']['percent'], 2)
        for i, stage in enumerate(app.STAGES):
            result = self.store.mutate(dict(id=uuid.uuid4().hex, action='report', stage=stage, status='done', note='已按标准完成'))
            self.assertEqual(result['summary']['completedStages'], i+1)
        result = self.store.read()
        self.assertEqual(result['summary']['percent'], 100)
        self.assertIsNone(result['summary']['next']['stage'])
        self.event(action='task', task='listening-1', checked=False)
        self.assertEqual(self.store.read()['summary']['next']['stage'], 'listening')

    def test_partial_blocked_report_does_not_complete_stage(self):
        self.event(action='report', status='blocked', note='听了三遍，还有两句听不懂', minutes=20)
        result = self.store.read()
        self.assertEqual(result['summary']['completedTasks'], 0)
        self.assertIn('0.85', result['summary']['next']['instruction'])
        self.assertEqual(result['summary']['seconds'], 1200)
        self.event(action='report', status='progress', note='重新听懂一句', minutes=20)
        self.assertEqual(self.store.read()['summary']['seconds'], 1200)
        self.event(action='report', status='done', note='听力完成')
        self.assertEqual(self.store.read()['summary']['next']['stage'], 'reading')

    def test_idempotency_and_restart(self):
        event = dict(id='same-operation', action='report', stage='listening', status='progress', note='文本 <script>原样保存</script>')
        self.store.mutate(event)
        self.store.mutate(event)
        reopened = app.Store(self.temp.name).read()
        self.assertEqual(len(reopened['state']['reports']), 1)
        self.assertIn('<script>', reopened['state']['stages']['listening']['notes'])
        self.assertTrue((Path(self.temp.name)/'学习进度.md').exists())

    def test_invalid_mutation_keeps_original(self):
        self.store.read()
        before = self.store.path.read_text()
        for bad in [dict(action='report', minutes=-2), dict(action='report', minutes=float('nan')), dict(action='report', confidence=8), dict(action='task', task='fake', checked=True), dict(action='notes', note='a'*12001)]:
            with self.assertRaises(ValueError):
                self.event(**bad)
            self.assertEqual(before, self.store.path.read_text())

    def test_parallel_task_updates_are_preserved(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: self.event(action='task', task=f'listening-{i}', checked=True), range(1,5)))
        self.assertEqual(len(self.store.read()['state']['stages']['listening']['done']),4)

    def test_timer_close_and_crash_do_not_count_away_time(self):
        with patch.object(app.time,'time',return_value=100):
            self.event(action='timer',command='start',owner='window-a')
        with patch.object(app.time,'time',return_value=110):
            self.event(action='timer',command='pulse',owner='window-a')
        with patch.object(app.time,'time',return_value=120):
            self.event(action='timer',command='pause',owner='window-b')
            self.assertIsNotNone(self.store.read()['state']['timer'])
        with patch.object(app.time,'time',return_value=1000):
            result=self.store.read()
            self.assertIsNone(result['state']['timer'])
            self.assertEqual(result['summary']['seconds'],10)
        self.assertEqual(result['summary']['completedTasks'],0)

    def test_source_and_timestamps(self):
        root=Path(__file__).resolve().parents[2]
        text=(root/'NCE2-course-text.md').read_text().split('# 01－A Private Conversation')[1].split('# 02－')[0]
        for sentence in app.LESSON['sentences']:
            self.assertIn(sentence['text'],text)
            self.assertGreater(sentence['end'],sentence['start'])
            self.assertLess(sentence['end'],app.LESSON['duration'])
        self.assertEqual(len(app.LESSON['sentences']),18)

    def test_http_audio_range_and_origin(self):
        server=app.ThreadingHTTPServer(('127.0.0.1',0),app.make_handler(self.store))
        worker=threading.Thread(target=server.serve_forever,daemon=True)
        worker.start()
        url=f'http://127.0.0.1:{server.server_port}'
        try:
            with urllib.request.urlopen(urllib.request.Request(url+'/assets/audio/lesson-01.mp3',headers={'Range':'bytes=100-199'})) as response:
                self.assertEqual(response.status,206)
                self.assertEqual(len(response.read()),100)
                self.assertIn('bytes 100-199/',response.headers['Content-Range'])
            request=urllib.request.Request(url+'/api/event',data=b'{}',headers={'Content-Type':'application/json','Origin':'https://example.com'})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code,403)
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(url+'/../assistant.py')
        finally:
            server.shutdown()
            server.server_close()


if __name__=='__main__':
    unittest.main()
