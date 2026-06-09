import asyncio
import json
import os
import pyaudio
import websockets
from elevenlabs.client import ElevenLabs
from elevenlabs import stream

DEEPGRAM_API_KEY = os.environ["DEEPGRAM_API_KEY"]
ELEVENLABS_API_KEY = os.environ["ELEVENLABS_API_KEY"]
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")  # "George" — free tier

_el = ElevenLabs(api_key=ELEVENLABS_API_KEY)

FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 16000
CHUNK = 8000

DEEPGRAM_URL = (
    "wss://api.deepgram.com/v1/listen"
    "?encoding=linear16"
    "&sample_rate=16000"
    "&channels=1"
    "&model=nova-2"
    "&interim_results=true"
    "&utterance_end_ms=1000"
    "&vad_events=true"
)

async def get_voice_input() -> str:
    """Capture mic audio, stream to Deepgram, return final transcript."""
    print("\n\033[1;36mListening...\033[0m (speak now, pause when done) \n", flush=True)

    audio_queue = asyncio.Queue()
    transcript_parts = []
    done = asyncio.Event()

    def mic_callback(input_data, frame_count, time_info, status_flag):
        audio_queue.put_nowait(input_data)
        return (input_data, pyaudio.paContinue)

    audio = pyaudio.PyAudio()
    stream = audio.open(
        format=FORMAT,
        channels=CHANNELS,
        rate=RATE,
        input=True,
        frames_per_buffer=CHUNK,
        stream_callback=mic_callback,
    )
    stream.start_stream()

    try:
        async with websockets.connect(
            DEEPGRAM_URL,
            additional_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"},
        ) as ws:

            async def sender():
                while not done.is_set():
                    try:
                        data = await asyncio.wait_for(audio_queue.get(), timeout=0.2)
                        await ws.send(data)
                    except asyncio.TimeoutError:
                        continue

            async def receiver():
                async for msg in ws:
                    data = json.loads(msg)
                    msg_type = data.get("type")
                    if msg_type == "Results":
                        t = data["channel"]["alternatives"][0]["transcript"]
                        if t:
                            print(f"\r\033[1;36mYou:\033[0m {t}    ", end="", flush=True)
                        if data.get("is_final") and t:
                            transcript_parts.append(t)
                    elif msg_type == "UtteranceEnd":
                        done.set()
                        break

            sender_task = asyncio.create_task(sender())
            try:
                await receiver()
            finally:
                sender_task.cancel()
                await ws.send(json.dumps({"type": "CloseStream"}))
    finally:
        stream.stop_stream()
        stream.close()
        audio.terminate()

    result = " ".join(transcript_parts).strip()
    print()
    # print(f"\033[1;36mYou\033[0m › {result}")
    return result


async def speak(text: str) -> None:
    """Stream text to ElevenLabs TTS and play via ffplay."""
    if not text.strip():
        return

    audio_stream = _el.text_to_speech.stream(
        voice_id=ELEVENLABS_VOICE_ID,
        text=text,
        model_id="eleven_turbo_v2_5",
    )
    await asyncio.to_thread(stream, audio_stream)