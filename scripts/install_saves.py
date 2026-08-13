#!/usr/bin/env python3
"""Install the test saves into the Civ 6 save directory.

Copies .Civ6Save files from tests/data/saves/ into the platform-specific
Civilization VI save directory so they can be loaded via load_game().
Needed before running `pytest -m live` or recording game traffic.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import typer

sys.path.insert(0, "src")
from civ_mcp.game_launcher import SINGLE_SAVE_DIR

SAVES_SRC = Path(__file__).resolve().parent.parent / "tests" / "data" / "saves"

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def main(
    force: bool = typer.Option(
        False, "--force", help="Overwrite saves that already exist."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would happen without copying."
    ),
) -> None:
    """Copy the test saves into Civ 6's save directory."""
    if not SINGLE_SAVE_DIR:
        typer.secho(
            "Could not determine the Civ 6 save directory for this platform.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)

    dest = Path(SINGLE_SAVE_DIR)
    saves = sorted(SAVES_SRC.glob("*.Civ6Save"))

    if not saves:
        typer.secho(f"No .Civ6Save files found in {SAVES_SRC}", fg=typer.colors.RED)
        raise typer.Exit(1)

    typer.echo(f"Source:      {SAVES_SRC}")
    typer.echo(f"Destination: {dest}")
    typer.echo(f"Saves found: {len(saves)}\n")

    if not dry_run:
        os.makedirs(dest, exist_ok=True)

    copied = skipped = 0
    for src in saves:
        dst = dest / src.name
        if dst.exists() and not force:
            typer.echo(f"  SKIP  {src.name}  (already exists)")
            skipped += 1
        elif dry_run:
            typer.echo(f"  COPY  {src.name}  (dry run)")
            copied += 1
        else:
            shutil.copy2(src, dst)
            typer.echo(f"  COPY  {src.name}")
            copied += 1

    typer.echo(f"\nDone: {copied} copied, {skipped} skipped, {len(saves)} total")
    if dry_run:
        typer.echo("(dry run — no files were actually copied)")


if __name__ == "__main__":
    app()
