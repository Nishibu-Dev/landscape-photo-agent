---
name: deploy
description: Cloud Run にデプロイする。「デプロイ」「deploy」「本番に上げて」と言われたときに使う。
---

## 手順

1. テストを実行して全パスを確認する
   ```
   cd /Users/anna/Desktop/landscape-photo-agent-dev/landscape-photo-agent
   pytest tests/ -v
   ```
   失敗したテストがあれば **デプロイを中止** してユーザーに報告する。

2. git の状態を確認する
   ```
   git status
   ```
   未コミットの変更があればユーザーに確認を取る。

3. Cloud Run にデプロイする
   ```
   gcloud run deploy landscape-photo-agent \
     --source . \
     --region asia-northeast1 \
     --no-cpu-throttling \
     --min-instances 0
   ```
   - `--no-cpu-throttling`: バックグラウンドタスク（ADKエージェント処理）のCPU割り当て維持
   - `--min-instances 0`: コスト優先。アイドル時はスケールtoゼロし、初回リクエストにコールドスタート遅延（数秒程度）が発生する

4. ヘルスチェックを実行する
   ```
   curl -s https://landscape-photo-agent-<hash>-an.a.run.app/ | python3 -m json.tool
   ```
   `{"status": "ok"}` が返ればデプロイ成功。URLが不明な場合は `gcloud run services describe landscape-photo-agent --region asia-northeast1 --format='value(status.url)'` で取得する。

5. 結果を報告する
   - テスト結果（全パス）
   - デプロイ先URL
   - ヘルスチェック結果
