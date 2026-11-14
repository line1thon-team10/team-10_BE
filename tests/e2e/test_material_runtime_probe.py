"""Independent subprocess checks; never invokes Docker or an external provider."""
import os
from pathlib import Path
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def test_deploy_probe_rejects_broken_lazy_material_service_before_stopping_writers():
    source = (ROOT / 'deploy/apply.sh').read_text()
    probe = source.split("material_probe='", 1)[1].split("'\nif docker run", 1)[0]
    bootstrap = '''
import importlib.abc
import sys
class BrokenMaterialRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'app.services.material_analysis':
            raise ModuleNotFoundError('broken runtime dependency', name='missing_material_dependency')
sys.meta_path.insert(0, BrokenMaterialRuntime())
'''
    result = subprocess.run([sys.executable, '-c', bootstrap + probe], cwd=ROOT,
        capture_output=True, text=True, timeout=10)
    assert result.returncode not in (0, 42), 'A present but broken material runtime must fail preflight'


