import asyncio
import os
import pyaudio
from deepgram import AsyncDeepgramClient
from deepgram.listen.v1.socket_client import ListenV1Results, ListenV1UtteranceEnd

DEEPGRAM_API_KEY = os.environ["DEEPGRAM_API_KEY"]

FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 16000
CHUNK = 8000

_dg = AsyncDeepgramClient(api_key=DEEPGRAM_API_KEY)


async def get_voice_input() -> str:
    """Capture mic audio, stream to Deepgram via SDK, return final transcript."""
    print("\n\033[1;36mListening...\033[0m (speak now, pause when done)", flush=True)

    audio_queue: asyncio.Queue[bytes] = asyncio.Queue()
    transcript_parts: list[str] = []
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
        async with _dg.listen.v1.connect(
            model="nova-2",
            encoding="linear16",
            sample_rate=RATE,
            channels=CHANNELS,
            interim_results="true",
            utterance_end_ms=1000,
            vad_events="true",
        ) as connection:

            async def sender():
                while not done.is_set():
                    try:
                        data = await asyncio.wait_for(audio_queue.get(), timeout=0.2)
                        await connection.send_media(data)
                    except asyncio.TimeoutError:
                        continue

            sender_task = asyncio.create_task(sender())
            try:
                async for msg in connection:
                    if isinstance(msg, ListenV1Results):
                        t = msg.channel.alternatives[0].transcript
                        if msg.is_final and t:
                            transcript_parts.append(t)
                    elif isinstance(msg, ListenV1UtteranceEnd):
                        done.set()
                        break
            finally:
                sender_task.cancel()
                await connection.send_close_stream()

    finally:
        stream.stop_stream()
        stream.close()
        audio.terminate()

    result = " ".join(transcript_parts).strip()
    print(f"\033[1;36mYou\033[0m › {result}")
    return result
