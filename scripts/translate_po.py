import polib
from openai import OpenAI
import time
from django.conf import settings
from dotenv import load_dotenv
import os
load_dotenv() 

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

po = polib.pofile("locale/es/LC_MESSAGES/django.po")

SYSTEM_PROMPT = (
    "You are translating Django UI strings into neutral Latin American Spanish. "
    "Preserve variables, placeholders, punctuation, and line breaks. "
    "Do not translate code, variable names, or template syntax."
)

def translate(text):
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
    )
    return resp.choices[0].message.content.strip()

for entry in po:
    if entry.msgid and not entry.msgstr:
        try:
            entry.msgstr = translate(entry.msgid)
            time.sleep(0.4)  # avoid rate limits
        except Exception as e:
            print("Error:", e)

po.save()
print("Translation complete.")
