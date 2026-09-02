"""The `recoup` CLI.

Commands are added as the phases that need them land -- `audit verify` in
Phase 2, `bench run` in Phase 3. Phase 0 ships the entry point itself and a
`serve` command, so the API is runnable without reaching into uvicorn directly.
"""

import typer
import uvicorn

app = typer.Typer(
    name="recoup", help="Recoup -- revenue recovery control plane.", no_args_is_help=True
)


@app.command()
def version() -> None:
    """Print the installed version."""
    from importlib.metadata import version as pkg_version

    typer.echo(pkg_version("recoup"))


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", help="Bind address."),  # noqa: S104 -- container-bound by design
    port: int = typer.Option(8000, help="Bind port."),
    reload: bool = typer.Option(False, help="Autoreload on code change (development only)."),
) -> None:
    """Run the API service."""
    uvicorn.run("recoup.api.app:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    app()
