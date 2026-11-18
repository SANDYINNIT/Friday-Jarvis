"""Reproductions found during the pre-user-test review; synthetic state only."""
from datetime import datetime
import json
from threading import Event, Thread
import pytest

from source.server import prompt_render_cache as cache
from source.server.schedule_store import ScheduleStore, calendar_projection


def test_late_prompt_render_cannot_replace_a_new_turn_cache(monkeypatch):
    from interpreter.core import respond
    cache.uninstall();monkeypatch.setenv('FRIDAY_PROMPT_RENDER_CACHE','1')
    entered,release=Event(),Event();calls=[];results=[]
    runtime=type('Runtime',(),{'messages':[{'role':'user','content':'fixture'}]})()
    def render(_runtime,_message):
        number=len(calls)+1;calls.append(number)
        if number==1:
            entered.set();assert release.wait(5)
        return 'render-'+str(number)
    monkeypatch.setattr(respond,'render_message',render);assert cache.install()
    worker=Thread(target=lambda:results.append(respond.render_message(runtime,'template')))
    worker.start()
    try:
        assert entered.wait(2)
        cache.begin_turn()
        newer=respond.render_message(runtime,'template')
        release.set();worker.join(2);assert not worker.is_alive()
        assert respond.render_message(runtime,'template')==newer
        assert len(calls)==2, 'A late old render discarded the newer admitted turn cache.'
        assert results==['render-1']
    finally:
        release.set();worker.join(2);cache.uninstall()


def test_malformed_or_proposed_calendar_records_do_not_break_conflict_detection(tmp_path,monkeypatch):
    monkeypatch.delenv('FRIDAY_SCHEDULE_FILE',raising=False)
    path=tmp_path/'calendar.json'
    path.write_text(json.dumps([
        dict(title='Invalid legacy duration',start='2026-11-14T10:00+01:00',duration_minutes='bad'),
        dict(title='Proposal only',start='2026-11-14T10:00+01:00',status='proposed'),
        dict(title='Saved event',start='2026-11-14T11:00+01:00',duration_minutes=30),
    ]))
    store=ScheduleStore(path)
    assert store.find_conflict(datetime.fromisoformat('2026-11-14T10:05+01:00'),10) is None
    assert store.find_conflict(datetime.fromisoformat('2026-11-14T11:05+01:00'),10)['title']=='Saved event'


def test_invalid_saved_duration_is_unplaced_instead_of_a_reversed_event():
    result=calendar_projection([dict(title='Invalid',start='2026-11-14T10:00+01:00',duration_minutes=-60)])
    assert result['events'][0]['kind']=='unplaced'


def test_calendar_constructor_obeys_the_same_size_bound_as_its_reader(tmp_path,monkeypatch):
    monkeypatch.delenv('FRIDAY_SCHEDULE_FILE',raising=False)
    path=tmp_path/'large.json';path.write_text(json.dumps([{'title':'x'*2_000_000}]))
    store=ScheduleStore(path)
    assert store._read_failed, 'Startup parsed a calendar that the passive reader refuses as too large.'


@pytest.mark.parametrize('limit',['count','bytes'])
def test_calendar_failed_publication_cannot_replace_its_last_readable_file(tmp_path,monkeypatch,limit):
    monkeypatch.delenv('FRIDAY_SCHEDULE_FILE',raising=False)
    rows=([{'title':str(i),'start':'2026-11-14'} for i in range(10000)] if limit=='count' else
          [{'title':'Keep','start':'2026-11-14','notes':'x'*1_999_850}])
    path=tmp_path/'calendar.json';path.write_text(json.dumps(rows,separators=(',',':')))
    before=path.read_bytes();store=ScheduleStore(path)
    revision=store.agenda()['revision']
    result=store.mutate(None,'create',dict(title='n'*300,start='2026-11-15',all_day=True),revision)
    assert result['ok'] is False
    assert path.read_bytes()==before, 'Failed publication replaced the last usable calendar.'
    assert ScheduleStore(path).agenda()['revision']==revision


def test_disabling_an_installed_prompt_cache_really_restores_the_renderer(monkeypatch):
    from interpreter.core import respond
    cache.uninstall();monkeypatch.setenv('FRIDAY_PROMPT_RENDER_CACHE','1')
    calls=[]
    def original(*_):calls.append(1);return 'call-'+str(len(calls))
    monkeypatch.setattr(respond,'render_message',original)
    runtime=type('Runtime',(),{'messages':[]})()
    try:
        assert cache.install()
        respond.render_message(runtime,'template')
        monkeypatch.setenv('FRIDAY_PROMPT_RENDER_CACHE','0');assert not cache.install()
        assert respond.render_message is original
        assert respond.render_message(runtime,'template')=='call-2'
    finally:cache.uninstall()


def test_replaced_cache_wrapper_cannot_publish_a_late_old_render(monkeypatch):
    from interpreter.core import respond
    cache.uninstall();monkeypatch.setenv('FRIDAY_PROMPT_RENDER_CACHE','1')
    entered,release=Event(),Event()
    runtime=type('Runtime',(),{'messages':[]})()
    def old(*_):entered.set();assert release.wait(5);return 'old-instance'
    monkeypatch.setattr(respond,'render_message',old);assert cache.install()
    worker=Thread(target=lambda:respond.render_message(runtime,'same-template'));worker.start()
    try:
        assert entered.wait(2)
        cache.uninstall()
        monkeypatch.setattr(respond,'render_message',lambda *_:'new-instance')
        assert cache.install()
        assert respond.render_message(runtime,'same-template')=='new-instance'
        release.set();worker.join(2);assert not worker.is_alive()
        assert respond.render_message(runtime,'same-template')=='new-instance'
    finally:release.set();worker.join(2);cache.uninstall()
