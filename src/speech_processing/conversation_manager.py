import os

# Provider switches — STT_PROVIDER + TTS_PROVIDER default to "deepgram"
# so existing setups keep working. Set either to "sixtydb" (or "60db")
# to route through 60db. Both peer modules expose the same interface
# names so the loop body below is unchanged.
if os.getenv("STT_PROVIDER", "deepgram").strip().lower() in ("sixtydb", "60db"):
    from .sixtydb_stt import get_transcript
else:
    from .speech_to_text import get_transcript

if os.getenv("TTS_PROVIDER", "deepgram").strip().lower() in ("sixtydb", "60db"):
    from .sixtydb_tts import TTS
else:
    from .text_to_speech import TTS


class ConversationManager:
    def __init__(self, assistant):
        self.transcription_response = ""
        self.assistant = assistant

    async def main(self):
        def handle_full_sentence(full_sentence):
            self.transcription_response = full_sentence

        # Loop indefinitely until "goodbye" is said
        while True:
            await get_transcript(handle_full_sentence)
            
            # Check for "goodbye" to exit the loop
            if "goodbye" in self.transcription_response.lower():
                break
            
            llm_response = self.assistant.invoke(self.transcription_response)
            print(f"AI: {llm_response}")

            tts = TTS()
            tts.speak(llm_response)

            # Reset transcription_response for the next loop iteration
            self.transcription_response = ""