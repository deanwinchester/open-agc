import os
import json
import asyncio
import sqlite3
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv, set_key

# Load environment variables
load_dotenv()

from agent.agent import OpenAGCAgent

app = FastAPI(title="Open-AGC UI Server")

# Initialize Database
DB_PATH = "data/chat_history.db"

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def save_message(role: str, content: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO messages (role, content) VALUES (?, ?)", (role, content))
    conn.commit()
    conn.close()

# Mount the static directory
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def read_index():
    return FileResponse("static/index.html")

# Settings API Models
class SettingsUpdate(BaseModel):
    model_name: str
    api_key: str
    provider: str  # 'openai', 'anthropic', 'deepseek', 'gemini'

@app.get("/api/settings")
async def get_settings():
    """Return current configured model, provider, and masked API key."""
    # Determine current provider based on active keys (simplified heuristic)
    provider = "openai"
    api_key_masked = ""
    
    if os.getenv("OEM_API_KEY") or os.getenv("OPENAI_API_KEY"): # Default fallback
        pass
    if os.getenv("ANTHROPIC_API_KEY"):
        provider = "anthropic"
        key = os.getenv("ANTHROPIC_API_KEY")
        api_key_masked = f"{key[:3]}...{key[-3:]}" if len(key) > 6 else "***"
    if os.getenv("DEEPSEEK_API_KEY"):
        provider = "deepseek"
        key = os.getenv("DEEPSEEK_API_KEY")
        api_key_masked = f"{key[:3]}...{key[-3:]}" if len(key) > 6 else "***"
    if os.getenv("GEMINI_API_KEY"):
        provider = "gemini"
        key = os.getenv("GEMINI_API_KEY")
        api_key_masked = f"{key[:3]}...{key[-3:]}" if len(key) > 6 else "***"
    # Recheck openai to ensure it overrides if it was explicitly the last set
    if os.getenv("OPENAI_API_KEY") and not api_key_masked:
         provider = "openai"
         key = os.getenv("OPENAI_API_KEY")
         api_key_masked = f"{key[:3]}...{key[-3:]}" if len(key) > 6 else "***"

    # Default logic for masked
    if not api_key_masked and os.getenv("DEFAULT_API_KEY"):
         api_key_masked = "***"

    return {
        "model_name": os.getenv("DEFAULT_MODEL", "gpt-4o"),
        "provider": provider,
        "api_key_masked": api_key_masked
    }

@app.post("/api/settings")
async def update_settings(settings: SettingsUpdate):
    """Update environment variables and save to .env"""
    env_file = ".env"
    if not os.path.exists(env_file):
        open(env_file, 'a').close()
        
    try:
        # Determine which key to set based on provider
        key_name = ""
        if settings.provider == 'openai':
            key_name = "OPENAI_API_KEY"
        elif settings.provider == 'anthropic':
            key_name = "ANTHROPIC_API_KEY"
        elif settings.provider == 'deepseek':
            key_name = "DEEPSEEK_API_KEY"
        elif settings.provider == 'gemini':
            key_name = "GEMINI_API_KEY"
        else:
            raise HTTPException(status_code=400, detail="Unknown provider")
            
        # Write to .env
        set_key(env_file, key_name, settings.api_key)
        set_key(env_file, "DEFAULT_MODEL", settings.model_name)
        
        # Update running process environment
        os.environ[key_name] = settings.api_key
        os.environ["DEFAULT_MODEL"] = settings.model_name
        
        # Reload env
        load_dotenv(override=True)
        return {"status": "success", "message": "Settings updated successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

import requests
@app.get("/api/provider-models")
async def get_provider_models(provider: str):
    """Query the actual provider API to get a list of available models."""
    models = []
    
    if provider == "gemini":
        key = os.getenv("GEMINI_API_KEY")
        if key:
            try:
                # Gemini list_models API
                res = requests.get(f"https://generativelanguage.googleapis.com/v1beta/models?key={key}", timeout=5)
                if res.status_code == 200:
                    models = [m["name"].replace("models/", "gemini/") for m in res.json().get("models", []) if "gemini" in m["name"] or "pro" in m["name"] or "flash" in m["name"]]
            except Exception as e:
                print(f"Error fetching gemini models: {e}")
                
    elif provider == "openai":
        key = os.getenv("OPENAI_API_KEY")
        if key:
            try:
                headers = {"Authorization": f"Bearer {key}"}
                res = requests.get("https://api.openai.com/v1/models", headers=headers, timeout=5)
                if res.status_code == 200:
                    models = [m["id"] for m in res.json().get("data", []) if "gpt" in m["id"]]
            except Exception as e:
                print(f"Error fetching openai models: {e}")
                
    elif provider == "deepseek":
        key = os.getenv("DEEPSEEK_API_KEY")
        if key:
            try:
                headers = {"Authorization": f"Bearer {key}"}
                res = requests.get("https://api.deepseek.com/v1/models", headers=headers, timeout=5)
                if res.status_code == 200:
                    models = [f"deepseek/{m['id']}" for m in res.json().get("data", [])]
            except Exception as e:
                print(f"Error fetching deepseek models: {e}")

    # Fallback default models if API call fails or key not set
    if not models:
        defaults = {
            'openai': ['gpt-4o', 'gpt-4o-mini', 'gpt-4-turbo', 'gpt-3.5-turbo'],
            'anthropic': ['claude-3-5-sonnet-20240620', 'claude-3-opus-20240229', 'claude-3-haiku-20240307'],
            'deepseek': ['deepseek/deepseek-chat', 'deepseek/deepseek-reasoner'],
            'gemini': ['gemini/gemini-1.5-pro', 'gemini/gemini-2.5-pro', 'gemini/gemini-3.1-pro-preview', 'gemini/gemini-3-pro-preview']
        }
        models = defaults.get(provider, [])
        
    # Sort alphabetically 
    models.sort()
    return {"models": models}

@app.get("/api/skills")
async def get_skills():
    """List available skills from the skills directory."""
    skills_dir = "skills"
    skills = []
    
    if os.path.exists(skills_dir):
        for filename in os.listdir(skills_dir):
            if filename.endswith(".md") or filename.endswith(".py"):
                skills.append({
                    "name": filename,
                    "type": filename.split('.')[-1]
                })
    return {"skills": skills}

@app.get("/api/history")
async def get_history():
    """Retrieve chat history."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT role, content FROM messages ORDER BY id ASC")
    rows = cursor.fetchall()
    conn.close()
    return {"history": [{"role": row["role"], "content": row["content"]} for row in rows]}

# Initialize a global agent instance
# In a real multi-user system, this would be per-session
# We'll instantiate per connection for simplicity and state isolation in this demo

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    
    # We will maintain conversation history for this session here
    session_history = []
    
    try:
        while True:
            # Wait for user message
            data = await websocket.receive_text()
            user_msg = json.loads(data)
            query = user_msg.get("query", "")
            
            if not query.strip():
                continue
                
            # Save user message to DB
            save_message("user", query)

            # Send immediate acknowledgment
            await websocket.send_json({
                "type": "status",
                "message": "Agent is thinking..."
            })
            
            # Create a fresh agent for this turn to pick up any ENV changes (e.g., API Keys/Models)
            current_model = os.getenv("DEFAULT_MODEL", "gpt-4o")
            agent = OpenAGCAgent(model=current_model)
            
            # Inject previous session history so it doesn't lose context
            if session_history:
                 # system prompt is at index 0, we append history after it
                 agent.messages.extend(session_history)
            
            try:
                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(None, agent.run_turn, query, False)
                
                # Update session history with the newly appended messages from this turn
                # We skip the system prompt (index 0)
                session_history = agent.messages[1:]
                
                # Save agent response to DB
                save_message("agent", response)

                # Send the final response
                await websocket.send_json({
                    "type": "message",
                    "role": "agent",
                    "content": response
                })
                
            except Exception as e:
                error_msg = f"Agent Encountered Error: {str(e)}"
                save_message("system", error_msg)
                await websocket.send_json({
                    "type": "error",
                    "content": error_msg
                })
                
    except WebSocketDisconnect:
        print("Client disconnected")
    except Exception as e:
        print(f"WebSocket error: {e}")
