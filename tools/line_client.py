import os
import httpx

LINE_API_URL = "https://api.line.me/v2/bot/message/push"

async def push_message(user_id: str, text: str):
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "to": user_id,
        "messages": [
            {
                "type": "text",
                "text": text
            }
        ]
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.post(LINE_API_URL, json=payload, headers=headers)
        return response.status_code