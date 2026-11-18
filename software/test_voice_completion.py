"""Actual PCM callback metrics, with fake devices by default."""
import importlib
import pytest
from source.server.audio_link import AudioLink

client=importlib.import_module('source.clients.light-python.client')


def test_callback_starvation_is_measured_not_mistaken_for_drain():
    playback=client.DevicePlayback('one')
    playback.append(b'\x01\x02'*16)
    playback.callback(None,32,None,client.pyaudio.paOutputUnderflow)
    assert playback.metrics()['starved_callbacks']==1
    assert playback.metrics()['device_underflows']==1
    assert playback.metrics()['inserted_silence_frames']==16
    assert not playback.completed
    playback.sealed=True
    assert playback.callback(None,32,None,0)[1]==client.pyaudio.paComplete
    assert playback.metrics()['starved_callbacks']==1


def test_metrics_cannot_change_outcome_or_update_a_new_connection():
    state={'is_paused':False};link=AudioLink(state)
    session=link.connect(True);token=object();link.begin(token)
    p=link.playback;p.bytes_sent=32
    frame={'audio_session':session,'playback':p.id,'event':'progress','bytes':16,
           'metrics':{'first_callback_ms':12,'peak_buffer_ms':float('inf'),'device_underflows':-2}}
    assert link.accept_playback(session,frame)
    assert p.metrics=={'first_callback_ms':12}
    assert p.outcome=='waiting'
    link.disconnect(session);new=link.connect(True);link.begin(object())
    assert not link.accept_playback(session,frame)
    assert link.playback.metrics=={}
