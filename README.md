# MTProto Library Spike

This spike evaluates Telethon and Pyrogram for unauthenticated MTProto proxy testing.

## Setup
1. Create a virtual environment: `python -m venv venv && source venv/bin/activate`
2. Install dependencies: `pip install -r requirements.txt`
3. Export your Telegram API credentials (you can get these from my.telegram.org):
   `export API_ID=your_api_id`
   `export API_HASH=your_api_hash`
4. Set a valid proxy for testing:
   `export PROXY_SERVER=1.2.3.4`
   `export PROXY_PORT=443`
   `export PROXY_SECRET=ee111122223333444455556666777788887777772e676f6f676c652e636f6d`
5. Run the spikes:
   `python spike_telethon.py`
   `python spike_pyrogram.py`