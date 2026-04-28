import os
import sys
import argparse
from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown
from core.paths import get_data_path

# Load environment variables (API keys)
env_file = get_data_path(".env")
load_dotenv(env_file)

from agent.agent import OpenAGCAgent

console = Console()


def print_welcome():
    console.print("[bold green]Welcome to Open-AGC (Agentic Computer Control)[/bold green]")
    console.print("I can help you execute terminal commands, manage files, and run python code.")
    console.print("Type [bold yellow]'exit'[/bold yellow] or [bold yellow]'quit'[/bold yellow] to leave.")
    console.print("Type [bold yellow]':image <path>'[/bold yellow] to attach an image for vision analysis.\n")


def run_query(agent: OpenAGCAgent, user_input: str, images: list = None):
    """Run a single query with the agent and print the response."""
    with console.status("[bold cyan]Agent is thinking and executing...[/bold cyan]", spinner="dots"):
        response = agent.run_turn(user_input, verbose=True, images=images)
    console.print("\n[bold magenta]Open-AGC:[/bold magenta]")
    console.print(Markdown(response))
    console.print("-" * 50)


def main():
    parser = argparse.ArgumentParser(description="Open-AGC Console")
    parser.add_argument("-i", "--image", action="append", default=None,
                        help="Attach an image file for vision analysis (can be specified multiple times)")
    parser.add_argument("query", nargs="*", default=None,
                        help="Text query (if provided, runs in one-shot mode and exits)")
    args = parser.parse_args()

    print_welcome()

    # Initialize the agent
    default_model = os.getenv("DEFAULT_MODEL", "moonshot/kimi-latest")
    agent = OpenAGCAgent(model=default_model)

    # One-shot mode: query and images from command line
    if args.query:
        query_text = " ".join(args.query)
        images = list(args.image) if args.image else None
        run_query(agent, query_text, images)
        return

    # Interactive REPL mode
    pending_images = list(args.image) if args.image else []

    while True:
        try:
            user_input = console.input("[bold blue]You:[/bold blue] ")

            if user_input.strip().lower() in ['exit', 'quit']:
                console.print("[yellow]Goodbye![/yellow]")
                break

            if not user_input.strip():
                continue

            # Handle :image command — accumulate images for next query
            if user_input.strip().startswith(":image"):
                parts = user_input.strip().split(maxsplit=1)
                if len(parts) < 2:
                    if pending_images:
                        console.print(f"[yellow]Pending images: {pending_images}[/yellow]")
                        console.print("[dim]Use ':image clear' to discard, or type your query to send them.[/dim]")
                    else:
                        console.print("[yellow]Usage: :image <path>[/yellow]")
                    continue
                img_path = parts[1].strip()
                if img_path.lower() == "clear":
                    pending_images = []
                    console.print("[dim]Cleared pending images.[/dim]")
                elif os.path.exists(img_path):
                    pending_images.append(img_path)
                    console.print(f"[dim]Queued image: {img_path} ({len(pending_images)} total)[/dim]")
                else:
                    console.print(f"[red]File not found: {img_path}[/red]")
                continue

            # Send query with any pending images
            images = pending_images if pending_images else None
            pending_images = []
            run_query(agent, user_input, images)

        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted by user. Type 'exit' to quit.[/yellow]")
            pending_images = []
        except Exception as e:
            console.print(f"\n[bold red]An error occurred:[/bold red] {str(e)}")


if __name__ == "__main__":
    main()
