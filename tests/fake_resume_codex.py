"""Persistent, paginated protocol fixture for importing external root sessions."""
import asyncio
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

store = Path('resume-fixture.json')
state = json.loads(store.read_text())
calls, tasks = [], {}


def send(value):
    store.write_text(json.dumps(state))
    print(json.dumps(value), flush=True)


async def run(params, turn):
    tid = params['threadId']
    thread = state['threads'][tid]
    text = '\n'.join(i['text'] for i in params['input'] if i['type'] == 'text'
                     and not i['text'].startswith('[Codex Room host context;'))
    turn['items'].append({'id': 'user-' + turn['id'], 'type': 'userMessage', 'content': params['input']})
    if text == 'hold':
        await asyncio.sleep(30)
    body = thread.get('memory', 'NO_MEMORY') if text == 'recall' else text
    answer = {'id': 'answer-' + turn['id'], 'type': 'agentMessage', 'text': body}
    turn['items'].append(answer)
    send({'method': 'item/completed', 'params': {'threadId': tid, 'turnId': turn['id'], 'item': answer}})
    turn.update(status='completed', completedAt=int(time.time()))
    send({'method': 'turn/completed', 'params': {'threadId': tid, 'turn': turn}})


async def main():
    while line := await asyncio.to_thread(sys.stdin.readline):
        msg = json.loads(line)
        if 'id' not in msg or 'method' not in msg:
            continue
        method, p = msg['method'], msg.get('params', {})
        calls.append({'method': method, 'params': p})
        result = {}
        try:
            if method == 'config/read':
                result = {'config': {'model': 'ROOM_DEFAULT', 'model_reasoning_effort': 'low'}}
            elif method == 'model/list':
                result = {'data': []}
            elif method == 'fixture/state':
                result = {'state': state, 'calls': calls}
            elif method == 'fixture/update':
                state['threads'][p['threadId']] = p['thread']
            elif method == 'fixture/unfinished':
                state['turns'][p['threadId']][-1]['status'] = 'inProgress'
            elif method == 'thread/start':
                tid = str(uuid4())
                state['threads'][tid] = {'id': tid, 'cwd': p['cwd'], 'model': p.get('model', 'ROOM_DEFAULT'),
                    'reasoningEffort': 'low', 'approvalPolicy': p['approvalPolicy'], 'sandbox': {'type': 'dangerFullAccess'}}
                result = {'thread': state['threads'][tid]}
            elif method == 'thread/read':
                result = {'thread': state['threads'][p['threadId']]}
            elif method == 'thread/name/set':
                state['threads'][p['threadId']]['name'] = p['name']
            elif method == 'thread/resume':
                thread = state['threads'][p['threadId']]
                result = {**thread, 'thread': thread}
            elif method == 'thread/list':
                result = {'data': [v for v in state['threads'].values() if v.get('parentThreadId') == p.get('ancestorThreadId')]}
            elif method == 'thread/turns/list':
                turns = state.get('turns', {}).get(p['threadId'], [])
                start = int(p.get('cursor', 0))
                result = {'data': [{**t, 'items': [], 'itemsView': 'summary'} for t in turns[start:start+1]],
                          'nextCursor': str(start+1) if start+1 < len(turns) else None}
            elif method == 'thread/items/list':
                turn = next(t for t in state['turns'][p['threadId']] if t['id'] == p['turnId'])
                start = int(p.get('cursor', 0))
                result = {'data': [{'item': i} for i in turn['items'][start:start+2]],
                          'nextCursor': str(start+2) if start+2 < len(turn['items']) else None}
            elif method == 'turn/start':
                tid = p['threadId']
                turn = {'id': str(uuid4()), 'status': 'inProgress', 'startedAt': int(time.time()), 'items': []}
                state.setdefault('turns', {}).setdefault(tid, []).append(turn)
                result = {'turn': turn}
                tasks[turn['id']] = asyncio.create_task(run(p, turn))
            elif method == 'turn/interrupt':
                if p['turnId'] in tasks:
                    tasks[p['turnId']].cancel()
            send({'id': msg['id'], 'result': result})
        except Exception as exc:
            send({'id': msg['id'], 'error': {'code': -32602, 'message': str(exc)}})


asyncio.run(main())
