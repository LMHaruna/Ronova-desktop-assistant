"""Asset integrity and useful startup failures without real user services."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from client_scene import SceneAssetError, SceneAssets


class StartupAssetTests(unittest.TestCase):
    def test_missing_and_corrupt_assets_keep_integrity_checks_and_identify_file(self):
        with tempfile.TemporaryDirectory() as folder:
            assets=Path(folder)/'design/client-assets';assets.mkdir(parents=True)
            relative='missing-portrait.png'
            manifest={'assets':[{'name':'portrait','file':relative,
                                 'sha256':hashlib.sha256(b'expected image').hexdigest()}]}
            (assets/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
            for content in (None,b'wrong contents'):
                if content is not None:(assets/relative).write_bytes(content)
                with self.subTest(content=content):
                    with self.assertRaisesRegex(SceneAssetError,relative):SceneAssets(folder)

    def test_startup_dialog_identifies_failed_asset_without_exposing_other_errors(self):
        import client_app
        with tempfile.TemporaryDirectory() as folder:
            asset='qwen-image-3.0/cleaned/missing-portrait.png'
            for error in (SceneAssetError('界面素材缺失或校验失败：'+asset),RuntimeError('private fixture detail')):
                with self.subTest(error=type(error).__name__), \
                        patch.object(client_app,'ROOT',Path(folder)), \
                        patch.object(sys,'argv',['client_app.py']), \
                        patch.object(client_app,'QApplication'), \
                        patch.object(client_app,'QLockFile'), \
                        patch.object(client_app,'DesktopController'), \
                        patch.object(client_app,'ClientWindow',side_effect=error), \
                        patch.object(client_app.QMessageBox,'critical') as critical:
                    self.assertEqual(client_app.main(),1)
                    message=critical.call_args.args[2]
                    if isinstance(error,SceneAssetError):self.assertIn(asset,message)
                    else:
                        self.assertIn('RuntimeError',message)
                        self.assertNotIn('private fixture detail',message)

    def test_portrait_inventory_and_client_manifest_agree_on_existing_files(self):
        root=Path(__file__).resolve().parents[1]
        base=root/'design/character-final'
        inventory=json.loads((base/'relationship-portraits.json').read_text(encoding='utf-8'))
        manifest=json.loads((root/'design/client-assets/manifest.json').read_text(encoding='utf-8'))
        entries={entry['name']:entry for entry in manifest['assets']}
        self.assertEqual(len(inventory['portraits']),5)
        for portrait in inventory['portraits']:
            with self.subTest(stage=portrait['tier']):
                path=(base/portrait['file']).resolve()
                client=entries[manifest['relationship_portraits'][portrait['tier']]]
                client_path=(root/'design/client-assets'/client['file']).resolve()
                self.assertEqual(path,client_path)
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),portrait['sha256'])
                self.assertEqual(client['sha256'],portrait['sha256'])


if __name__=='__main__':unittest.main()
