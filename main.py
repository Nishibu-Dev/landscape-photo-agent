import os
import hmac
import hashlib
import base64
import json
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

from agents.coordinator import root_agent
from tools.line_client import push_message
# 利用ログ記録は GAS 受付係側に移管したため Cloud Run では扱わない。
# 旧 tools/usage_log.py はデッドコードとして残してあるが、ここからは呼ばない。

app = FastAPI()

SESSION_SERVICE = InMemorySessionService()
RUNNER = Runner(
    agent=root_agent,
    app_name="landscape-photo-agent",
    session_service=SESSION_SERVICE,
)

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")

# --- セッション TTL（連続会話の有効時間） ---
# 前回発話から SESSION_TTL_SECONDS 以上空いたら過去文脈を破棄して
# 新規セッションで始める。閾値内ならセッション継続(リアクション継続用)。
SESSION_TTL_SECONDS = 300  # 5 分
_LAST_SEEN: dict[str, datetime] = {}  # user_id -> 最終発話時刻(JST aware)


def _now_jst() -> datetime:
    return datetime.now(timezone(timedelta(hours=9)))


# 即時応答(⏳解析中)は GAS 受付係が reply で返す。
# Cloud Run 側では待機 Push を出さず、最終結果のみ Push する。


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

    # 署名検証について:
    #   この構成は LINE → GAS(受付係) → Cloud Run。LINE と直接通信するのは
    #   GAS であり、Cloud Run は GAS からの転送を受けるだけ。
    #   GAS が UrlFetchApp でボディを転送する際、エンコードや整形でボディが
    #   わずかに変化するため、LINE 署名(ボディから計算)は構造的に一致せず
    #   検証は必ず失敗する。よって既定では検証しない。
    #   LINE 直結に戻す場合のみ VERIFY_LINE_SIGNATURE=true で有効化する。
    if os.environ.get("VERIFY_LINE_SIGNATURE", "").lower() == "true":
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

        # 重い処理はバックグラウンドへ。webhook 自体は即 200 を返す。
        background_tasks.add_task(_handle_message, user_id, user_message)

    return {"status": "ok"}


async def _handle_message(user_id: str, user_message: str):
    """ADK 本体処理を実行する。
    ブラックリスト判定・利用ログ記録は GAS 受付係側で完結しているため、
    Cloud Run まで届いた時点でそのユーザーはホワイト扱い・ログ記録済み。
    """
    print(f"[DEBUG] _handle_message start: user={user_id[:8]}... msg={user_message!r}")

    # --- ADK 本体処理 ---
    await _process_message(user_id, user_message)
    print("[DEBUG] _handle_message end")


def _extract_text(chunk) -> str:
    """chunk から表示用テキストを連結して返す（無ければ空文字）。"""
    if not getattr(chunk, "content", None) or not chunk.content.parts:
        return ""
    parts = [
        p.text for p in chunk.content.parts
        if getattr(p, "text", None) and p.text.strip()
    ]
    return "".join(parts)


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

        # --- 5分閾値でのセッションリセット ---
        # 前回発話から SESSION_TTL_SECONDS 以上空いていれば、
        # 過去の文脈（前回の予測など）を引きずらないよう旧セッションを破棄して
        # 新規セッションで始める。閾値以内なら継続して、
        # 予測直後の「キタコレ」「草」等のリアクションは文脈付きで応答できる。
        now = _now_jst()
        last = _LAST_SEEN.get(user_id)
        _LAST_SEEN[user_id] = now
        expired = last is not None and (now - last).total_seconds() > SESSION_TTL_SECONDS
        if expired:
            try:
                await SESSION_SERVICE.delete_session(
                    app_name="landscape-photo-agent",
                    user_id=user_id,
                    session_id=session_id,
                )
                print(f"[DEBUG] session expired, reset: user={user_id[:8]}...")
            except Exception as e:
                # 既に存在しない場合などは無視
                print(f"[DEBUG] session delete skipped: {e}")

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

        last_final_text = ""   # 最後に観測した final 応答テキスト（最優先）
        output_key_text = ""   # output_key 由来テキスト（保険）

        chunk_count = 0
        async for chunk in RUNNER.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=content,
        ):
            chunk_count += 1
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
        print(
            f"[DEBUG] run done: chunks={chunk_count} "
            f"final_len={len(last_final_text)} okey_len={len(output_key_text)} "
            f"reply_len={len(reply_text)}"
        )

        if reply_text:
            await push_message(user_id, reply_text)
        else:
            print("[WARN] reply_text is empty, sending fallback message")
            await push_message(
                user_id,
                "⚠️ 応答を取得できませんでした。もう一度お試しください。",
            )

    except Exception as e:
        print(f"Error processing message for {user_id}: {e}")
        await push_message(user_id, "⚠️ エラーが発生しました。もう一度お試しください。")