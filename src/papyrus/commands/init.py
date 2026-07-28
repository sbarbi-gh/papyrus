import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from papyrus.core.constants import SCHEMA_VERSION

app = typer.Typer()
console = Console()


@app.command()
def init(
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite existing files and skip intent.md check"),
    ] = False,
):
    """Initialize a new KB tree in the current directory."""
    cwd = Path.cwd()

    intent = cwd / "intent.md"
    if not intent.exists() and not force:
        console.print(
            "[red]intent.md not found.[/red] "
            "Write the premise (intent.md) before initializing, or use --force."
        )
        raise typer.Exit(code=2)

    dirs = ["db", "concepts", "updates", "staging", "embeddings"]
    conflicts = [d for d in dirs if (cwd / d).exists() and not force]
    if conflicts:
        console.print(f"[red]Existing directories found: {', '.join(conflicts)}[/red]")
        console.print("Use --force to overwrite.")
        raise typer.Exit(code=1)

    for d in dirs:
        (cwd / d).mkdir(parents=True, exist_ok=True)

    (cwd / "events.jsonl").touch(exist_ok=force)

    index = {
        "schema_version": SCHEMA_VERSION,
        "concepts": {},
        "citekey_index": {},
        "pmid_to_citekey": {},
    }
    with open(cwd / "concepts" / "index.json", "w") as f:
        json.dump(index, f, indent=2)
        f.write("\n")

    method_toml = f'''schema_version = "{SCHEMA_VERSION}"

[settings]
db_path = "./db"
primary_db = "./db"
chunk_size = 60

[fulltext]
max_attempts = 5
backoff = "exponential"
keep_on_wishlist = true

[relevance]
# TODO: derive from intent.md

[source_conversion]
engine = "marker"
fallback = "pymupdf"
max_pages = 100
image_handling = "reference"

[chunking]
method = "section"
target_bytes = 2048
overlap_bytes = 256
max_chunks_per_article = 80

[embeddings]
model = "text-embedding-3-small"
dimensions = 1536
provider = "openai"
batch_size = 50

[distillation]
[concepts]
[search]

[notifications]
enabled = false
provider = "resend"
from = ""
to = []
'''
    with open(cwd / "method.toml", "w") as f:
        f.write(method_toml)

    env_example = """# papyrus — API keys (copy to .env and fill in)
# .env is git-ignored; never commit it
OPENAI_API_KEY=sk-...
NCBI_API_KEY=
RESEND_API_KEY=
"""
    with open(cwd / ".env.example", "w") as f:
        f.write(env_example)

    console.print(f"[green]Initialized KB tree (schema {SCHEMA_VERSION})[/green]")
    console.print("  db/ concepts/ updates/ staging/ embeddings/")
    console.print("  events.jsonl  concepts/index.json  method.toml")
    console.print("  [dim].env.example — copy to .env and set your API keys[/dim]")
    if not intent.exists():
        console.print("  [yellow]intent.md NOT FOUND -- create it before running update[/yellow]")
