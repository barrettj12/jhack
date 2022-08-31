import json
import os
import re
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from functools import singledispatch, partial
from io import StringIO
from os.path import expanduser
from pathlib import Path
from typing import Dict, Type, Optional, Any, Callable, Literal, Union, Iterable

import typer
import yaml
from logger import logger
from ops.storage import SQLiteStorage
from rich.align import Align
from rich.console import RenderableType, Console
from rich.live import Live
from rich.table import Table

from jhack.helpers import JPopen

Adapter = Callable[[Any], RenderableType]
_Color = Optional[
    Literal["auto", "standard", "256", "truecolor", "windows", "no"]]
unit_re = re.compile(r"^(?P<unit_name>\S+)\/(?P<unit_number>\d+)$")

OF_STORAGE_HANDLE_PATH = "StoredStateData[_stored]"


def _is_file(target: str):
    pth = Path(target)
    return pth.exists() and pth.is_file()


def _is_unit(target: str):
    return bool(unit_re.match(target))


@singledispatch
def view(obj: Any):
    return repr(obj)


@view.register
def _view(obj: dict):
    return view_dict(obj)


def view_dict(obj: Dict[str, Any]):
    table = Table()
    table.add_column('key')
    table.add_column('value')
    for k, v in obj.items():
        table.add_row(repr(k), repr(v))
    return table


class Store(ABC):
    def __init__(self, path: str):
        self._path = path

    @abstractmethod
    def list_snapshots(self) -> Iterable[str]: ...

    @abstractmethod
    def load_snapshot(self, handle: str) -> Any: ...

    @abstractmethod
    def close(self) -> None: ...


class SQLiteStore(Store):
    def __init__(self, path: str):
        super().__init__(path)
        self._db = SQLiteStorage(path)

    def list_snapshots(self):
        return self._db.list_snapshots()

    def load_snapshot(self, handle: str):
        return self._db.load_snapshot(handle)

    def close(self):
        return self._db.close()


class YAMLStore(Store):
    def __init__(self, path: str, is_snapshot=lambda s: s != '#notices#'):
        super().__init__(path)
        self._db = yaml.safe_load(Path(path).read_text('utf-8'))
        self._is_snapshot = is_snapshot

    def list_snapshots(self):
        yield from filter(self._is_snapshot, self._db)

    def load_snapshot(self, handle: str):
        return yaml.safe_load(self._db[handle])

    def close(self):
        pass


