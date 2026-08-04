from pathlib import Path

import typer
from dotenv import load_dotenv

from papyrus.commands.check import app as check_app
from papyrus.commands.convert import app as convert_app
from papyrus.commands.defer import app as defer_app
from papyrus.commands.discard import app as discard_app
from papyrus.commands.embed import app as embed_app
from papyrus.commands.index import app as index_app
from papyrus.commands.ingest import app as ingest_app
from papyrus.commands.init import app as init_app
from papyrus.commands.inbox import app as inbox_app
from papyrus.commands.revisit import app as revisit_app
from papyrus.commands.search import app as search_app
from papyrus.commands.notify import app as notify_app
from papyrus.commands.store import app as store_app
from papyrus.commands.update import app as update_app

app = typer.Typer(no_args_is_help=True)

app.add_typer(init_app)
app.add_typer(ingest_app)
app.add_typer(discard_app)
app.add_typer(defer_app)
app.add_typer(revisit_app)
app.add_typer(convert_app)
app.add_typer(update_app)
app.add_typer(check_app)
app.add_typer(embed_app)
app.add_typer(index_app)
app.add_typer(search_app)
app.add_typer(notify_app)
app.add_typer(store_app, name="store", help="Manage PDF external storage")
app.add_typer(inbox_app, name="inbox", help="Manage pdfinbox/ directory")


def _load_dotenv_for_kb():
    env_path = Path.cwd() / ".env"
    for p in [Path.cwd(), *Path.cwd().parents]:
        if (p / ".env").exists():
            env_path = p / ".env"
            break
    load_dotenv(dotenv_path=env_path)


def main():
    _load_dotenv_for_kb()
    app()


if __name__ == "__main__":
    main()
