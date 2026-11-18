"""A synthetic registry backed by a dedicated fixture file, never real winreg."""
import json
import sys
from types import SimpleNamespace


class Registry:
    HKEY_CURRENT_USER = 1
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self, path):
        self.path = path
        if not path.exists():
            path.write_text(json.dumps({"FRIDAY01":None,"unrelated":"preserve-me"}))
        self.fail_read = self.fail_write = False
        self.reads = self.writes = 0

    def OpenKey(self, _root, _key, _zero, access):
        if self.fail_read or self.fail_write and access & self.KEY_SET_VALUE:
            raise PermissionError("SECRET_PRIVATE_PATH https://private.invalid/token")
        return self

    def __enter__(self): return self
    def __exit__(self, *_args): pass
    def Close(self): pass

    def QueryValueEx(self, _key, value):
        self.reads += 1
        stored = json.loads(self.path.read_text()).get(value)
        if stored is None: raise FileNotFoundError()
        return tuple(stored)

    def _save(self, value, entry):
        if self.fail_write: raise PermissionError("PRIVATE_ERROR")
        data = json.loads(self.path.read_text())
        data[value] = entry
        self.path.write_text(json.dumps(data))
        self.writes += 1

    def SetValueEx(self, _key, value, _zero, kind, command): self._save(value,[command,kind])
    def DeleteValue(self, _key, value): self._save(value,None)


class SettingsData:
    def __init__(self, area, runtime):
        self.registry = Registry(area/'settings-registry.json')
        sys.modules['winreg'] = self.registry
        self.runtime = runtime
        runtime.llm = SimpleNamespace(model='fixture-model <img src=x onerror=window.settingsRan=true> https://private.invalid/v1 token=PRIVATE_SECRET')
        self.initial = self.registry.path.read_bytes()

    def control(self, command):
        action = command['action'].removeprefix('settings:')
        reg = self.registry
        if action == 'read-error': reg.fail_read = command['value']
        elif action == 'write-error': reg.fail_write = command['value']
        elif action == 'external':
            from source.server.startup_settings import _startup_command
            reg._save('FRIDAY01',[_startup_command(),reg.REG_SZ] if command['value'] else None)
        elif action == 'foreign': reg._save('FRIDAY01',['PRIVATE_FOREIGN_COMMAND',reg.REG_SZ])
        elif action == 'reopen':
            self.registry = Registry(reg.path);sys.modules['winreg']=self.registry
        elif action == 'queue-error': self.runtime.voice_queue.reject=command['value']
        elif action == 'host-error': self.runtime.settings_host_error=command['value']
        return {'writes':self.registry.writes,'unchanged':reg.path.read_bytes()==self.initial,
                'voice_frames':self.runtime.voice_queue.frames,
                'unrelated':json.loads(reg.path.read_text())['unrelated']}
