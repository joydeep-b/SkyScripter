import json
import os
import tempfile

from sky_scripter.config import Config, DEFAULTS


def test_defaults_loaded():
    cfg = Config(path='/nonexistent/path.json')
    assert cfg['capture']['gain'] == 56
    assert cfg['devices']['mount'] == 'ZWO AM5'
    assert cfg['cooler']['target_temp'] == -10.0
    assert cfg['site']['latitude'] is None


def test_user_override():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump({"site": {"latitude": 30.5}, "cooler": {"target_temp": -20}}, f)
        path = f.name
    try:
        cfg = Config(path=path)
        assert cfg['site']['latitude'] == 30.5
        assert cfg['cooler']['target_temp'] == -20
        # Non-overridden values still have defaults
        assert cfg['devices']['mount'] == 'ZWO AM5'
        assert cfg['site']['longitude'] is None
    finally:
        os.unlink(path)


def test_nested_get():
    cfg = Config(path='/nonexistent/path.json')
    assert cfg.get('site', 'latitude') is None
    assert cfg.get('devices', 'mount') == 'ZWO AM5'
    assert cfg.get('nonexistent', 'key', default=42) == 42
    assert cfg.get('capture', 'nonexistent', default='x') == 'x'


def test_save_and_reload():
    with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
        path = f.name
    try:
        Config.generate_default(path)
        cfg = Config(path=path)
        assert cfg['capture']['gain'] == 56
        cfg._data['capture']['gain'] = 100
        cfg.save(path)
        cfg2 = Config(path=path)
        assert cfg2['capture']['gain'] == 100
    finally:
        os.unlink(path)


def test_deep_merge_preserves_unset_keys():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump({"capture": {"gain": 100}}, f)
        path = f.name
    try:
        cfg = Config(path=path)
        assert cfg['capture']['gain'] == 100
        assert cfg['capture']['offset'] == 20
        assert cfg['capture']['mode'] == 5
    finally:
        os.unlink(path)
