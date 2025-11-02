from fastapi import FastAPI
from fastapi.websockets import WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRouter
import os, json, base64, asyncio, uuid
from deepgram import DeepgramClient, LiveTranscriptionEvents, LiveOptions, SpeakOptions
import requests

app = FastAPI()
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)
router = APIRouter()

# ======== CONFIG ========
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://your-render-app.onrender.com")
deepgram = DeepgramClient(DEEPGRAM_API_KEY)


# ======== TWILIO ENTRY POINT ========
@router.post("/twilio/voice")
async def twilio_voice():
    """TwiML instructs Twilio to start streaming audio to /audio"""
    stream_url = f"wss://{PUBLIC_URL.replace('https://','').replace('http://','')}/audio"
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Say voice="Polly.Joanna">Hello! You are connected to the AI assistant.</Say>
        <Start>
            <Stream url="{stream_url}">
                <Parameter name="caller" value="+18702735332"/>
                <Parameter name="receiver" value="+19094135795"/>
            </Stream>
        </Start>
    </Response>"""
    return Response(content=twiml, media_type="application/xml")


# ======== AI PIPELINE ========
async def generate_ai_reply(text: str) -> str:
    """Send transcript to Groq and get a simple text reply."""
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    payload = {
        "model": "mixtral-8x7b",  # or llama3-8b if you prefer
        "messages": [{"role": "user", "content": text}],
        "temperature": 0.7,
    }
    r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                      json=payload, headers=headers, timeout=30)
    if r.status_code != 200:
        print("Groq error:", r.text)
        return "Sorry, I encountered an error."
    return r.json()["choices"][0]["message"]["content"]


async def synthesize_tts(text: str) -> bytes:
    """Convert AI reply text → mu-law encoded audio bytes for Twilio."""
    try:
        from io import BytesIO
        buffer = BytesIO()
        options = SpeakOptions(model="aura-asteria-en", encoding="mulaw", sample_rate=8000)
        deepgram.speak.v("1").stream(buffer, {"text": text}, options)
        buffer.seek(0)
        return buffer.read()
    except Exception as e:
        print("TTS error:", e)
        return b""


# ======== WEBSOCKET HANDLER ========
@router.websocket("/audio")
async def audio_stream(websocket: WebSocket):
    """Handles live Twilio → Deepgram → Groq → Deepgram TTS → Twilio"""
    await websocket.accept()
    print("✅ WebSocket connected")

    dg_socket = deepgram.listen.websocket.v("1")

    def on_transcript(self, result, **kwargs):
        transcript = result.channel.alternatives[0].transcript
        if not transcript.strip():
            return
        if getattr(result, "is_final", False):
            print(f"🗣️ Heard: {transcript}")
            asyncio.run_coroutine_threadsafe(process_transcript(transcript, websocket), asyncio.get_event_loop())

    dg_socket.on(LiveTranscriptionEvents.Transcript, on_transcript)
    dg_socket.start(LiveOptions(model="nova-2-general", encoding="mulaw", sample_rate=8000))

    try:
        while True:
            msg = await websocket.receive()
            data = msg.get("text") or msg.get("bytes")
            if isinstance(data, str):
                event = json.loads(data)
                if event.get("event") == "media":
                    audio_payload = base64.b64decode(event["media"]["payload"])
                    dg_socket.send(audio_payload)
                elif event.get("event") == "stop":
                    print("🛑 Twilio stream stopped")
                    break
            elif isinstance(data, (bytes, bytearray)):
                dg_socket.send(data)
    except WebSocketDisconnect:
        print("❌ WebSocket disconnected")
    finally:
        dg_socket.finish()
        print("✅ Deepgram socket closed")


# ======== CORE LOGIC ========
async def process_transcript(text: str, websocket: WebSocket):
    """Full loop: transcript → Groq → Deepgram TTS → send to Twilio."""
    try:
        ai_reply = await generate_ai_reply(text)
        print(f"🤖 AI: {ai_reply}")
        audio_bytes = await synthesize_tts(ai_reply)
        if audio_bytes:
            payload = base64.b64encode(audio_bytes).decode("utf-8")
            await websocket.send_json({
                "event": "media",
                "media": {"payload": payload}
            })
            print("🎧 Sent synthesized audio to Twilio")
    except Exception as e:
        print("Error in process_transcript:", e)


# ======== REGISTER ROUTES ========
app.include_router(router)
