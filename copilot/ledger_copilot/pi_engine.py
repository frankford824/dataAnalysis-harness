"""用 Pi（pi.dev）跑工具循环：上下文压缩、中途补充、出错重试交给 Pi；模型调用和工具都回到本服务。"""
import json
import os
import queue
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import config, store

PI_HOME = Path(os.environ.get('COPILOT_PI_HOME', r'D:\ledger\agent-pi'))
CLI = PI_HOME / 'node_modules' / '@earendil-works' / 'pi-coding-agent' / 'dist' / 'cli.js'
EXTENSION = Path(__file__).resolve().parent / 'pi' / 'ledger.ts'
AGENT_DIR = config.ROOT / 'pi'
OFF_SWITCH = config.ROOT / 'pi-off'
PROVIDER = 'ledger'
MAX_TURNS = 20
TURN_DEADLINE = 15 * 60
START_DEADLINE = 60


class Unavailable(RuntimeError):
    """Pi 没起来或没开始干活：调用方退回原来的循环。"""


class Run:
    def __init__(self, thread_id, role, step_emit, cards, emit):
        self.token = secrets.token_urlsafe(24)
        self.thread_id, self.role = thread_id, role
        self.step_emit, self.cards, self.emit = step_emit, cards, emit
        self.tokens, self.cost = 0, 0.0
        self.models = []
        self.budget_hit = False
        self.wants_engineer = False
        self.lock = threading.Lock()

    def add_usage(self, model_id, tokens, cost):
        with self.lock:
            self.tokens += tokens
            self.cost += cost
            if model_id not in self.models:
                self.models.append(model_id)


_live = {}


def run_for(token):
    return _live.get(token or '')


def node():
    found = shutil.which('node')
    if found:
        return found
    default = Path(r'C:\Program Files\nodejs\node.exe')
    return str(default) if default.exists() else None


def available():
    return not OFF_SWITCH.exists() and CLI.exists() and EXTENSION.exists() and node() is not None


def _model_ids():
    ids = [spec['id'] for role, spec in config.models().items() if role != 'vision']
    ids += list(config.FALLBACK.values())
    return list(dict.fromkeys(ids))


def _write_config():
    AGENT_DIR.mkdir(parents=True, exist_ok=True)
    models = [{'id': model_id, 'name': model_id, 'input': ['text'], 'contextWindow': 128000, 'maxTokens': 8192,
               'cost': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0}} for model_id in _model_ids()]
    provider = {
        'baseUrl': f'http://127.0.0.1:{config.PORT}/internal/pi/v1',
        'api': 'openai-completions',
        'apiKey': '$LEDGER_PI_TOKEN',
        'compat': {'supportsStore': False, 'supportsDeveloperRole': False, 'supportsReasoningEffort': False,
                   'maxTokensField': 'max_tokens', 'cacheControlFormat': 'anthropic', 'supportsStrictMode': False},
        'models': models,
    }
    _write(AGENT_DIR / 'models.json', {'providers': {PROVIDER: provider}})
    _write(AGENT_DIR / 'settings.json', {'quietStartup': True, 'enableInstallTelemetry': False})


def _write(path, data):
    text = json.dumps(data, ensure_ascii=False, indent=1)
    if not path.exists() or path.read_text(encoding='utf-8') != text:
        path.write_text(text, encoding='utf-8')


def _send(process, command):
    process.stdin.write(json.dumps(command, ensure_ascii=False).encode('utf-8') + b'\n')
    process.stdin.flush()


def _reader(process, out):
    for raw in process.stdout:
        line = raw.rstrip(b'\r\n')
        if not line:
            continue
        try:
            out.put(json.loads(line))
        except ValueError:
            continue
    out.put(None)


def _stderr(process, tail):
    for raw in process.stderr:
        tail.append(raw.decode('utf-8', 'replace').rstrip())
        del tail[:-40]


