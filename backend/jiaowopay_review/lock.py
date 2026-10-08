"""One running workbench per data directory; OS releases the lock on exit."""
import os
from pathlib import Path
from .store import ReviewError


class DataDirectoryLock:
    def __init__(self,root):
        root=Path(root)
        root.mkdir(parents=True,exist_ok=True)
        self.file=(root/'workbench.lock').open('a+b')
        if self.file.seek(0,2)==0:
            self.file.write(b'0');self.file.flush()
        self.file.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise ReviewError('此数据目录已有工作台在运行，请使用已有窗口，或关闭它后重新启动。') from None

    def close(self):
        self.file.close()
