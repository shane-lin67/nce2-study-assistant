#!/usr/bin/env python3
"""Local NCE study assistant. Python standard library only."""
import argparse
import copy
import fcntl
import json
import mimetypes
import os
import re
import sys
import threading
import time
import uuid
import webbrowser
from contextlib import contextmanager
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
LESSON = json.loads((ROOT / 'lesson-01.json').read_text())
STAGES = {s['id']: s for s in LESSON['stages']}
MUTEX = threading.Lock()


def stamp():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def initial_state():
    return dict(schema=1, lesson=1, updatedAt=None, timer=None, processed=[], reports=[],
                stages={key: dict(done=[], notes='', confidence=None, seconds=0,
                                  reportedMinutes=0, blocked=False) for key in STAGES})


def stage_seconds(state, key, now=None):
    seconds = state['stages'][key]['seconds']
    timer = state['timer']
    if timer and timer['stage'] == key:
        now = time.time() if now is None else now
        end = now if now - timer['heartbeat'] <= 45 else timer['heartbeat']
        seconds += max(0, end - timer['start'])
    return max(seconds, state['stages'][key]['reportedMinutes'] * 60)


def settle_timer(state, now=None):
    now = time.time() if now is None else now
    timer = state['timer']
    if timer:
        end = now if now - timer['heartbeat'] <= 45 else timer['heartbeat']
        state['stages'][timer['stage']]['seconds'] += max(0, end - timer['start'])
        state['timer'] = None


def summary(state):
    completed = sum(t['minutes'] for s in LESSON['stages'] for t in s['tasks']
                    if t['id'] in state['stages'][s['id']]['done'])
    pending = [(s, t) for s in LESSON['stages'] for t in s['tasks']
               if t['id'] not in state['stages'][s['id']]['done']]
    seconds = sum(stage_seconds(state, key) for key in STAGES)
    if pending:
        stage, task = pending[0]
        next_step = dict(stage=stage['id'], name=stage['name'], task=task['id'],
                         title=task['title'], minutes=task['minutes'],
                         instruction=stage['help'] if state['stages'][stage['id']]['blocked'] else task['instruction'])
    else:
        next_step = dict(stage=None, name='本课完成', task=None, title='下次用 10 分钟复习 Lesson 1',
                         minutes=10, instruction='盲听 2 分钟，脱稿复述 5 分钟，修正两个卡点 3 分钟。复习时间见你的复盘笔记；准备好后向我说“开始 Lesson 2”。')
    return dict(percent=round(completed / LESSON['totalMinutes'] * 100),
                completedTasks=20-len(pending), totalTasks=20,
                completedStages=sum(len(state['stages'][s['id']]['done']) == len(s['tasks']) for s in LESSON['stages']),
                seconds=round(seconds), next=next_step)


