# Google Drive 上の JSON ファイルを読み書きする
#
# 【認証の仕組み】
# このアプリは Cloud Run 上で動いており、デプロイ時に
# サービスアカウント（93996837741-compute@developer.gserviceaccount.com）
# が紐づいている。
#
# サービスアカウント = GCPにおけるサービスプリンシパル
#   → 人間ではなくアプリケーション自身の「身元証明」
#
# 認証フロー:
#   Cloud Run コンテナ
#     ↓ メタデータサーバーにトークンをリクエスト
#   http://metadata.google.internal（GCP内部専用・外部からアクセス不可）
#     ↓ サービスアカウントの権限を確認し、アクセストークンを発行
#   アクセストークン（1時間有効）
#     ↓ Authorization ヘッダーに付けて Drive API を呼び出す
#   Google Drive API → JSONファイルの読み書き成功
#
# このトークンは Drive 専用ではなく、サービスアカウントに付与された
# 権限すべてに使える汎用トークン（Secret Manager 等も同じトークンで使える）
#
# コードに鍵やパスワードを書かなくても、インフラの仕組みで
# 自動的に本人確認が完結するのがこの設計の強み

import json
import httpx

from tools.logger import get_logger

logger = get_logger(__name__)

DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3"


async def _get_access_token() -> str:
    url = (
        "http://metadata.google.internal/computeMetadata/v1/"
        "instance/service-accounts/default/token"
    )
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers={"Metadata-Flavor": "Google"})
        resp.raise_for_status()
        return resp.json()["access_token"]


async def read_json(file_id: str) -> dict | list:
    token = await _get_access_token()
    url = f"{DRIVE_API_BASE}/files/{file_id}?alt=media"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
        return resp.json()


async def write_json(file_id: str, data: dict | list) -> None:
    token = await _get_access_token()
    url = f"{DRIVE_UPLOAD_BASE}/files/{file_id}?uploadType=media"
    async with httpx.AsyncClient() as client:
        resp = await client.patch(
            url,
            content=json.dumps(data, ensure_ascii=False, indent=2).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        resp.raise_for_status()


async def append_to_json_list(file_id: str, new_item: dict) -> None:
    """
    ファイルが list 形式（[...]）または {"predictions":[...]} のような
    単一キーラッパー形式の両方に対応して末尾追記する。
    """
    try:
        data = await read_json(file_id)
    except Exception:
        # ここでログを残さないと、Drive 側の読み込み失敗(認証切れ・ファイルID不正等)で
        # 気づかぬまま新規リストとして上書き保存してしまうリスクに気づけない。
        logger.exception(f"append_to_json_list: read_json failed file_id={file_id}, starting with empty list")
        data = []

    if isinstance(data, list):
        data.append(new_item)
        await write_json(file_id, data)
        return

    # ラッパー形式（predictions / actuals）への追記
    if isinstance(data, dict):
        for key in ("predictions", "actuals", "fog_knowledge"):
            if key in data and isinstance(data[key], list):
                data[key].append(new_item)
                await write_json(file_id, data)
                return
        # 想定キーが無ければ list として作り直す
    await write_json(file_id, [new_item])


async def find_latest_prediction_id(
    file_id: str,
    spot_name: str,
    target_date: str | None = None,
) -> str | None:
    """
    predictions.json から、指定地点（必要なら対象日も一致）の
    最も新しい予測の id を返す。見つからなければ None。

    Args:
        file_id: predictions.json の Drive ファイルID
        spot_name: 地点名
        target_date: 撮影対象日 YYYY-MM-DD。指定時はこの日に一致するものを優先。
    """
    try:
        data = await read_json(file_id)
    except Exception:
        logger.exception(f"find_latest_prediction_id: read_json failed file_id={file_id} spot={spot_name}")
        return None

    if isinstance(data, dict):
        records = data.get("predictions", [])
    elif isinstance(data, list):
        records = data
    else:
        return None

    candidates = [
        r for r in records
        if isinstance(r, dict) and r.get("spot_name") == spot_name
    ]
    if target_date:
        dated = [r for r in candidates if r.get("target_date") == target_date]
        if dated:
            candidates = dated

    if not candidates:
        return None

    def _sort_key(r: dict):
        return r.get("predicted_at") or r.get("target_date") or ""

    candidates.sort(key=_sort_key)
    return candidates[-1].get("id")