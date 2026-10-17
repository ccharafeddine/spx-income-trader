"""Secrets must come from the untracked local config or environment variables."""
from pathlib import Path

import yaml

from config.settings import _apply_env_overrides

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / 'config' / 'strategy_params.example.yaml'


def test_example_config_has_no_secrets():
    data = yaml.safe_load(EXAMPLE.read_text(encoding='utf-8'))
    assert data['broker']['schwab']['account_number'] == ''
    for hook in data['notifications']['webhooks']:
        assert hook['url'] == ''


def test_local_config_is_gitignored():
    gitignore = (ROOT / '.gitignore').read_text(encoding='utf-8').splitlines()
    assert 'config/strategy_params.yaml' in gitignore


def test_env_overrides_account_and_discord(monkeypatch):
    monkeypatch.setenv('SCHWAB_ACCOUNT_NUMBER', '1234-5678')
    monkeypatch.setenv('DISCORD_WEBHOOK_URL', 'https://discord.com/api/webhooks/1/abc')
    settings = {
        'broker': {'schwab': {'account_number': ''}},
        'notifications': {'webhooks': [
            {'type': 'slack', 'url': 'https://hooks.slack.com/x', 'enabled': True},
            {'type': 'discord', 'url': '', 'enabled': True, 'min_level': 'info'},
        ]},
    }
    out = _apply_env_overrides(settings)
    assert out['broker']['schwab']['account_number'] == '1234-5678'
    assert out['notifications']['webhooks'][0]['url'] == 'https://hooks.slack.com/x'
    assert out['notifications']['webhooks'][1]['url'] == 'https://discord.com/api/webhooks/1/abc'


def test_env_override_adds_discord_hook_when_missing(monkeypatch):
    monkeypatch.delenv('SCHWAB_ACCOUNT_NUMBER', raising=False)
    monkeypatch.setenv('DISCORD_WEBHOOK_URL', 'https://discord.com/api/webhooks/2/def')
    out = _apply_env_overrides({})
    assert out['notifications']['webhooks'] == [
        {'type': 'discord', 'url': 'https://discord.com/api/webhooks/2/def',
         'min_level': 'info', 'enabled': True}]
    assert 'broker' not in out


def test_no_env_leaves_settings_untouched(monkeypatch):
    monkeypatch.delenv('SCHWAB_ACCOUNT_NUMBER', raising=False)
    monkeypatch.delenv('DISCORD_WEBHOOK_URL', raising=False)
    settings = {'broker': {'schwab': {'account_number': 'local'}}}
    assert _apply_env_overrides(settings) == {'broker': {'schwab': {'account_number': 'local'}}}