class StorageView:
    _builtin_adapters: Dict[str, Adapter] = {
        'StoredStateData[_stored]': view_dict,
    }

    _builtin_path_names = {
        'StoredStateData[_stored]': "(OF storage)",
    }

    def __init__(self, adapters=None, color: str = 'auto', live: bool = False,
                 filter_re: str = None, include_of_storage: bool = False,
                 reader: str = 'sqlite'):
        self.store = None
        self._filter_re = re.compile(filter_re) if filter_re else None
        self._user_adapters: Optional[Dict[str, Adapter]] = exec(adapters,
                                                                 globals()) if adapters else {}
        self._include_of_storage = include_of_storage
        self._reader = reader

        if color == "no":
            color = None

        self.console = console = Console(color_system=color)
        if live:
            live = Live(console=console)
            live.start()
            live.update("Fetching state...", refresh=True)

        else:
            live = False
        self.live: Optional[Live] = live

    def get_store(self, file: str) -> Store:
        reader = self._reader
        if reader == 'sqlite':
            logger.debug('initializing SQLiteStore reader')
            return SQLiteStore(file)
        elif reader == 'yaml':
            logger.debug('initializing YAMLStore reader')
            return YAMLStore(file)
        else:
            raise RuntimeError()

    def _render_snapshot_content(self, snapshot_name: str,
                                 snapshot_content: Any):
        if self._user_adapters and (
                adapter := self._user_adapters.get(snapshot_name)):
            logger.info(f'found user adapter for {snapshot_name}: {adapter}')
        elif adapter := self._builtin_adapters.get(snapshot_name):
            logger.info(
                f'found builtin adapter for {snapshot_name}: {adapter}')
        else:
            logger.debug(
                f'no specific path adapter found for {snapshot_name}: using builtin view')
            adapter = view
        return adapter(snapshot_content)

    def _get_size(self, obj: Any) -> str:
        def get_size(obj, seen=None):
            """Recursively finds size of objects
            source: https://goshippo.com/blog/measure-real-size-any-python-object/
            """
            size = sys.getsizeof(obj)
            if seen is None:
                seen = set()
            obj_id = id(obj)
            if obj_id in seen:
                return 0
            # Important mark as seen *before* entering recursion to gracefully handle
            # self-referential objects
            seen.add(obj_id)
            if isinstance(obj, dict):
                size += sum([get_size(v, seen) for v in obj.values()])
                size += sum([get_size(k, seen) for k in obj.keys()])
            elif hasattr(obj, '__dict__'):
                size += get_size(obj.__dict__, seen)
            elif hasattr(obj, '__iter__') and not isinstance(obj, (
                    str, bytes, bytearray)):
                size += sum([get_size(i, seen) for i in obj])
            return size

        try:
            return str(get_size(obj)) + 'b'
        except (AttributeError, TypeError, Exception):
            return '???'

    def _render_metadata(self, name: str, snapshot: str, obj: Any):
        t = Table(box=None)
        t.add_column()
        t.add_column()
        t.add_row('handle path:', snapshot)
        t.add_row('size:', self._get_size(obj))
        return t

    def _render_snapshot(self, snapshot_name: str):
        if not self.store:
            raise RuntimeError('no store loaded')

        snap_content = self.store.load_snapshot(snapshot_name)
        rendered = self._render_snapshot_content(snapshot_name, snap_content)
        return rendered, snap_content

    def _get_name(self, snapshot: str):
        """Derive user-friendly name from snapshot name (ops.Handle path)."""
        # E.g. TraefikIngressCharm/StoredStateData[_stored]
        if snapshot in self._builtin_path_names:
            return self._builtin_path_names[snapshot]

        key_re = re.compile(r"\[([^\d\W]\w*)\]")
        try:
            key = key_re.findall(snapshot)[0]
            owners = snapshot.split('/')[:-1]
            return '.'.join(owners + [key])
        except Exception as e:
            logger.debug(f"failure processing snapshot {snapshot}: {e}")
            return snapshot

    def render(self, store_path: Union[str, Path]):
        logger.info(f"loading storage from path: {store_path}")

        try:
            self.store = store = self.get_store(store_path)
        except Exception as e:
            raise RuntimeError(
                f'Failed to parse SQLite storage file {store_path}: {e}') from e

        table = Table()
        contents = []
        metadata = []
        snapshots = tuple(store.list_snapshots())

        for snapshot in snapshots:
            if self._filter_re and not self._filter_re.match(snapshot):
                logger.debug(f're-filter: skipped {snapshot}')
                continue
            if not self._include_of_storage and snapshot == OF_STORAGE_HANDLE_PATH:
                logger.debug(f'skipped of storage')
                continue

            name = self._get_name(snapshot)
            table.add_column(name)
            contents_, raw_data = self._render_snapshot(snapshot)
            contents.append(contents_)
            metadata_ = self._render_metadata(name, snapshot, raw_data)
            metadata.append(metadata_)

        if metadata or contents:
            table.add_row(*metadata)
            table.add_row(*contents)
            rendered = Align.center(table)
        else:
            rendered = f"No snapshots found in {store_path}."

        if self.live:
            self.live.update(rendered)
        else:
            self.console.print(rendered)

        self.store.close()

    def quit(self):
        if self.live:
            self.live.refresh()
            self.live.stop()


def get_local_storage(unit_name: str):
    unit_name_sane = unit_name.replace('/', '-')
    cmd = f"juju scp --container charm {unit_name}:" \
          f"/var/lib/juju/agents/unit-{unit_name_sane}/charm/" \
          f".unit-state.db ".split()

    while True:
        # todo: does this work in a snap?
        with tempfile.NamedTemporaryFile(suffix='.db',
                                         prefix='unit-state-',
                                         dir=expanduser('~')) as tf:
            cmd.append(tf.name)

            proc = JPopen(cmd)
            proc.wait(10)

            if not proc.returncode == 0:
                logger.error(
                    f'failed to fetch db; command {cmd} exited with {proc.returncode}')
                print(
                    f'failed to fetch db; aborting. {proc.stderr.read()}')
                return

            tf_file = Path(tf.name)

            yield tf.name

            tf_file.unlink()


