"""Actual Settings registry/bridge paths with an explicit synthetic registry only."""
from concurrent.futures import ThreadPoolExecutor
import json
import sys
from types import SimpleNamespace
import pytest

from source.server.status_ui import WebviewBridge, StatusBus
from source.server import startup_settings
from tools.field_notes_acceptance.settings_data import Registry


@pytest.fixture
def setup(tmp_path,monkeypatch):
    registry=Registry(tmp_path/'registry.json')
    monkeypatch.setitem(sys.modules,'winreg',registry)
    runtime=SimpleNamespace(server_state={'is_paused':False},llm=SimpleNamespace(model='model https://private.invalid token=SECRET'))
    return registry, WebviewBridge(StatusBus(),runtime),runtime


def test_reads_are_passive_sanitized_and_preserve_missing_values(setup,monkeypatch):
    reg,bridge,runtime=setup
    before=reg.path.read_bytes()
    monkeypatch.setattr(startup_settings,'_pystray_available',lambda:pytest.fail('tray import'))
    monkeypatch.setenv('OPENAI_BASE_URL','https://PRIVATE_ENDPOINT.invalid')
    monkeypatch.setenv('FRIDAY_MEMORY_DB','PRIVATE_DATABASE')
    for _ in range(4):
        snapshot=bridge.get_startup();host=bridge.get_settings()
    assert snapshot['startup_on'] is False and snapshot['editable']
    assert snapshot['registration']=='absent'
    assert reg.path.read_bytes()==before and reg.writes==0 and runtime.server_state=={'is_paused':False}
    assert 'PRIVATE' not in json.dumps(host) and 'https://' not in json.dumps(host)
    assert 'TTS URL' not in host and 'memory DB' not in host
    assert snapshot['tray_state']=='not reported'


def test_save_reopen_and_preserve_unrelated_registry_values(setup,monkeypatch):
    reg,bridge,_=setup
    saved=bridge.set_startup(True,bridge.get_startup()['revision'])
    assert saved['accepted'] and saved['enabled']
    reopened=Registry(reg.path);monkeypatch.setitem(sys.modules,'winreg',reopened)
    assert bridge.get_startup()['startup_on'] is True
    assert json.loads(reg.path.read_text())['unrelated']=='preserve-me'
    assert bridge.set_startup(False,saved['revision'])['accepted']
    assert bridge.get_startup()['startup_on'] is False


@pytest.mark.parametrize('invalid',[None,1,'true',{},['on']])
def test_invalid_settings_rejected_without_partial_mutation(setup,invalid):
    reg,bridge,_=setup;before=reg.path.read_bytes()
    assert not bridge.set_startup(invalid,bridge.get_startup()['revision'])['accepted']
    assert not bridge.set_startup(True,{'public_ingress':True})['accepted']
    assert not bridge.set_startup(True)['accepted']
    assert reg.path.read_bytes()==before and reg.writes==0


def test_denied_write_keeps_original_and_errors_secret_free(setup):
    reg,bridge,runtime=setup;expected=bridge.get_startup()['revision'];before=reg.path.read_bytes()
    reg.fail_write=True
    result=bridge.set_startup(True,expected)
    assert not result['accepted'] and 'PRIVATE' not in json.dumps(result)
    assert reg.path.read_bytes()==before and not runtime.server_state['is_paused']
    reg.fail_read=True
    assert not bridge.get_startup()['available'] and reg.path.read_bytes()==before


def test_competing_stale_saves_cannot_overwrite_current_registration(setup):
    reg,bridge,_=setup;revision=bridge.get_startup()['revision']
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:bridge.set_startup(True,revision),range(2)))
    assert sum(result['accepted'] for result in results)==1 and reg.writes==1
    assert any(result.get('conflict') for result in results)
    assert not bridge.set_startup(False,revision)['accepted']
    assert bridge.get_startup()['startup_on'] is True


def test_foreign_or_malformed_registration_is_not_overwritten(setup):
    reg,bridge,_=setup
    for entry in (['PRIVATE_OTHER_APP',reg.REG_SZ],['not-string',99]):
        reg._save('FRIDAY01',entry);before=reg.path.read_bytes();snapshot=bridge.get_startup()
        assert snapshot['startup_on'] is None and not snapshot['editable']
        assert not bridge.set_startup(True,snapshot['revision'])['accepted']
        assert reg.path.read_bytes()==before and 'PRIVATE' not in json.dumps(snapshot)


def test_unknown_keyword_never_mutates_registration(setup):
    reg,bridge,_=setup;before=reg.path.read_bytes()
    with pytest.raises(TypeError):bridge.set_startup(True,bridge.get_startup()['revision'],public_ingress=True)
    assert reg.path.read_bytes()==before
