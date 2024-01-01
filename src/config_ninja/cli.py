"""Create `config-ninja`_'s CLI with `typer`_.

.. include:: cli.md

.. note:: `typer`_ does not support `from __future__ import annotations` as of 2023-12-31

.. _config-ninja: https://bryant-finney.github.io/config-ninja/config_ninja.html
.. _typer: https://typer.tiangolo.com/
"""
import asyncio
import contextlib
import dataclasses
import logging
import typing
from pathlib import Path

import jinja2
import pyspry
import typer
import yaml
from rich import print  # pylint: disable=redefined-builtin

import config_ninja
from config_ninja.backend import DUMPERS, Backend, FormatT, dumps, loads
from config_ninja.contrib import get_backend

try:
    from typing import Annotated, TypeAlias  # type: ignore[attr-defined,unused-ignore]
except ImportError:  # pragma: no cover
    from typing_extensions import Annotated, TypeAlias  # type: ignore[assignment]

__all__ = [
    'app',
    'apply',
    'BackendController',
    'DestSpec',
    'get',
    'main',
    'monitor',
    'print_self_config',
    'version',
]

logger = logging.getLogger(__name__)

app_kwargs: typing.Dict[str, typing.Any] = {
    'context_settings': {'help_option_names': ['-h', '--help']},
    'no_args_is_help': True,
    'rich_markup_mode': 'rich',
}

app = typer.Typer(**app_kwargs)
"""The root `typer`_ application.

.. _typer: https://typer.tiangolo.com/
"""

self_app = typer.Typer(**app_kwargs)

app.add_typer(
    self_app, name='self', help="Operate on [bold blue]config-ninja[/]'s own configuration file."
)

ActionType = typing.Callable[[str], typing.Any]
KeyAnnotation: TypeAlias = Annotated[
    str,
    typer.Argument(help='The key of the configuration object to retrieve', show_default=False),
]
PollAnnotation: TypeAlias = Annotated[
    typing.Optional[bool],
    typer.Option(
        '-p',
        '--poll',
        help='Enable polling; print the configuration on changes.',
        show_default=False,
    ),
]
SettingsAnnotation: TypeAlias = Annotated[
    typing.Optional[Path],
    typer.Option(
        '-c',
        '--config',
        help="Path to [bold blue]config-ninja[/]'s own configuration file.",
        show_default=False,
    ),
]


def version_callback(ctx: typer.Context, value: typing.Optional[bool] = None) -> None:
    """Print the version of the package."""
    if ctx.resilient_parsing:  # pragma: no cover  # this is for tab completions
        return

    if value:
        print(config_ninja.__version__)
        raise typer.Exit()


VersionAnnotation = Annotated[
    typing.Optional[bool],
    typer.Option(
        '-v',
        '--version',
        callback=version_callback,
        show_default=False,
        is_eager=True,
        help='Print the version and exit.',
    ),
]


@contextlib.contextmanager
def handle_key_errors(objects: typing.Dict[str, typing.Any]) -> typing.Iterator[None]:
    """Handle KeyError exceptions within the managed context."""
    try:
        yield
    except KeyError as exc:  # pragma: no cover
        print(f'[red]ERROR[/]: Missing key: [green]{exc.args[0]}[/]\n')
        print(yaml.dump(objects))
        raise typer.Exit(1) from exc


@dataclasses.dataclass
class DestSpec:
    """Container for the destination spec parsed from `config-ninja`_'s own configuration file.

    .. _config-ninja: https://bryant-finney.github.io/config-ninja/config_ninja.html
    """

    path: Path
    """Write the configuration file to this path."""

    format: typing.Union[FormatT, jinja2.Template]
    """Specify the format of the configuration file to write.

    This property is either a `config_ninja.backend.FormatT` or a `jinja2.environment.Template`:
    - if `config_ninja.backend.FormatT`, the identified `config_ninja.backend.DUMPERS` will be used
        to serialize the configuration object
    - if `jinja2.environment.Template`, this template will be used to render the configuration file
    """

    @property
    def is_template(self) -> bool:
        """Whether the destination uses a Jinja2 template."""
        return isinstance(self.format, jinja2.Template)


