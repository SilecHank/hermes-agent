"""Offline child process: project imports may resolve only inside the built artifact."""
import importlib
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import pwd
import sys

artifact = Path(sys.argv[1]).resolve()
private = Path(sys.argv[2]).resolve()
project_names = set(json.loads(sys.argv[3]))


class ArtifactOnly(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] not in project_names:
            return None
        spec = importlib.machinery.PathFinder.find_spec(
            fullname, [str(artifact)] if path is None else path
        )
        if spec is None:
            raise ModuleNotFoundError(f'serving artifact missing {fullname}', name=fullname)
        if spec.origin and spec.origin not in {'built-in', 'frozen'}:
            if not Path(spec.origin).resolve().is_relative_to(artifact):
                raise ImportError(f'project module escaped serving artifact: {fullname}')
        return spec


sys.meta_path.insert(0, ArtifactOnly())
sys.path.insert(0, str(artifact))
real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
readable = (artifact, private, Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve())


def offline_guard(event, args):
    if event in {
        'socket.connect', 'socket.bind', 'socket.getaddrinfo', 'socket.sendto',
        'subprocess.Popen', 'os.system', 'os.exec', 'os.posix_spawn',
    }:
        raise RuntimeError(f'offline import blocked {event}')
    if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
        path = Path(os.fsdecode(args[0])).resolve()
        if path.is_relative_to(real_home) and not any(path.is_relative_to(p) for p in readable):
            raise RuntimeError('offline import cannot read the real home')


sys.addaudithook(offline_guard)
importlib.import_module(sys.argv[4])
for name in ('gateway.ivd_runtime', 'gateway.ivd_dispatcher', 'gateway.ivd_parameter_scope'):
    importlib.import_module(name)
origins = {}
for name, module in list(sys.modules.items()):
    if name.split('.')[0] in project_names and getattr(module, '__file__', None):
        path = Path(module.__file__).resolve()
        if not path.is_relative_to(artifact):
            raise ImportError(f'project module escaped serving artifact: {name}')
        origins[name] = str(path.relative_to(artifact))
print(json.dumps({'status': 'ready', 'entrypoint': sys.argv[4], 'origins': origins}))
