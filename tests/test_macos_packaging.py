"""Mac packaging regressions: writable data, brand metadata and architecture updates."""
import hashlib
import json
import platform
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_bundle_metadata_uses_brand_and_release_version(tmp_path):
    (tmp_path / 'build_data').mkdir()
    (tmp_path / 'build_data/brand.json').write_text(
        json.dumps({'app_name': 'ZXSAssistant', 'display_name': '中新助手'}), encoding='utf-8')
    (tmp_path / 'VERSION').write_text('2.3.4rc2\n', encoding='utf-8')
    metadata = runpy.run_path(str(ROOT / 'packaging/macos_bundle.py'))['bundle_metadata']
    result = metadata(tmp_path, 'arm64')
    assert result['name'] == 'ZXSAssistant'
    assert result['plist']['CFBundleShortVersionString'] == '2.3.4'
    assert result['plist']['OpenAGCVersion'] == '2.3.4rc2'
    assert result['plist']['NSPrincipalClass'] == 'NSApplication'
    with pytest.raises(ValueError):
        metadata(tmp_path, 'universal')


def test_frozen_mac_paths_and_seeding_stay_outside_app(tmp_path, monkeypatch):
    from core import paths
    bundle = tmp_path / 'Test.app/Contents/Resources'
    (bundle / 'data').mkdir(parents=True)
    (bundle / 'data/brand.json').write_text('{"app_name":"ZXSAssistant"}', encoding='utf-8')
    (bundle / 'data/config.json').write_text('{"sandbox_mode":true}', encoding='utf-8')
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, '_MEIPASS', str(bundle), raising=False)
    monkeypatch.delenv('OPEN_AGC_DATA_DIR', raising=False)
    monkeypatch.setattr(paths.os.path, 'expanduser', lambda _: str(tmp_path / 'home'))
    expected = tmp_path / 'home/Library/Application Support/ZXSAssistant'
    assert paths.get_base_dir() == str(expected)
    paths.seed_frozen_data(str(bundle))
    assert (expected / 'data/config.json').is_file()
    assert paths.resolve_sandbox_dir() == str(expected / 'workspace')
    custom = tmp_path / 'isolated'
    monkeypatch.setenv('OPEN_AGC_DATA_DIR', str(custom))
    assert paths.get_base_dir() == str(custom)


@pytest.mark.parametrize('machine,key', [('arm64','macos-arm64'), ('x86_64','macos-x86_64')])
def test_manifest_selects_matching_mac_architecture(monkeypatch, machine, key):
    from core.auto_upgrade import AutoUpgrader
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(platform, 'machine', lambda: machine)
    upgrader = AutoUpgrader()
    upgrader._manifest_assets = {
        'macos-arm64': 'ZXSAssistant-1.0.0-macOS-arm64.dmg',
        'macos-x86_64': 'ZXSAssistant-1.0.0-macOS-x86_64.dmg',
    }
    assert upgrader._desktop_asset_name('1.0.0') == upgrader._manifest_assets[key]
    upgrader._manifest_assets = {'macos': 'Old-macOS-arm64.dmg'}
    if machine == 'x86_64':
        assert upgrader._desktop_asset_name('1.0.0') == ''


def test_release_manifest_preserves_both_mac_builds(tmp_path, monkeypatch):
    dest = tmp_path / 'release/v1.0.0'
    dest.mkdir(parents=True)
    names = [f'ZXSAssistant-1.0.0-macOS-{arch}.dmg' for arch in ('arm64', 'x86_64')]
    for name in names:
        (dest / name).write_bytes(b'dmg fixture')
    monkeypatch.setenv('AGC_ARTIFACTS_ROOT', str(tmp_path))
    subprocess.run([sys.executable, str(ROOT / 'scripts/make_release_manifest.py'), '1.0.0'], check=True)
    result = json.loads((tmp_path / 'release/version.json').read_text(encoding='utf-8'))
    digest = hashlib.sha256(b'dmg fixture').hexdigest()
    assert result['assets'] == {
        key: {'name': name, 'sha256': digest}
        for key, name in zip(('macos-arm64', 'macos-x86_64'), names)
    }
