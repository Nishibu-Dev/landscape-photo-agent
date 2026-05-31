import os
import hmac
import hashlib
import base64
import json

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

from agents.coordinator import root_agent
from tools.line_client import push_message

app = FastAPI()

SESSION_SERVICE = InMemorySessionService()
RUNNER = Runner(
    agent=root_agent,
    app_name="landscape-photo-agent",
    session_service=SESSION_SERVICE,
)

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")

# 待機メッセージを出すツール名
WAIT_FORECAST_TOOLS = ("fetch_forecast", "fetch_all_forecasts")
WAIT_RECORD_TOOLS = ("save_actual",)


def _verify_signature(body: bytes, signature: str) -> bool:
    hash_ = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(hash_).decode("utf-8")
    return hmac.compare_digest(expected, signature)


@app.get("/")
def health_check():
    return {"status": "ok", "service": "landscape-photo-agent"}


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()

    signature = request.headers.get("X-Line-Signature", "")
    if LINE_CHANNEL_SECRET and not _verify_signature(body, signature):
        raise HTTPException(status_code=400, detail="Invalid signature")

    events = json.loads(body).get("events", [])

    for event in events:
        if event.get("type") != "message":
            continue
        if event.get("message", {}).get("type") != "text":
            continue

        user_id      = event["source"]["userId"]
        user_message = event["message"]["text"]

        background_tasks.add_task(_process_message, user_id, user_message)

    return {"status": "ok"}


def _extract_text(chunk) -> str:
    """chunk から表示用テキストを連結して返す（無ければ空文字）。"""
    if not getattr(chunk, "content", None) or not chunk.content.parts:
        return ""
    parts = [
        p.text for p in chunk.content.parts
        if getattr(p, "text", None) and p.text.strip()
    ]
    return "".join(parts)


def _function_calls(chunk) -> list[str]:
    """chunk に含まれる function_call 名のリストを返す。"""
    names = []
    if getattr(chunk, "content", None) and chunk.content.parts:
        for p in chunk.content.parts:
            fc = getattr(p, "function_call", None)
            if fc and getattr(fc, "name", None):
                names.append(fc.name)
    return names


def _state_delta_text(chunk) -> str:
    """chunk の actions.state_delta から output_key 由来のテキストを拾う。"""
    actions = getattr(chunk, "actions", None)
    if not actions:
        return ""
    state_delta = getattr(actions, "state_delta", None)
    if not state_delta:
        return ""
    for key in ("prediction_result", "record_result"):
        if key in state_delta:
            val = state_delta[key]
            if isinstance(val, str) and val.strip():
                return val
    return ""


async def _process_message(user_id: str, user_message: str):
    try:
        session_id = f"line-{user_id}"

        session = await SESSION_SERVICE.get_session(
            app_name="landscape-photo-agent",
            user_id=user_id,
            session_id=session_id,
        )
        if session is None:
            session = await SESSION_SERVICE.create_session(
                app_name="landscape-photo-agent",
                user_id=user_id,
                session_id=session_id,
            )

        content = genai_types.Content(
            role="user",
            parts=[genai_types.Part(text=user_message)],
        )

        waiting_sent = False
        last_final_text = ""   # 最後に観測した final 応答テキスト（最優先）
        output_key_text = ""   # output_key 由来テキスト（保険）

        async for chunk in RUNNER.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=content,
        ):
            # --- 待機メッセージ（ツール呼び出し開始時に1回だけ） ---
            if not waiting_sent:
                calls = _function_calls(chunk)
                if any(c in WAIT_FORECAST_TOOLS for c in calls):
                    await push_message(
                        user_id,
                        "⏳ 条件を解析します。\n少し時間がかかります。\n2分ほどお待ちください...🙇",
                    )
                    waiting_sent = True
                elif any(c in WAIT_RECORD_TOOLS for c in calls):
                    await push_message(
                        user_id,
                        "📝 実績を記録しています。\n少々お待ちください...🙇",
                    )
                    waiting_sent = True

            # --- output_key 由来テキストを随時控えておく（保険） ---
            sd_text = _state_delta_text(chunk)
            if sd_text:
                output_key_text = sd_text

            # --- 最終応答テキストを随時更新（最後のものが本命） ---
            # 途中の Coordinator が空 final を出しても上書きされないよう、
            # テキストがあるときだけ更新する。
            if chunk.is_final_response():
                text = _extract_text(chunk)
                if text:
                    last_final_text = text
            # ※ ここで break しない。transfer 後のサブエージェント最終応答まで
            #    ループを回し切り、最後に観測した final テキストを採用する。

        reply_text = last_final_text or output_key_text

        if reply_text:
            await push_message(user_id, reply_text)
        else:
            await push_message(
                user_id,
                "⚠️ 応答を取得できませんでした。もう一度お試しください。",
            )

    except Exception as e:
        print(f"Error processing message for {user_id}: {e}")
        await push_message(user_id, "⚠️ エラーが発生しました。もう一度お試しください。")