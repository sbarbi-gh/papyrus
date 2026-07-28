import mistune
import os
from pathlib import Path
from typing import Annotated, Optional

import httpx
import typer
from rich.console import Console

from papyrus.core.config import load_config, locate_kb_root
from papyrus.core.events import write_event

app = typer.Typer()
console = Console()

RESEND_API = "https://api.resend.com/emails"


def _digests(kb_root: Path) -> list[Path]:
    updates_dir = kb_root / "updates"
    if not updates_dir.exists():
        return []
    return sorted(updates_dir.glob("*.md"), key=lambda p: p.name, reverse=True)


def _subject_from_digest(path: Path) -> str:
    content = path.read_text().strip()
    kb_name = path.parent.parent.name
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("# "):
            return f"[{kb_name}] {stripped[2:]}"
    return f"[{kb_name}] Digest {path.stem}"


@app.command()
def notify(
    digest: Annotated[
        Optional[str],
        typer.Argument(help="Digest timestamp or filename to send"),
    ] = None,
    latest: Annotated[
        bool,
        typer.Option("--latest", "-l", help="Send the most recent digest"),
    ] = False,
    list_digests: Annotated[
        bool,
        typer.Option("--list", help="List available digests"),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Preview email without sending"),
    ] = False,
):
    """Send a digest via email (Resend)."""
    kb_root = locate_kb_root()
    config = load_config(kb_root)
    notifications = config.get("notifications", {})

    if not notifications.get("enabled"):
        console.print("[red]Notifications are not enabled.[/red]")
        console.print("Set [notifications].enabled = true in method.toml")
        raise typer.Exit(code=1)

    updates = _digests(kb_root)

    if list_digests:
        if not updates:
            console.print("[yellow]No digests found in updates/[/yellow]")
            return
        console.print("[bold]Available digests:[/bold]")
        for p in updates:
            console.print(f"  {p.name}")
        return

    if latest:
        if not updates:
            console.print("[yellow]No digests found in updates/[/yellow]")
            raise typer.Exit(code=1)
        digest_path = updates[0]
    elif digest:
        candidate = kb_root / "updates" / digest
        if not candidate.exists():
            candidate = kb_root / "updates" / f"{digest}.md"
        if not candidate.exists():
            console.print(f"[red]Digest not found: {digest}[/red]")
            raise typer.Exit(code=1)
        digest_path = candidate
    else:
        if not updates:
            console.print("[yellow]No digests found in updates/[/yellow]")
            console.print("Use --latest to send the most recent, or pass a filename.")
            raise typer.Exit(code=1)
        digest_path = updates[0]

    content = digest_path.read_text().strip()
    if not content:
        console.print("[red]Digest file is empty.[/red]")
        raise typer.Exit(code=1)

    apikey = os.environ.get("RESEND_API_KEY", "")
    if not apikey:
        console.print("[red]RESEND_API_KEY not set in environment or .env[/red]")
        raise typer.Exit(code=1)

    sender = notifications.get("from", "")
    if not sender:
        sender = f"{kb_root.name}@sbarbi.it"
        console.print(f"[dim]Using derived sender: {sender}[/dim]")

    recipients = notifications.get("to", [])
    if not recipients:
        console.print("[red][notifications].to is empty in method.toml[/red]")
        raise typer.Exit(code=1)

    subject = _subject_from_digest(digest_path)
    html_body = mistune.html(content)
    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 640px; margin: 0 auto; padding: 2em; line-height: 1.6; color: #1a1a1a;">
{html_body}
</body>
</html>"""

    if dry_run:
        console.print(f"[bold]DRY RUN[/bold]")
        console.print(f"  From: {sender}")
        console.print(f"  To: {', '.join(recipients)}")
        console.print(f"  Subject: {subject}")
        console.print(f"  Digest: {digest_path.name}")
        console.print()
        console.print(content)
        return

    console.print(f"Sending digest [bold]{digest_path.name}[/bold]...")
    try:
        resp = httpx.post(
            RESEND_API,
            headers={
                "Authorization": f"Bearer {apikey}",
                "Content-Type": "application/json",
            },
            json={
                "from": sender,
                "to": recipients,
                "subject": subject,
                "html": html,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        email_id = data.get("id", "unknown")
        console.print(f"[green]Sent. Resend ID: {email_id}[/green]")

        write_event(
            kb_root,
            {
                "kind": "digest-sent",
                "digest": digest_path.name,
                "resend_id": email_id,
                "recipients": recipients,
            },
        )
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Resend API error: {e.response.status_code}[/red]")
        console.print(e.response.text)
        raise typer.Exit(code=1)
    except Exception as e:
        console.print(f"[red]Failed to send: {e}[/red]")
        raise typer.Exit(code=1)