def run(run, *, model_id, system, prompt, inbox, stop, on_text, escalate):
    """跑完一轮，返回 (是否有过动作, 错误说明或 None)。没开始干活就出错抛 Unavailable。"""
    _write_config()
    system_file = AGENT_DIR / f'system-{run.token[:12]}.md'
    system_file.write_text(system, encoding='utf-8')
    env = {**os.environ, 'PI_CODING_AGENT_DIR': str(AGENT_DIR), 'PI_OFFLINE': '1', 'PI_SKIP_VERSION_CHECK': '1',
           'PI_TELEMETRY': '0', 'LEDGER_PI_TOKEN': run.token, 'LEDGER_PI_URL': f'http://127.0.0.1:{config.PORT}'}
    cwd = config.APP_DIR if config.APP_DIR.exists() else config.ROOT
    command = [node(), str(CLI), '--mode', 'rpc', '--no-session', '--provider', PROVIDER, '--model', model_id,
               '--thinking', 'off', '--no-builtin-tools', '--no-extensions', '--extension', str(EXTENSION),
               '--no-skills', '--no-prompt-templates', '--no-themes', '--no-context-files', '--no-mcp', '--offline',
               '--system-prompt', str(system_file)]
    _live[run.token] = run
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   cwd=str(cwd), env=env, creationflags=flags)
    except OSError as exc:
        _live.pop(run.token, None)
        raise Unavailable(str(exc)) from exc
    records, tail = queue.Queue(), []
    threading.Thread(target=_reader, args=(process, records), daemon=True).start()
    threading.Thread(target=_stderr, args=(process, tail), daemon=True).start()
    acted, error, failed, turns, started = False, None, '', 0, time.time()
    separate = aborting = False
    try:
        _send(process, {'id': 'ask', 'type': 'prompt', 'message': prompt})
        while True:
            if stop.is_set() and not aborting:
                aborting = True
                _send(process, {'type': 'abort'})
            if time.time() - started > TURN_DEADLINE and not aborting:
                aborting = True
                error = '这个问题查得太久，先停在这里。'
                _send(process, {'type': 'abort'})
            while inbox is not None:
                try:
                    extra = inbox.get_nowait()
                except queue.Empty:
                    break
                _send(process, {'type': 'steer', 'message': extra})
            try:
                record = records.get(timeout=0.5)
            except queue.Empty:
                if not acted and time.time() - started > START_DEADLINE:
                    raise Unavailable('Pi 没有响应：' + ' | '.join(tail[-5:]))
                continue
            if record is None:
                if not acted:
                    raise Unavailable('Pi 退出：' + ' | '.join(tail[-5:]))
                error = error or '这次处理中断了，请再问一次。'
                break
            kind = record.get('type')
            if kind == 'response' and record.get('id') == 'ask' and not record.get('success'):
                raise Unavailable('Pi 拒绝了问题：' + str(record.get('error'))[:300])
            if kind == 'message_update':
                update = record.get('assistantMessageEvent') or {}
                if update.get('type') == 'text_delta' and update.get('delta'):
                    if separate:
                        on_text('\n\n')
                        separate = False
                    acted = True
                    on_text(update['delta'])
            elif kind == 'message_end':
                message = record.get('message') or {}
                if message.get('role') == 'assistant':
                    if any(block.get('type') == 'text' and block.get('text') for block in message.get('content') or []):
                        separate = True
                    if message.get('stopReason') == 'error' and not run.budget_hit:
                        failed = str(message.get('errorMessage'))[:1000]
                        store.log_event('turn_error', {'thread': run.thread_id, 'error': 'pi: ' + failed})
            elif kind == 'tool_execution_start':
                acted = True
            elif kind == 'turn_end':
                turns += 1
                target = escalate(turns, run) if record.get('toolResults') else None
                if target:
                    _send(process, {'type': 'set_model', 'provider': PROVIDER, 'modelId': target})
                if turns > MAX_TURNS + 3 and not aborting:
                    aborting = True
                    _send(process, {'type': 'abort'})
            elif kind == 'auto_retry_end' and not record.get('success'):
                error = '这次处理遇到了问题，请稍后再试或换个说法。'
            elif kind == 'agent_settled':
                pending = []
                while inbox is not None:
                    try:
                        pending.append(inbox.get_nowait())
                    except queue.Empty:
                        break
                if pending and not stop.is_set():
                    _send(process, {'type': 'prompt', 'message': '\n\n'.join(pending)})
                    continue
                if failed and not acted and not run.budget_hit:
                    raise Unavailable('Pi 调模型失败：' + failed)
                break
    except OSError as exc:
        if not acted:
            raise Unavailable('Pi 管道断开：' + str(exc)) from exc
        error = '这次处理中断了，请再问一次。'
    finally:
        _live.pop(run.token, None)
        try:
            process.stdin.close()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
        system_file.unlink(missing_ok=True)
    if run.budget_hit:
        error = '本月助手的模型费用已到上限，下月自动恢复；如需提高上限请联系管理员。'
    return acted, error
