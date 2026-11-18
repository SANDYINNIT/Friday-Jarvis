import contextlib
import io
from types import SimpleNamespace
import pytest
from source.server.prompt_batch import render


class Computer:
    def __init__(self):
        self.save_skills = True
        self.calls = 0
        self.namespace = {}

    def run(self, language, program, display=False):
        self.calls += 1
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            exec(program, self.namespace)
        return [{"format":"output", "content":stream.getvalue()}]


def test_same_order_namespace_and_output_in_one_real_program():
    computer = Computer()
    runtime = SimpleNamespace(computer=computer, verbose=False)
    result = render(runtime, "before {{x=3;print(x)}} middle {{x+=2;print(x)}} after")
    assert result == "before 3\n middle 5\n after"
    assert computer.namespace['x'] == 5
    assert computer.calls == 1 and computer.save_skills


def test_block_failure_does_not_drop_other_sections_or_change_saved_setting():
    computer = Computer();computer.save_skills=False
    result = render(SimpleNamespace(computer=computer,verbose=False),
                    "{{print('first')}}/{{raise ValueError('private')}}/{{print('last')}}")
    assert result == 'first\n//last' and computer.save_skills is False


def test_bad_frame_never_reexecutes_blocks_and_restores_setting():
    computer = Computer()
    def bad(*args, **kwargs):
        computer.calls += 1
        return [{"format":"output","content":"not a frame"}]
    computer.run = bad
    with pytest.raises(ValueError):
        render(SimpleNamespace(computer=computer,verbose=False),'{{print(1)}}')
    assert computer.calls == 1 and computer.save_skills


def test_literal_only_render_and_inert_output():
    computer=Computer();runtime=SimpleNamespace(computer=computer,verbose=False)
    assert render(runtime,'literal')=='literal' and computer.calls==0
    assert render(runtime,"{{print('<script>never execute</script>')}}")=='<script>never execute</script>'
