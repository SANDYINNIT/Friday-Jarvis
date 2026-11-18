"""Normal OI prompt assembly, isolated devices and providers."""
from types import SimpleNamespace
import pytest
from interpreter import AsyncInterpreter
from interpreter.core import respond
from interpreter.core.render_message import render_message
from source.server import prompt_render_cache as cache
from test_prompt_batch import Computer

@pytest.fixture(autouse=True)
def clean_cache():
    cache.uninstall()
    yield
    cache.uninstall()

@pytest.mark.parametrize('suffix', ['language','custom','computer'])
def test_normal_oi_assembled_prompt_uses_one_profile_batch(suffix, monkeypatch):
    runtime=AsyncInterpreter()
    computer=Computer()
    runtime.system_message='\n'.join('section%d {{print(%r)}}' % (i,'value-%d'%i) for i in range(11))
    runtime.friday_batch_prompt=True
    computer.terminal=SimpleNamespace(languages=[SimpleNamespace(system_message='Language directions')] if suffix=='language' else [])
    computer.import_computer_api=suffix=='computer'
    computer.system_message='Computer directions'
    runtime.computer=computer
    runtime.custom_instructions='Additional directions' if suffix=='custom' else ''
    runtime.messages=[{'role':'user','type':'message','content':'Synthetic request'}]
    seen=[]
    def run(messages):
        seen.append(messages[0]['content'])
        yield {'type':'message','content':'Synthetic answer.'}
    runtime.llm.run=run
    monkeypatch.setattr(respond,'render_message',render_message)
    cache.install()
    generator=respond.respond(runtime)
    try:assert next(generator)['content']=='Synthetic answer.'
    finally:generator.close()
    assert computer.calls==1, 'Normal OI suffix bypassed the enabled profile batch: %d kernel submissions' % computer.calls
    assert all('value-%d'%i in seen[0] for i in range(11))
    assert {'language':'Language directions','custom':'Additional directions','computer':'Computer directions'}[suffix] in seen[0]


def test_extra_dynamic_blocks_keep_original_renderer_and_order(monkeypatch):
    computer=Computer();template='{{x=1;print(x)}}'
    runtime=SimpleNamespace(computer=computer,verbose=False,debug=False,friday_batch_prompt=True,system_message=template,messages=[])
    monkeypatch.setattr(respond,'render_message',render_message)
    cache.install()
    assert respond.render_message(runtime,template+'\n\n{{x+=1;print(x)}}')=='1\n\n\n2'
    assert computer.calls==2 and computer.namespace['x']==2