def get_controller_storage(unit_name: str):
    cmd = f"juju exec --unit {unit_name} -- state-get".split()

    while True:
        # todo: does this work in a snap?
        with tempfile.NamedTemporaryFile(suffix='.db',
                                         prefix='controller-state-',
                                         dir=expanduser('~')) as tf:
            proc = JPopen(cmd)
            proc.wait(10)

            if not proc.returncode == 0:
                logger.error(
                    f'failed to fetch db; command {cmd} exited with {proc.returncode}')
                print(
                    f'failed to fetch db; aborting. {proc.stderr.read()}')
                return

            tf_file = Path(tf.name)
            tf_file.write_bytes(proc.stdout.read())

            yield tf.name

            tf_file.unlink()


def _show_stored(target: str,
                 filter_re: str = None,
                 adapters: str = None,
                 use_controller_storage: bool = False,
                 color: _Color = "auto",
                 watch: bool = False,
                 include_of_storage: bool = False,
                 refresh_rate=.5):
    """Execute the _show_stored script inside the juju unit."""
    is_file = _is_file(target)
    is_unit = _is_unit(target)

    if not (is_file or is_unit):
        print(f'Unknown target type: {target}. '
              f'Provide either a unit name (e.g. `my-charm/0`) or a path to '
              f'a database file (e.g. `./unit-state.db`)')
        return
    if is_file and is_unit:
        logger.warning(f'Ambiguous target type: {target} is both a file and a '
                       f'valid unit name. We know the file to exist, '
                       f'so we will show that one.')
        is_unit = False

    if is_unit:
        if use_controller_storage:
            get_db = partial(get_controller_storage, unit_name=target)

        else:
            get_db = partial(get_local_storage, unit_name=target)

    else:  # file
        def get_db():
            yield from iter((target,))

    viewer = StorageView(adapters=adapters, color=color, live=watch,
                         filter_re=filter_re,
                         reader='yaml' if use_controller_storage else 'sqlite',
                         include_of_storage=include_of_storage)
    try:
        for db in get_db():
            viewer.render(db)

            if watch:
                time.sleep(refresh_rate)
            else:
                return

    except KeyboardInterrupt:
        print('exiting')

    finally:
        if watch:
            viewer.quit()


def show_stored(
        target: str = typer.Argument(
            ...,
            help="Target unit or database file."),
        use_controller_storage: bool = typer.Option(
            False, '--cs', '--controller-storage',
            help="Whether the target _unit_ uses controller storage "
                 "instead of local storage.", is_flag=True),
        filter_: Optional[str] = typer.Option(
            None, '-f', '--filter',
            help="State prefix regex for filtering which states should be shown."),
        adapters: Optional[str] = typer.Option(
            None, '-a', '--adapters',
            help="Path to a python file containing a Dict[str: Adapter] mapping. See docs for more info."),
        color: Optional[str] = typer.Option(
            "auto",
            "-c", "--color",
            help="Color scheme to adopt. Supported options: "
                 "['auto', 'standard', '256', 'truecolor', 'windows']"
                 "no: disable colors entirely.",
        ),
        watch: bool = typer.Option(False, '-w', '--watch',
                                   help="Keep watching for changes.",
                                   is_flag=True),
        include_of_storage: bool = typer.Option(False, '-o', '--of-storage',
                                                help="Also show Operator Framework storage "
                                                     "(StoredStateData[_stored]).",
                                                is_flag=True),
        refresh_rate: float = typer.Option(.5, '-r', '--refresh-rate',
                                           help="How often the stored state view should be updated.")
):
    return _show_stored(target=target,
                        filter_re=filter_,
                        adapters=adapters,
                        use_controller_storage=use_controller_storage,
                        color=color,
                        watch=watch,
                        include_of_storage=include_of_storage,
                        refresh_rate=refresh_rate)


if __name__ == '__main__':
    _show_stored('trfk/0', watch=False)
    # _show_stored('/home/pietro/hacking/jhack/jhack/tests/utils/show_stored_mocks/trfk-0.dbdump')  # noqa
