"""CLI entry point for {{project_name}}."""

import click

from . import __version__


@click.group()
@click.version_option(version=__version__)
def main():
    """{{project_description}}"""
    pass


@main.command()
@click.argument("name", default="World")
def hello(name: str):
    """Say hello to NAME."""
    click.echo(f"Hello, {name}!")


if __name__ == "__main__":
    main()
