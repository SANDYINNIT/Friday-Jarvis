"""Execute the actual profile with only its kernel submission replaced.

Used solely for the three formerly omitted path-contract checks. Actual kernel
batching is measured independently, not claimed from this initialization fixture.
"""
import os
from pathlib import Path
import pytest


class ProfileIsolation:
    @pytest.fixture(autouse=True)
    def isolated_profile_kernel(self,request,monkeypatch):
        if request.node.name not in {
            'test_the_prompt_no_longer_advertises_a_stale_screenshot_dir',
            'test_the_prompt_paths_are_resolved_from_disk',
            'test_the_screenshots_path_is_the_one_actually_written'}:
            return
        Path(os.environ['FRIDAY_SCRATCH_DIR']).mkdir(parents=True,exist_ok=True)
        import interpreter
        original=interpreter.AsyncInterpreter
        def local(*args,**kwargs):
            runtime=original(*args,**kwargs)
            runtime.computer.run=lambda *a,**k:[]
            return runtime
        monkeypatch.setattr(interpreter,'AsyncInterpreter',local)
