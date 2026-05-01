"""TransLive Mobile — FastAPI backend server."""

import asyncio
import json
import os
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()

app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY", "")
DEEPGRAM_URL = (
    "wss://api.deepgram.com/v1/listen"
    "?encoding=linear16&sample_rate=16000&model=nova-3"
    "&punctuate=true&smart_format=true"
)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health():
    return JSONResponse({
        "status": "ok",
        "api_key_set": bool(DEEPGRAM_API_KEY),
        "api_key_prefix": DEEPGRAM_API_KEY[:8] + "..." if DEEPGRAM_API_KEY else "none"
    })


@app.websocket("/ws/transcribe")
async def transcribe_ws(client_ws: WebSocket):
    await client_ws.accept()
    print("Client connected")

    if not DEEPGRAM_API_KEY:
        await client_ws.send_json({"error": "DEEPGRAM_API_KEY not set"})
        return

    source_lang = client_ws.query_params.get("source", "auto")
    target_lang = client_ws.query_params.get("target", "en")
    print(f"Languages: {source_lang} → {target_lang}")

    # Send immediate status so client knows we're alive
    await client_ws.send_json({"status": "connecting"})

    # Connect to Deepgram using aiohttp instead of websockets library
    try:
        import aiohttp
        headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}"}

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(DEEPGRAM_URL, headers=headers) as dg_ws:
                print("Connected to Deepgram")
                await client_ws.send_json({"status": "ready"})

                async def from_deepgram():
                    from deep_translator import GoogleTranslator
                    async for msg in dg_ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                                if data.get("type") in ("Metadata", "SpeechStarted"):
                                    continue
                                text = (data.get("channel") or {}).get(
                                    "alternatives", [{}])[0].get("transcript", "")
                                is_final = data.get("is_final", False)
                                if not text.strip():
                                    continue
                                try:
                                    translated = await asyncio.get_event_loop().run_in_executor(
                                        None,
                                        lambda t=text: GoogleTranslator(
                                            source=source_lang, target=target_lang
                                        ).translate(t)
                                    )
                                except Exception:
                                    translated = text
                                await client_ws.send_json({
                                    "original": text,
                                    "translated": translated or text,
                                    "is_final": is_final,
                                })
                            except Exception as e:
                                print(f"Parse error: {e}")
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break

                async def from_client():
                    while True:
                        try:
                            data = await client_ws.receive_bytes()
                            await dg_ws.send_bytes(data)
                        except (WebSocketDisconnect, Exception):
                            break

                await asyncio.gather(from_deepgram(), from_client(),
                                     return_exceptions=True)

    except Exception as e:
        print(f"Error: {type(e).__name__}: {e}")
        try:
            await client_ws.send_json({"error": str(e)})
        except Exception:
            pass
