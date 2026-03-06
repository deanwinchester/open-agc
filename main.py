import os
import sys
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
    console.print("Type [bold yellow]'exit'[/bold yellow] or [bold yellow]'quit'[/bold yellow] to leave.\n")

def main():
    print_welcome()
    
    # Initialize the agent
    # By default, use litellm's routing. 
    # E.g., model="gpt-4o" requires OPENAI_API_KEY
    # You can change model="claude-3-5-sonnet-20240620" for Anthropic.
    default_model = os.getenv("DEFAULT_MODEL", "gpt-4o")
    agent = OpenAGCAgent(model=default_model)
    
    while True:
        try:
            # Get user input
            user_input = console.input("[bold blue]You:[/bold blue] ")
            
            if user_input.strip().lower() in ['exit', 'quit']:
                console.print("[yellow]Goodbye![/yellow]")
                break
                
            if not user_input.strip():
                continue
                
            # Get agent response
            with console.status("[bold cyan]Agent is thinking and executing...[/bold cyan]", spinner="dots"):
                response = agent.run_turn(user_input, verbose=True)
                
            # Print response (rendered as Markdown)
            console.print("\n[bold magenta]Open-AGC:[/bold magenta]")
            console.print(Markdown(response))
            console.print("-" * 50)
            
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted by user. Type 'exit' to quit.[/yellow]")
        except Exception as e:
            console.print(f"\n[bold red]An error occurred:[/bold red] {str(e)}")

if __name__ == "__main__":
    main()
