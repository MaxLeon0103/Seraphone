import os
import json
import requests
from typing import Dict, List, Any

from app.common.config import cfg


class AIDraftAnalyzer:
    """Lightweight draft analyzer.

    Design:
    - Rule layer first (fast, deterministic)
    - Optional AI layer (rewrite/strategy expansion)
    """

    def __init__(self):
        self.timeout = 2.2

    def _safe_winrate(self, games: List[Dict[str, Any]]) -> float:
        if not games:
            return 0.5
        valid = [g for g in games[:10] if isinstance(g, dict)]
        if not valid:
            return 0.5
        wins = sum(1 for g in valid if g.get("win"))
        return wins / len(valid)

    def _safe_kda(self, games: List[Dict[str, Any]]) -> float:
        if not games:
            return 2.0
        valid = [g for g in games[:10] if isinstance(g, dict)]
        if not valid:
            return 2.0
        kills = sum(float(g.get("kills", 0)) for g in valid)
        deaths = sum(float(g.get("deaths", 0)) for g in valid)
        assists = sum(float(g.get("assists", 0)) for g in valid)
        return (kills + assists) / max(1.0, deaths)

    def analyze(self, ally_summoners: List[Dict[str, Any]], champ_select_data: Dict[str, Any], queue_id: int = None) -> Dict[str, Any]:
        ally_wr = []
        ally_kda = []

        for s in ally_summoners:
            games = s.get("gamesInfo", [])
            ally_wr.append(self._safe_winrate(games))
            ally_kda.append(self._safe_kda(games))

        avg_wr = sum(ally_wr) / len(ally_wr) if ally_wr else 0.5
        avg_kda = sum(ally_kda) / len(ally_kda) if ally_kda else 2.0

        my_team = champ_select_data.get("myTeam", [])
        their_team = champ_select_data.get("theirTeam", [])

        ally_locked = sum(1 for x in my_team if int(x.get("championId", 0) or 0) > 0)
        enemy_locked = sum(1 for x in their_team if int(x.get("championId", 0) or 0) > 0)

        balance_score = int(max(20, min(95, avg_wr * 70 + avg_kda * 10 + 20)))

        risk = "low"
        if avg_wr < 0.48 or avg_kda < 2.0:
            risk = "high"
        elif avg_wr < 0.53:
            risk = "medium"

        line1 = f"阵容/状态评分：{balance_score}/100（风险：{risk}）"
        line2 = f"锁定进度：我方 {ally_locked}/5，敌方 {enemy_locked}/5"

        actions = []
        if risk == "high":
            actions.append("前10分钟以稳线和视野为核心，避免无信息开战")
            actions.append("优先做容错装备与保命符文，减少被秒概率")
            actions.append("围绕有优势路做2人以上联动，不单点硬打")
        elif risk == "medium":
            actions.append("对线期控线找窗口，优先拿首个中立资源")
            actions.append("第一件装备后再主动发起节奏，不提前赌团")
            actions.append("通过小规模多打少建立滚雪球")
        else:
            actions.append("保持节奏压制，优先先锋/小龙交换最大化")
            actions.append("利用强势时间窗主动开团，逼出关键技能")
            actions.append("优势转地图资源，不做无意义追击")

        result = {
            "team_balance_score": balance_score,
            "lane_risk": risk,
            "summary": [line1, line2],
            "next_actions": actions,
            "build_hint": "敌方控制多则优先韧性；爆发高则优先保命；均势按核心三件套推进。",
        }

        ai = self._ai_rewrite(result)
        if ai:
            result["ai_summary"] = ai

        return result

    def _ai_rewrite(self, result: Dict[str, Any]) -> str:
        api_key = cfg.get(cfg.aiApiKey) or os.getenv("SERAPHINE_AI_API_KEY", "")
        if not api_key:
            return ""

        base_url = cfg.get(cfg.aiBaseUrl) or os.getenv("SERAPHINE_AI_BASE_URL", "https://api.openai.com/v1")
        model = cfg.get(cfg.aiModel) or os.getenv("SERAPHINE_AI_MODEL", "gpt-4o-mini")
        timeout = float(cfg.get(cfg.aiTimeoutSeconds) or os.getenv("SERAPHINE_AI_TIMEOUT", "2.2"))

        try:
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": "你是LOL BP分析助手。输出必须短、可执行、禁止空话。"},
                    {"role": "user", "content": "将以下JSON改写成3行中文策略建议：" + json.dumps(result, ensure_ascii=False)},
                ],
                "temperature": 0.2,
            }
            r = requests.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=timeout,
            )
            if r.status_code != 200:
                return ""
            data = r.json()
            return data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        except Exception:
            return ""

    def push_to_telegram(self, title: str, analysis: Dict[str, Any], match_key: str = "") -> bool:
        if not cfg.get(cfg.enableAiTelegramPush):
            return False
        token = cfg.get(cfg.aiTelegramBotToken)
        chat_id = cfg.get(cfg.aiTelegramChatId)
        if not token or not chat_id:
            return False

        summary = analysis.get("summary", [])
        actions = analysis.get("next_actions", [])
        ai_summary = analysis.get("ai_summary", "")
        msg = [
            f"🎮 {title}",
            f"局标识: {match_key}" if match_key else "",
            *summary[:2],
            "",
            "📌 即时动作:",
            *[f"- {x}" for x in actions[:3]],
            "",
            f"🧠 AI: {ai_summary}" if ai_summary else "",
            f"🛠 出装提示: {analysis.get('build_hint','')}"
        ]
        text = "\n".join([x for x in msg if x])

        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": str(chat_id), "text": text},
                timeout=6,
            )
            return r.status_code == 200
        except Exception:
            return False


analyzer = AIDraftAnalyzer()
