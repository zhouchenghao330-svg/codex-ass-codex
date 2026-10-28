"""Opt-in recovery check: real Codex app-server with a localhost-only model fixture.

No cloud model calls or API key. Uses temporary Room data and archives its new
native thread after checking automatic mid-turn compaction and original-body
recovery. It does not open or resume any existing user thread.
Run: uv run python scripts/check_context_recovery_native.py
"""
import asyncio
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import time
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_room.attachments import Prompt
from codex_room.backend import Member, Service
from codex_room.models import Entry
from codex_room.platform_support import codex_command

requests = []
class Provider(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
        requests.append(body)
        number = len(requests)
        if number == 1:
            item = {'type': 'function_call', 'call_id': 'local-plan', 'name': 'update_plan',
                    'arguments': json.dumps({'plan': [{'step': 'Verify recovery', 'status': 'in_progress'}]})}
            tokens = 160000
        else:
            item = {'type': 'message', 'role': 'assistant', 'id': f'message-{number}',
                    'content': [{'type': 'output_text', 'text': 'COMPACTED_SYNTHETIC_HISTORY' if number == 2 else 'NATIVE_RECOVERY_VERIFIED'}]}
            tokens = 100
        events = [{'type': 'response.created', 'response': {'id': f'response-{number}'}},
                  {'type': 'response.output_item.done', 'item': item},
                  {'type': 'response.completed', 'response': {'id': f'response-{number}', 'usage': {
                      'input_tokens': tokens, 'output_tokens': 1, 'total_tokens': tokens + 1,
                      'input_tokens_details': None, 'output_tokens_details': None}}}]
        content = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        time.sleep(.1)
        self.wfile.write(content)

async def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix='room-recovery-native-') as temp:
        cwd = Path(temp)
        overrides = {
            'model_provider': 'room_native_fixture', 'model': 'gpt-6.1-sol', 'model_reasoning_effort': 'medium',
            'model_providers.room_native_fixture.name': 'Room Native Fixture',
            'model_providers.room_native_fixture.base_url': f'http://127.0.0.1:{server.server_port}/v1',
            'model_providers.room_native_fixture.wire_api': 'responses',
            'model_providers.room_native_fixture.requires_openai_auth': False,
            'model_providers.room_native_fixture.supports_websockets': False,
            'model_auto_compact_token_limit': 100000,
            'model_auto_compact_token_limit_scope': 'total', 'features.token_budget': False,
        }
        config_path = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')) / 'config.toml'
        if config_path.is_file():
            for name in tomllib.loads(config_path.read_text()).get('mcp_servers', {}):
                overrides[f'mcp_servers.{name}.enabled'] = False
        command = codex_command()
        for key, value in overrides.items():
            command.extend(['-c', key + '=' + json.dumps(value)])
        room = Service(cwd, command=command, emit=lambda _: None)
        native_events, recoveries, event_kinds = [], [], []
        thread_id = None
        try:
            await room.start()
            assert room.connected, 'native app-server connection failed'
            room.agents['A'] = Member('A', '开发', 'white', str(cwd), model='gpt-6.1-sol', effort='medium')
            room.agents['B'] = Member('B', '资料员', 'white', str(cwd), thread_id='fixture-peer')
            await room.client.ensure_thread(room.agents['A'])
            thread_id = room.agents['A'].thread_id
            original_body = 'B 原始消息\n\n```json\n{"keep": "原样"}\n```\n请检查接口。'
            request = Entry('B', original_body, 'A', communication={
                'transport': 'room', 'source_thread_id': 'fixture-peer', 'target_thread_id': thread_id,
                'expect_reply': True, 'delivery': 'delivered', 'chain_id': 'native-recovery-check'})
            room.peers.messages[request.id] = request
            room.add_entry(request)
            original_request, observer = room.client.request, room.client.observer
            async def record(method, params, **kwargs):
                if method == 'turn/steer':
                    recoveries.append(params)
                return await original_request(method, params, **kwargs)
            def observe(event):
                event_kinds.append((event['method'], event.get('params', {}).get('item', {}).get('type')))
                if event['method'] == 'item/completed' and event.get('params', {}).get('item', {}).get('type') == 'contextCompaction':
                    native_events.append(event['params'])
                    room.agents['A'].label = '压缩后最新身份'
                return observer(event)
            room.client.request, room.client.observer = record, observe
            await room.submit(Prompt('@A Complete the local plan and reply.'))
            async with asyncio.timeout(40):
                while room.agents['A'].busy:
                    await asyncio.sleep(.02)
            errors = [e.body for e in room.entries if e.sender == 'A' and e.state == 'failed']
            assert not errors, errors
            assert native_events, repr({'native_events':event_kinds, 'requests':len(requests), 'limit':room.client.config.get('model_auto_compact_token_limit'), 'request_tools':[t.get('name',t.get('type')) for t in requests[0].get('tools',[])] if requests else []})
            assert len(recoveries) == 1, f'recovery count: {len(recoveries)}'
            recovered_text = recoveries[0]['input'][0]['text']
            pending = json.loads(next(line.split(': ', 1)[1] for line in recovered_text.splitlines() if line.startswith('Outstanding replies')))
            assert pending['requests'][0]['message'] == original_body
            assert recoveries[0]['expectedTurnId'] == native_events[0]['turnId']
            assert len([e for e in room.entries if e.sender == 'you']) == 1
            assert len(requests) >= 3
            def strings(value):
                if isinstance(value, str):
                    yield value
                elif isinstance(value, list):
                    for item in value:
                        yield from strings(item)
                elif isinstance(value, dict):
                    for item in value.values():
                        yield from strings(item)
            restored_in = [index + 1 for index, r in enumerate(requests) if any(recovered_text in text for text in strings(r.get('input', [])))]
            assert restored_in, 'recovered state was accepted but not observed by a model request'
            assert '压缩后最新身份' in recovered_text
            print(json.dumps({'codex': 'real native app-server', 'provider': 'localhost simulated SSE; no cloud requests',
                'native_compactions': len(native_events), 'recovery_steers': len(recoveries),
                'original_body_preserved': True, 'same_turn': True,
                'model_requests': len(requests),
                'model_requests_with_recovery_text': restored_in}, ensure_ascii=False))
        finally:
            if thread_id and room.client and not room.agents['A'].busy:
                try:
                    await room.client.request('thread/archive', {'threadId': thread_id})
                except Exception:
                    pass
            await room.close()
            server.shutdown()

asyncio.run(main())
