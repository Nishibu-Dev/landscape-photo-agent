import os
import httpx

LINE_API_URL = "https://api.line.me/v2/bot/message/push"


async def push_message(user_id: str, text: str):
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")

    if not token:
        print("[ERROR] push_message: LINE_CHANNEL_ACCESS_TOKEN が未設定です")
        return

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    payload = {
        "to": user_id,
        "messages": [
            {
                "type": "text",
                "text": text,
            }
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(LINE_API_URL, json=payload, headers=headers)

        # 成功/失敗をログに出す。LINE Push API は成功時 200 を返す。
        if response.status_code == 200:
            print(f"[INFO] push_message OK: to={user_id[:8]}... len={len(text)}")
        else:
            # 失敗時はステータスと本文を出す（トークン不正・ID不正などが分かる）
            print(
                f"[ERROR] push_message failed: status={response.status_code} "
                f"body={response.text}"
            )
    except Exception as e:
        print(f"[ERROR] push_message exception: {e}")