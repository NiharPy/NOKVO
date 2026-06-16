import sys
from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline

try:
    print(NokvoOneVoicePipeline._parse_appointment_time("14:00"))
except Exception as e:
    print("Error:", repr(e))
