"""Installed root launcher; shared by menu, subprocesses and login startup."""
from pathlib import Path
import runpy
import sys

sys.dont_write_bytecode = True
root = Path(__file__).resolve().parent
if sys.version_info[:2] not in ((3, 12), (3, 13)):
    sys.exit('此安装包支持 Ubuntu 24.04 或 Debian 13 的系统 Python。')
sys.path[:0] = [str(root/'vendor'/f'python{sys.version_info.major}.{sys.version_info.minor}'),
                str(root/'vendor/common'), str(root)]
runpy.run_path(str(root/'app_entry.py'), run_name='__main__')
