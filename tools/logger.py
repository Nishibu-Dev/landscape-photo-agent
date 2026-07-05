# Cloud Run(Cloud Logging)向けの構造化ロガー。
#
# stdout に severity 付き JSON を1行ずつ出すと、Cloud Logging 側が自動でパースして
# LogEntry の severity・jsonPayload に割り当てる(追加ライブラリ不要)。
# これにより Cloud Console / gcloud logging で
#   severity>=ERROR かつ jsonPayload.trace_id="xxxx"
# のように、1回のユーザー発話に紐づくログだけを絞り込んで追える。
#
# trace_id / user_id は ContextVar に持たせ、webhook 受信時に1回セットするだけで
# 以降その処理から呼ばれるどのモジュールの logger.xxx() にも自動で乗る
# (関数シグネチャに trace_id を引き回す必要がない。ADK の FunctionTool は
#  シグネチャがそのまま LLM のツール定義になるため、余計な引数を増やしたくない)。

import contextvars
import json
import logging
import sys

trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="-")
user_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("user_id", default="-")


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "trace_id": trace_id_var.get(),
            "user_id": user_id_var.get(),
        }
        if record.exc_info:
            payload["stack_trace"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger
