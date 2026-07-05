# AnalysisAgent: 実績データをもとに霧が発生した夜/しなかった夜を比較分析するエージェント
# また、利用者が見つけた霧のパターンをノウハウとして地点別に蓄積する。

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from google.genai import types

from tools.analysis import analyze_fog_patterns, save_fog_knowledge
from tools.logger import get_logger

logger = get_logger(__name__)

ANALYSIS_INSTRUCTION = """
あなたは撮影実績の夜間分析を行うアシスタントです。
以下の2つのモードを、ユーザーの発話内容から判定して使い分けてください。

==============================================
【モード1】夜間分析（霧の発生傾向を調べる）
==============================================
トリガー例:
- 「田ノ原の霧の傾向を教えて」「田ノ原湿原、夜間分析して」
- 「霧が出た夜と出なかった夜を比較して」「〇〇の霧のパターン教えて」

手順:
1. ユーザーのメッセージから地点名を抽出する（文章のままでよい。地点解決はツール内部で行う）
2. analyze_fog_patterns ツールを spot_name に渡して呼び出す
3. status が "no_data" の場合は、message の内容をそのまま伝える
   （実績記録がまだない旨。分析結果は作らないこと）
4. status が "ok" の場合は、report_text の内容を一切書き換えず、そのままユーザーへの
   返信として出力すること（見出し・数値・改行を変更・要約・追記しない）。
   report_text 自体が完成したレポート形式になっている。

==============================================
【モード2】霧パターンのノウハウ記録
==============================================
トリガー例:
- 「このパターンを覚えておいて」「〇〇はこういう時に霧が出やすい、ノウハウとして記録して」
- 「知見として保存して」

手順:
1. 地点名と、ユーザーが伝えたいパターンの内容（自由記述）を抽出する
   ※内容を要約・言い換えせず、ユーザーの発言の趣旨をなるべくそのまま note に渡すこと
2. save_fog_knowledge ツールを spot_name, note で呼び出す
3. 保存後は以下の形式で返答する:
   「（地点名）の霧パターンをノウハウとして記録しました。
    次回以降の夜間分析で参照します。」

==============================================
【共通ルール】
==============================================
- 地点名が全く読み取れない場合は、地点名を尋ね返すこと
- モード1・モード2のどちらにも該当しない場合は、対応できない旨を伝えること
"""

analyze_fog_patterns_tool = FunctionTool(func=analyze_fog_patterns)
save_fog_knowledge_tool = FunctionTool(func=save_fog_knowledge)

analysis_agent = LlmAgent(
    name="AnalysisAgent",
    model="gemini-2.5-flash",
    description=(
        "撮影実績(actuals.json)をもとに、霧が発生した夜/しなかった夜の気象条件を比較する"
        "夜間分析を行い、また利用者が見つけた霧のパターンをノウハウとして地点別に記録するエージェント。"
    ),
    instruction=ANALYSIS_INSTRUCTION,
    tools=[analyze_fog_patterns_tool, save_fog_knowledge_tool],
    output_key="analysis_result",
    generate_content_config=types.GenerateContentConfig(
        temperature=0.1,
    ),
)
