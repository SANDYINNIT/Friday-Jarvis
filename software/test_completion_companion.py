"""Optional voxel appearance uses the existing owned companion, never a runtime."""
from unittest.mock import Mock
from source.server.companion import Companion
from source.server.companion_native import BarBridge,buddy_image


def test_bee_persists_but_never_enables_cursor_or_opens_a_window_on_read(tmp_path):
    presenter=Companion(tmp_path/'settings.json');opened=Mock(return_value=True)
    presenter.action=opened
    assert presenter.save({'style':'bee'},presenter.preferences()['revision'])['ok']
    reopened=Companion(presenter.path)
    assert reopened.preferences()['values']['style']=='bee'
    assert reopened.preferences()['values']['cursor']=='off'
    for _ in range(3):assert BarBridge(presenter).get_snapshot()['appearance']=='bee'
    opened.assert_not_called()
    image=buddy_image('bee');assert image.mode=='RGBA' and image.size==(32,32)
    assert image.getpixel((0,0))[3]==0
    BarBridge(presenter).open('home')
    opened.assert_called_once_with('home')