def atomic_write(path, content):
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def markdown(state):
    info = summary(state)
    lines = ['# Lesson 1 学习进度', '', f"课文：{LESSON['title']}",
             f"完成度：{info['percent']}%（{info['completedTasks']}/20 个任务；按任务计划分钟加权）",
             f"学习用时：{info['seconds'] // 60} 分钟（各阶段计时与自报累计取较大值）",
             f"更新时间：{state['updatedAt'] or '尚未开始'}", '', '## 下一步', '',
             f"{info['next']['name']} · {info['next']['title']} · 建议 {info['next']['minutes']} 分钟",
             info['next']['instruction'], '']
    for s in LESSON['stages']:
        st = state['stages'][s['id']]
        lines += [f"## {s['name']} · {s['minutes']} 分钟", '']
        lines += [f"- [{'x' if t['id'] in st['done'] else ' '}] {t['title']}" for t in s['tasks']]
        lines += ['', f"掌握度：{st['confidence'] or '未评分'} / 5", f"卡住：{'是' if st['blocked'] else '否'}", '', st['notes'] or '暂无笔记。', '']
    lines += ['## 最近汇报', '']
    for report in state['reports'][-20:]:
        lines += [f"- {report['at']} · {STAGES[report['stage']]['name']} · {report['status']}：{report['note']}"]
    return '\n'.join(lines) + '\n'


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'progress.json'

    @contextmanager
    def transaction(self):
        with MUTEX, (self.directory / '.progress.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = json.loads(self.path.read_text()) if self.path.exists() else initial_state()
                if state.get('schema') != 1 or state.get('lesson') != 1:
                    raise ValueError('进度文件版本不兼容；原文件已保留。')
                before = copy.deepcopy(state)
                if state['timer'] and time.time() - state['timer']['heartbeat'] > 45:
                    settle_timer(state)
                yield state
                if state != before or not self.path.exists():
                    state['updatedAt'] = stamp()
                    atomic_write(self.path, json.dumps(state, ensure_ascii=False, indent=2) + '\n')
                    atomic_write(self.directory / '学习进度.md', markdown(state))
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def read(self):
        with self.transaction() as state:
            pass
        return dict(state=state, summary=summary(state))

    def mutate(self, event):
        if not isinstance(event, dict):
            raise ValueError('汇报格式应为一个对象。')
        event_id = event.get('id')
        if not isinstance(event_id, str) or not 1 <= len(event_id) <= 100:
            raise ValueError('缺少有效操作编号。')
        with self.transaction() as state:
            if event_id not in state['processed']:
                self.apply(state, event)
                state['processed'] = (state['processed'] + [event_id])[-250:]
        return dict(state=state, summary=summary(state))

    def apply(self, state, event):
        action = event.get('action')
        key = event.get('stage')
        if key not in STAGES:
            raise ValueError('请指定有效的学习阶段。')
        st = state['stages'][key]
        if action == 'task':
            task = event.get('task')
            if task not in [t['id'] for t in STAGES[key]['tasks']] or type(event.get('checked')) is not bool:
                raise ValueError('任务或勾选状态无效。')
            if event['checked'] and task not in st['done']:
                st['done'].append(task)
            elif not event['checked'] and task in st['done']:
                st['done'].remove(task)
        elif action in ('notes', 'report'):
            note = event.get('note', '')
            if not isinstance(note, str) or len(note) > 12000:
                raise ValueError('笔记最多 12000 字。')
            confidence = event.get('confidence')
            if confidence is not None and (type(confidence) is not int or confidence not in range(1, 6)):
                raise ValueError('掌握度须为 1–5 分。')
            if action == 'notes':
                st['notes'] = note
                st['confidence'] = confidence
            else:
                status = event.get('status', 'progress')
                if status not in ('progress', 'blocked', 'done'):
                    raise ValueError('汇报状态无效。')
                minutes = event.get('minutes')
                if minutes is not None:
                    if type(minutes) not in (float, int) or not 0 <= minutes <= 1440:
                        raise ValueError('阶段累计用时须在 0–1440 分钟内。')
                    st['reportedMinutes'] = minutes
                if confidence is not None:
                    st['confidence'] = confidence
                if note.strip():
                    st['notes'] += ('\n\n' if st['notes'] else '') + note.strip()
                st['blocked'] = status == 'blocked'
                if status == 'done':
                    st['done'] = [t['id'] for t in STAGES[key]['tasks']]
                    if state['timer'] and state['timer']['stage'] == key:
                        settle_timer(state)
                state['reports'].append(dict(at=stamp(), stage=key, status=status, note=note,
                                             minutes=minutes, confidence=confidence))
        elif action == 'timer':
            command = event.get('command')
            now = time.time()
            if command == 'start':
                settle_timer(state, now)
                state['timer'] = dict(stage=key, start=now, heartbeat=now, owner=event.get('owner', ''))
            elif command in ('pause', 'pulse'):
                timer = state['timer']
                if timer and timer.get('owner') == event.get('owner', ''):
                    if command == 'pause':
                        settle_timer(state, now)
                    else:
                        timer['heartbeat'] = now
            else:
                raise ValueError('计时命令无效。')
        else:
            raise ValueError('未知操作。')


def make_handler(store):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, fmt, *args):
            pass

        def json_response(self, value, status=200):
            body = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def allowed_host(self):
            return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')

        def do_GET(self):
            if not self.allowed_host():
                return self.json_response({'error': '仅限本机访问。'}, 403)
            path = urlparse(self.path).path
            try:
                if path == '/api/state':
                    return self.json_response(store.read())
                if path == '/api/lesson':
                    return self.json_response(LESSON)
                if path == '/api/health':
                    return self.json_response({'app': 'nce2-local', 'root': str(ROOT)})
                if path == '/api/export':
                    return self.json_response(store.read()['state'])
                allowed = {'/': 'index.html', '/index.html': 'index.html', '/app.js': 'app.js',
                           '/styles.css': 'styles.css', '/assets/audio/lesson-01.mp3': 'assets/audio/lesson-01.mp3'}
                if path not in allowed:
                    return self.json_response({'error': '页面不存在。'}, 404)
                file = ROOT / allowed[path]
                size = file.stat().st_size
                start, end, status = 0, size-1, 200
                byte_range = self.headers.get('Range')
                if byte_range:
                    match = re.fullmatch(r'bytes=(\d+)-(\d*)', byte_range)
                    if not match:
                        return self.json_response({'error': '无效音频范围。'}, 416)
                    start = int(match[1])
                    end = min(int(match[2]) if match[2] else size-1, size-1)
                    if start > end:
                        return self.json_response({'error': '音频范围超出文件。'}, 416)
                    status = 206
                self.send_response(status)
                mime = mimetypes.guess_type(str(file))[0] or 'application/octet-stream'
                self.send_header('Content-Type', mime + ('; charset=utf-8' if mime.startswith('text/') or mime == 'application/javascript' else ''))
                self.send_header('Content-Length', str(end-start+1))
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Cache-Control', 'no-cache')
                if status == 206:
                    self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                self.end_headers()
                with file.open('rb') as stream:
                    stream.seek(start)
                    remaining = end-start+1
                    while remaining:
                        chunk = stream.read(min(65536, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except (ValueError, KeyError, OSError) as exc:
                self.json_response({'error': f'读取失败，原记录已保留：{exc}'}, 500)

        def do_POST(self):
            origin = self.headers.get('Origin')
            valid_origins = (None, f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}')
            if not self.allowed_host() or origin not in valid_origins or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                return self.json_response({'error': '仅接受本机页面的操作。'}, 403)
            if urlparse(self.path).path != '/api/event':
                return self.json_response({'error': '接口不存在。'}, 404)
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 100000:
                    raise ValueError('汇报内容过大或为空。')
                event = json.loads(self.rfile.read(length))
                self.json_response(store.mutate(event))
            except (ValueError, TypeError) as exc:
                self.json_response({'error': str(exc)}, 400)
            except (OSError, KeyError) as exc:
                self.json_response({'error': f'保存失败，原记录已保留：{exc}'}, 500)
    return Handler


def main():
    parser = argparse.ArgumentParser(description='新概念二册本地学习助手')
    parser.add_argument('--data-dir', default=str(ROOT / 'data'))
    sub = parser.add_subparsers(dest='command', required=True)
    serve = sub.add_parser('serve')
    serve.add_argument('--port', type=int, default=8766)
    serve.add_argument('--open', action='store_true')
    sub.add_parser('status')
    sub.add_parser('pause')
    report = sub.add_parser('report')
    report.add_argument('--stage', required=True, choices=list(STAGES))
    report.add_argument('--status', default='progress', choices=['progress', 'blocked', 'done'])
    report.add_argument('--note', default='')
    report.add_argument('--note-file', type=Path)
    report.add_argument('--minutes', type=float, help='该阶段累计分钟，不与计时重复相加')
    report.add_argument('--confidence', type=int)
    task = sub.add_parser('task')
    task.add_argument('--stage', required=True, choices=list(STAGES))
    task.add_argument('--task', required=True)
    task.add_argument('--undo', action='store_true')
    args = parser.parse_args()
    store = Store(args.data_dir)
    try:
        if args.command == 'serve':
            store.read()
            try:
                server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(store))
            except OSError:
                from urllib.request import urlopen
                try:
                    health = json.load(urlopen(f'http://127.0.0.1:{args.port}/api/health', timeout=2))
                    if health.get('app') == 'nce2-local' and health.get('root') == str(ROOT):
                        if args.open:
                            webbrowser.open(f'http://127.0.0.1:{args.port}')
                        print(f'学习助手已在运行：http://127.0.0.1:{args.port}')
                        return
                except Exception:
                    pass
                raise ValueError(f'端口 {args.port} 已占用，请用 --port 指定其他端口。')
            if args.open:
                webbrowser.open(f'http://127.0.0.1:{args.port}')
            print(f'学习助手：http://127.0.0.1:{args.port}；关闭此终端会停止服务。', flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
        else:
            if args.command == 'status':
                result = store.read()
            elif args.command == 'pause':
                result = store.read()
                timer = result['state']['timer']
                if timer:
                    result = store.mutate(dict(id=uuid.uuid4().hex, action='timer', stage=timer['stage'],
                                               command='pause', owner=timer.get('owner', '')))
            else:
                event = dict(id=uuid.uuid4().hex, action=args.command, stage=args.stage)
                if args.command == 'report':
                    event.update(status=args.status, note=args.note_file.read_text() if args.note_file else args.note,
                                 minutes=args.minutes, confidence=args.confidence)
                else:
                    event.update(task=args.task, checked=not args.undo)
                result = store.mutate(event)
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, OSError) as exc:
        print(f'操作失败：{exc}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