class BackendController:
    """Define logic for initializing a backend from settings and interacting with it."""

    backend: Backend
    """The backend instance to use for retrieving configuration data."""

    dest: DestSpec
    """Parameters for writing the configuration file."""

    key: str
    """The key of the backend in the settings file"""

    settings: pyspry.Settings
    """`config-ninja`_'s own configuration settings

    .. _config-ninja: https://bryant-finney.github.io/config-ninja/config_ninja.html
    """

    src_format: FormatT
    """The format of the configuration object in the backend.

    The named `config_ninja.backend.LOADERS` function will be used to deserialize the configuration
    object from the backend.
    """

    def __init__(self, settings: typing.Optional[pyspry.Settings], key: str) -> None:
        """Parse the settings to initialize the backend.

        .. note::
            The `settings` parameter is required and cannot be `None` (`typer.Exit(1)` is raised if
            it is). This odd handling is due to the statement in `config_ninja.cli.main` that sets
            `ctx.obj['settings'] = None`, which is needed to allow the `self` commands to function
            without a settings file.
        """
        if not settings:  # pragma: no cover
            print('[red]ERROR[/]: Could not load settings.')
            raise typer.Exit(1)

        assert settings is not None  # noqa: S101  # 👈 for static analysis

        self.settings, self.key = settings, key

        self.src_format, self.backend = self._init_backend()
        self.dest = self._get_dest()

    def _get_dest(self) -> DestSpec:
        """Read the destination spec from the settings file."""
        objects = self.settings.OBJECTS
        with handle_key_errors(objects):
            dest = objects[self.key]['dest']
            path = Path(dest['path'])
            if dest['format'] in DUMPERS:
                fmt: FormatT = dest['format']  # type: ignore[assignment]
                return DestSpec(format=fmt, path=path)

            template_path = Path(dest['format'])

        loader = jinja2.FileSystemLoader(template_path.parent)
        env = jinja2.Environment(autoescape=jinja2.select_autoescape(default=True), loader=loader)

        return DestSpec(path=path, format=env.get_template(template_path.name))

    def _init_backend(self) -> typing.Tuple[FormatT, Backend]:
        """Get the backend for the specified configuration object."""
        objects = self.settings.OBJECTS

        with handle_key_errors(objects):
            source = objects[self.key]['source']
            backend_class: typing.Type[Backend] = get_backend(source['backend'])
            fmt = source.get('format', 'raw')
            if source.get('new'):
                backend = backend_class.new(**source['new']['kwargs'])
            else:
                backend = backend_class(**source['init']['kwargs'])

        return fmt, backend

    def _do(self, action: ActionType, data: typing.Dict[str, typing.Any]) -> None:
        if self.dest.is_template:
            assert isinstance(self.dest.format, jinja2.Template)  # noqa: S101  # 👈 for static analysis
            action(self.dest.format.render(data))
        else:
            fmt: FormatT = self.dest.format  # type: ignore[assignment]
            action(dumps(fmt, data))

    def get(self) -> None:
        """Retrieve and print the value of the configuration object."""
        data = loads(self.src_format, self.backend.get())
        self._do(print, data)

    async def aget(self) -> None:
        """Poll to retrieve the latest configuration object, and print on each update."""
        async for content in self.backend.poll():
            data = loads(self.src_format, content)
            self._do(print, data)

    def write(self) -> None:
        """Retrieve the latest value of the configuration object, and write to file."""
        data = loads(self.src_format, self.backend.get())
        self._do(self.dest.path.write_text, data)

    async def awrite(self) -> None:
        """Poll to retrieve the latest configuration object, and write to file on each update."""
        async for content in self.backend.poll():
            data = loads(self.src_format, content)
            self._do(self.dest.path.write_text, data)


@app.command()
def get(ctx: typer.Context, key: KeyAnnotation, poll: PollAnnotation = False) -> None:
    """Print the value of the specified configuration object."""
    ctrl = BackendController(ctx.obj['settings'], key)

    if poll:
        asyncio.run(ctrl.aget())
    else:
        ctrl.get()


@app.command()
def apply(ctx: typer.Context, key: KeyAnnotation, poll: PollAnnotation = False) -> None:
    """Apply the specified configuration to the system."""
    ctrl = BackendController(ctx.obj['settings'], key)
    ctrl.dest.path.parent.mkdir(parents=True, exist_ok=True)

    if poll:
        asyncio.run(ctrl.awrite())
    else:
        ctrl.write()


@app.command()
def monitor(ctx: typer.Context) -> None:
    """Apply all configuration objects to the filesystem, and poll for changes."""
    settings: pyspry.Settings = ctx.obj['settings']
    controllers = [BackendController(settings, key) for key in settings.OBJECTS]
    for ctrl in controllers:
        ctrl.dest.path.parent.mkdir(parents=True, exist_ok=True)

    async def poll_all() -> None:
        await asyncio.gather(*[ctrl.awrite() for ctrl in controllers])

    asyncio.run(poll_all())


@self_app.command(name='print')
def print_self_config(ctx: typer.Context) -> None:
    """Print the configuration file."""
    if settings := ctx.obj['settings']:
        print(yaml.dump(settings.OBJECTS))
    else:
        print('[yellow]WARNING[/]: No settings file found.')


@app.command()
def version(ctx: typer.Context) -> None:
    """Print the version and exit."""
    version_callback(ctx, True)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    settings_file: SettingsAnnotation = None,
    version: VersionAnnotation = None,  # pylint: disable=unused-argument,redefined-outer-name
) -> None:
    """Manage operating system configuration files based on data in the cloud."""
    ctx.ensure_object(dict)

    try:
        settings_file = settings_file or config_ninja.resolve_settings_path()
    except FileNotFoundError as exc:
        message = "[yellow]WARNING[/]: Could not find [bold blue]config-ninja[/]'s settings file"
        if len(exc.args) > 1:
            message += ' at any of the following locations:\n' + '\n'.join(
                f'    {p}' for p in exc.args[1]
            )
        print(message)
        ctx.obj['settings'] = None

    else:
        ctx.obj['settings'] = config_ninja.load_settings(settings_file)

    if not ctx.invoked_subcommand:  # pragma: no cover
        print(ctx.get_help())


logger.debug('successfully imported %s', __name__)