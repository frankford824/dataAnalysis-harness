import json
import os
from pathlib import Path

ROOT = Path(os.environ.get('COPILOT_HOME', r'D:\ledger\home\agent'))
LEDGER_HOME = Path(os.environ.get('LEDGER_HOME', r'D:\ledger\home'))
LEDGER_URL = os.environ.get('COPILOT_LEDGER_URL', 'http://127.0.0.1:8000').rstrip('/')
APP_DIR = Path(os.environ.get('COPILOT_APP_DIR', r'D:\ledger\app'))
SELF_DIR = Path(__file__).resolve().parents[1]
PORT = int(os.environ.get('COPILOT_PORT', '8020'))
MONTH_BUDGET_CNY = 2000.0
DATABASES = {
    'workspace': LEDGER_HOME / 'workspace.db',
    'commission': LEDGER_HOME / 'commission' / 'registry.db',
}

MODELS = {
    'quick': {'id': 'qwen3.8-flash'},
    'investigate': {'id': 'qwen3.8-max'},
    'engineer': {'id': 'kimi-k2.7-code'},
    'vision': {'id': 'qwen3-vl-plus'},
}
FALLBACK = {'investigate': 'qwen3.7-plus', 'engineer': 'glm-5.3', 'quick': 'deepseek-v4.1-flash'}


def models():
    path = ROOT / 'models.json'
    if path.exists():
        override = json.loads(path.read_text(encoding='utf-8'))
        return {role: {**MODELS.get(role, {}), **value} for role, value in {**MODELS, **override}.items()}
    return MODELS


def gateway():
    cfg = json.loads((LEDGER_HOME / 'llm.json').read_text(encoding='utf-8'))
    key_file = cfg.get('api_key_file') or str(LEDGER_HOME.parent / 'secrets' / 'llm-api-key')
    key = os.environ.get('LEDGER_LLM_API_KEY') or Path(key_file).read_text(encoding='utf-8').strip()
    return cfg['base_url'].rstrip('/'), key
