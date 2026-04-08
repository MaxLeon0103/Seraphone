import os
import json
import math
import requests
import asyncio
from typing import Dict, List, Any, Tuple

from app.common.config import cfg
from app.lol.opgg import opgg
from app.lol.connector import connector


class AIDraftAnalyzer:
    """Draft / in-game analyzer (lightweight, explainable).

    目标：
    1) 不依赖 AI 也能输出有价值、可解释的结果
    2) 明确告诉用户“拿到了多少数据、风险从何而来”
    3) 支持可选 AI 文案润色（失败不影响核心分析）
    """

    def __init__(self):
        self.timeout = 2.2
        self._zh_item_name_map = None

    # -----------------------------
    # 基础工具
    # -----------------------------
    @staticmethod
    def _clamp(v: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, v))

    @staticmethod
    def _safe_float(v: Any, default: float = 0.0) -> float:
        try:
            return float(v)
        except Exception:
            return default

    @staticmethod
    def _mean(values: List[float], default: float = 0.0) -> float:
        if not values:
            return default
        return sum(values) / len(values)

    # -----------------------------
    # 段位评分
    # -----------------------------
    def _tier_base_score(self, tier_name: str) -> float:
        tier = (tier_name or "").strip().upper()

        # 英文 tier
        english_map = {
            "IRON": 900,
            "BRONZE": 1050,
            "SILVER": 1200,
            "GOLD": 1350,
            "PLATINUM": 1500,
            "EMERALD": 1650,
            "DIAMOND": 1800,
            "MASTER": 2000,
            "GRANDMASTER": 2200,
            "CHALLENGER": 2400,
            "UNRANKED": 800,
        }

        # 中文短称（parseRankInfo 后常见）
        cn_map = {
            "黑铁": 900,
            "黄铜": 1050,
            "白银": 1200,
            "黄金": 1350,
            "铂金": 1500,
            "翡翠": 1650,
            "钻石": 1800,
            "大师": 2000,
            "宗师": 2200,
            "王者": 2400,
            "未定级": 800,
            "未知": 800,
        }

        if tier in english_map:
            return float(english_map[tier])

        # 处理 icon 路径中 tier 文本不标准的情况
        for k, v in english_map.items():
            if k in tier:
                return float(v)

        # 走中文映射
        cn = (tier_name or "").strip()
        return float(cn_map.get(cn, 800))

    def _division_score(self, division: str) -> float:
        # I > II > III > IV
        d = (division or "").strip().upper()
        mapping = {"I": 60, "II": 45, "III": 30, "IV": 15}
        return float(mapping.get(d, 0))

    def _extract_tier_from_icon(self, icon_path: str) -> str:
        # e.g. app/resource/images/DIAMOND.svg
        if not icon_path:
            return ""
        name = icon_path.replace("\\", "/").split("/")[-1]
        if "." in name:
            name = name.split(".")[0]
        return name.upper()

    def _rank_entry_score(self, rank_entry: Dict[str, Any]) -> float:
        if not isinstance(rank_entry, dict):
            return 800.0

        icon_tier = self._extract_tier_from_icon(rank_entry.get("icon", ""))
        text_tier = rank_entry.get("tier", "")
        tier_base = self._tier_base_score(icon_tier or text_tier)

        division = rank_entry.get("division", "")
        lp = self._safe_float(rank_entry.get("lp", 0), 0)
        lp_bonus = self._clamp(lp, 0, 100) * 0.25

        return tier_base + self._division_score(division) + lp_bonus

    def _rank_score(self, rank_info: Dict[str, Any]) -> float:
        if not isinstance(rank_info, dict):
            return 800.0

        solo = self._rank_entry_score(rank_info.get("solo", {}))
        flex = self._rank_entry_score(rank_info.get("flex", {}))
        return max(solo, flex)

    # -----------------------------
    # 个人指标
    # -----------------------------
    def _normalize_games(self, games: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not isinstance(games, list):
            return []

        valid = [g for g in games if isinstance(g, dict)]
        # 默认取最近 10 场，提高时效性
        valid = valid[:10]

        # 尽量剔除 remake
        non_remake = [g for g in valid if not g.get("remake", False)]
        return non_remake if non_remake else valid

    def _player_metrics(self, s: Dict[str, Any]) -> Dict[str, Any]:
        games = self._normalize_games((s or {}).get("gamesInfo", []))
        sample = len(games)

        if sample == 0:
            wr = 0.5
            kda = 2.0
            recent_form = 0.5
            avg_deaths = 6.0
            champ_pool = 0
            streak_type = "none"
            streak_count = 0
            streak_signed = 0
        else:
            wins = sum(1 for g in games if g.get("win"))
            wr = wins / sample

            kills = sum(self._safe_float(g.get("kills", 0)) for g in games)
            deaths = sum(self._safe_float(g.get("deaths", 0)) for g in games)
            assists = sum(self._safe_float(g.get("assists", 0)) for g in games)
            kda = (kills + assists) / max(1.0, deaths)
            avg_deaths = deaths / max(1, sample)

            recent = games[:5]
            if recent:
                recent_form = sum(1 for g in recent if g.get("win")) / len(recent)
            else:
                recent_form = wr

            champ_pool = len({int(g.get("championId", 0) or 0) for g in games if int(g.get("championId", 0) or 0) > 0})

            # 连胜/连败（按最近到更早）
            streak_type = "none"
            streak_count = 0
            streak_signed = 0
            first = games[0].get("win", None)
            if isinstance(first, bool):
                streak_type = "win" if first else "loss"
                for g in games:
                    if g.get("win", None) is first:
                        streak_count += 1
                    else:
                        break
                streak_signed = streak_count if first else -streak_count

        rank_score = self._rank_score((s or {}).get("rankInfo", {}))
        level = self._safe_float((s or {}).get("level", 0), 0)

        state = "中性"
        if sample < 3:
            state = "样本不足"
        elif recent_form <= 0.25 and wr < 0.46:
            state = "低迷"
        elif recent_form >= 0.70 and wr >= 0.55:
            state = "火热"
        elif kda >= 3.2 and wr >= 0.52:
            state = "稳定"
        elif avg_deaths >= 7.0:
            state = "偏激进"

        return {
            "name": (s or {}).get("name", "未知玩家"),
            "level": level,
            "sample": sample,
            "wr": wr,
            "kda": kda,
            "recent_form": recent_form,
            "avg_deaths": avg_deaths,
            "champ_pool": champ_pool,
            "rank_score": rank_score,
            "state": state,
            "streak_type": streak_type,
            "streak_count": streak_count,
            "streak_signed": streak_signed,
        }


    def _team_metrics(self, players: List[Dict[str, Any]]) -> Dict[str, Any]:
        metrics = [self._player_metrics(p) for p in (players or [])]
        covered = [m for m in metrics if m["sample"] > 0]

        team = {
            "players": metrics,
            "count": len(metrics),
            "covered": len(covered),
            "coverage_rate": (len(covered) / len(metrics)) if metrics else 0.0,
            "wr": self._mean([m["wr"] for m in covered], 0.5),
            "kda": self._mean([m["kda"] for m in covered], 2.0),
            "recent_form": self._mean([m["recent_form"] for m in covered], 0.5),
            "level": self._mean([m["level"] for m in covered], 0.0),
            "rank_score": self._mean([m["rank_score"] for m in covered], 800.0),
            "high_risk_cnt": len([m for m in covered if (m["state"] in ("低迷", "偏激进"))]),
            "hot_cnt": len([m for m in covered if m["state"] == "火热"]),
        }

        # 强度指标（仅做相对比较，不是绝对胜率）
        kda_norm = self._clamp((team["kda"] - 1.5) / 3.5, 0, 1)
        rank_norm = self._clamp((team["rank_score"] - 900) / 1500, 0, 1)
        lvl_norm = self._clamp(team["level"] / 600, 0, 1)

        strength = (
            team["wr"] * 0.40 +
            kda_norm * 0.20 +
            rank_norm * 0.22 +
            team["recent_form"] * 0.13 +
            lvl_norm * 0.05
        )
        team["strength"] = self._clamp(strength, 0, 1)
        return team

    # -----------------------------
    # 风险 / 动作建议
    # -----------------------------
    def _risk_and_factors(self, ally: Dict[str, Any], enemy: Dict[str, Any]) -> Tuple[int, str, List[str], List[str]]:
        # risk_score 越高越危险
        risk_score = 50.0
        risk_factors: List[str] = []
        opportunities: List[str] = []

        wr_diff = enemy["wr"] - ally["wr"]
        if wr_diff > 0.05:
            risk_score += wr_diff * 120
            risk_factors.append(f"近期胜率劣势（我方{ally['wr']:.0%} vs 敌方{enemy['wr']:.0%}）")
        elif wr_diff < -0.05:
            risk_score += wr_diff * 80
            opportunities.append(f"近期胜率占优（我方{ally['wr']:.0%} vs 敌方{enemy['wr']:.0%}）")

        kda_diff = enemy["kda"] - ally["kda"]
        if kda_diff > 0.45:
            risk_score += kda_diff * 10
            risk_factors.append(f"对手团战效率更高（KDA {ally['kda']:.2f} vs {enemy['kda']:.2f}）")
        elif kda_diff < -0.35:
            risk_score += kda_diff * 8
            opportunities.append(f"我方团战效率更高（KDA {ally['kda']:.2f} vs {enemy['kda']:.2f}）")

        rank_gap = enemy["rank_score"] - ally["rank_score"]
        if rank_gap > 120:
            risk_score += rank_gap / 15
            risk_factors.append(f"段位/LP 均值落后（约{rank_gap:.0f}分）")
        elif rank_gap < -120:
            risk_score += rank_gap / 20
            opportunities.append(f"段位/LP 均值领先（约{-rank_gap:.0f}分）")

        form_diff = enemy["recent_form"] - ally["recent_form"]
        if form_diff > 0.15:
            risk_score += form_diff * 35
            risk_factors.append("近期状态不如对手（近5场趋势偏弱）")
        elif form_diff < -0.12:
            risk_score += form_diff * 25
            opportunities.append("近期状态优于对手（近5场趋势偏强）")

        if ally["high_risk_cnt"] >= 2:
            risk_score += 8
            risk_factors.append("我方低迷/高波动玩家较多")
        if enemy["high_risk_cnt"] >= 2:
            risk_score -= 6
            opportunities.append("敌方高波动点较多，可针对失误滚雪球")

        # 数据覆盖不足时，提升不确定性
        coverage_floor = min(ally["coverage_rate"], enemy["coverage_rate"])
        if coverage_floor < 0.6:
            risk_factors.append("样本覆盖不足，结论置信度下降")

        risk_score = self._clamp(risk_score, 10, 95)
        if risk_score >= 68:
            lane_risk = "high"
        elif risk_score >= 48:
            lane_risk = "medium"
        else:
            lane_risk = "low"

        return int(round(risk_score)), lane_risk, risk_factors[:4], opportunities[:4]

    def _build_actions(self, lane_risk: str, ally: Dict[str, Any], enemy: Dict[str, Any], risks: List[str], opps: List[str]) -> List[str]:
        actions: List[str] = []

        if lane_risk == "high":
            actions.extend([
                "前10分钟优先控线+视野，不做无信息先手。",
                "优先围绕我方状态最好的一路打2v1/3v2，小规模建立经济差。",
                "团前先逼关键技能（闪现/大招）再接资源团。",
            ])
        elif lane_risk == "medium":
            actions.extend([
                "第一波中立资源前以换血与线权为主，避免盲目开团。",
                "让状态好的队友拿到第一波节奏权（先锋/小龙二选一）。",
                "团战优先保护输出位，别把前排资源浪费在追击上。",
            ])
        else:
            actions.extend([
                "保持主动换资源节奏，优先先锋→镀层→小龙链路。",
                "利用我方强势窗口先手开团，打完立即转地图目标。",
                "优势局减少高风险越塔和深追，稳住胜率曲线。",
            ])

        if risks:
            actions.append(f"本局首要风险：{risks[0]}。")
        if opps:
            actions.append(f"可利用机会：{opps[0]}。")

        return actions[:5]

    def _build_hint(self, ally: Dict[str, Any], enemy: Dict[str, Any], lane_risk: str) -> str:
        if lane_risk == "high":
            return "优先容错：韧性/减伤/保命装提前一件；避免纯输出贪伤害。"
        if enemy["kda"] - ally["kda"] > 0.4:
            return "对手团战效率高，先做抗性与反开道具，等关键技能真空再反打。"
        if ally["strength"] > enemy["strength"] + 0.08:
            return "我方强度领先，按核心两件套提速，资源团前做视野压迫。"
        return "均势局：按核心三件套推进，先手前确保视野和技能状态。"

    def _team_vs_lines(self, ally: Dict[str, Any], enemy: Dict[str, Any]) -> List[str]:
        return [
            f"胜率：我方 {ally['wr']:.0%} vs 敌方 {enemy['wr']:.0%}",
            f"KDA：我方 {ally['kda']:.2f} vs 敌方 {enemy['kda']:.2f}",
            f"段位分：我方 {ally['rank_score']:.0f} vs 敌方 {enemy['rank_score']:.0f}",
            f"状态分：我方 {ally['recent_form']:.0%} vs 敌方 {enemy['recent_form']:.0%}",
            f"等级均值：我方 {ally['level']:.0f} vs 敌方 {enemy['level']:.0f}",
        ]

    def _player_brief_lines(self, players: List[Dict[str, Any]], title: str, top_n: int = 3, reverse: bool = True) -> List[str]:
        # reverse=True 取强点；False 取风险点
        if not players:
            return [f"{title}：无样本"]

        def score(p):
            # 玩家综合指数（用于排序，不是绝对实力）
            kda_norm = self._clamp((p["kda"] - 1.2) / 3.8, 0, 1)
            rank_norm = self._clamp((p["rank_score"] - 900) / 1500, 0, 1)
            return p["wr"] * 0.45 + kda_norm * 0.25 + p["recent_form"] * 0.20 + rank_norm * 0.10

        arr = [p for p in players if p.get("sample", 0) > 0]
        if not arr:
            return [f"{title}：样本不足"]

        arr = sorted(arr, key=score, reverse=reverse)[:top_n]
        lines = [f"{title}:"]
        for p in arr:
            lines.append(
                f"- {p['name']} | WR {p['wr']:.0%} | KDA {p['kda']:.2f} | 近5场 {p['recent_form']:.0%} | {p['state']}"
            )
        return lines

    def _get_zh_item_name(self, item_id: int, fallback_name: str = "") -> str:
        """尽量返回中文装备名；失败则回退到原名。"""
        if item_id <= 0:
            return fallback_name or str(item_id)

        if self._zh_item_name_map is None:
            self._zh_item_name_map = {}
            urls = [
                "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/zh_cn/v1/items.json",
                "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default/v1/items.json",
            ]
            for u in urls:
                try:
                    r = requests.get(u, timeout=2.8)
                    if r.status_code != 200:
                        continue
                    arr = r.json()
                    m = {}
                    for it in arr:
                        iid = int(it.get("id", 0) or 0)
                        if iid <= 0:
                            continue
                        name = str(it.get("name", "") or "").strip()
                        if name:
                            m[iid] = name
                    if m:
                        self._zh_item_name_map = m
                        break
                except Exception:
                    continue

        if self._zh_item_name_map and item_id in self._zh_item_name_map:
            return self._zh_item_name_map[item_id]

        return fallback_name or str(item_id)

    async def _fetch_opgg_build_hint(self, champ_select_data: Dict[str, Any], queue_id: int = None) -> Dict[str, Any]:
        """读取当前英雄在 OPGG 的推荐构筑（核心三件/起手/鞋子）。

        返回示例:
        {
            "ok": True,
            "champion": "Ahri",
            "mode": "ranked",
            "position": "MID",
            "win_rate": 0.518,
            "pick_rate": 0.072,
            "core_items": ["卢登", "影焰", "帽子"]
        }
        """
        try:
            my_team = (champ_select_data or {}).get("myTeam", [])
            if not my_team:
                return {"ok": False, "reason": "no_my_team"}

            champion_id = 0
            position = ""

            # 优先用已锁定英雄
            for p in my_team:
                cid = int((p or {}).get("championId", 0) or 0)
                if cid > 0:
                    champion_id = cid
                    pos = (p or {}).get("assignedPosition", "")
                    if pos:
                        position = pos
                    break

            # 兜底：意向英雄
            if champion_id <= 0:
                for p in my_team:
                    cid = int((p or {}).get("championPickIntent", 0) or 0)
                    if cid > 0:
                        champion_id = cid
                        pos = (p or {}).get("assignedPosition", "")
                        if pos:
                            position = pos
                        break

            if champion_id <= 0:
                return {"ok": False, "reason": "no_champion"}

            # 优先遵循用户在 OP.GG 窗口当前选择的模式；否则回落为队列映射
            mode = ""
            try:
                mode = cfg.get(cfg.opggMode)
            except Exception:
                mode = ""

            if mode not in ("ranked", "aram", "arena", "urf", "nexus_blitz"):
                mode = "ranked"
                if queue_id in (450,):
                    mode = "aram"
                elif queue_id in (1700, 1710):
                    mode = "arena"
                elif queue_id in (1300,):
                    mode = "nexus_blitz"
                elif queue_id in (900, 1900):
                    mode = "urf"

            pos_map = {
                "TOP": "TOP",
                "JUNGLE": "JUNGLE",
                "MIDDLE": "MID",
                "BOTTOM": "ADC",
                "UTILITY": "SUPPORT",
                "MID": "MID",
                "ADC": "ADC",
                "SUPPORT": "SUPPORT",
            }
            opgg_position = pos_map.get((position or "").upper(), "")

            # ranked 必须给 position，若没有则交给 getChampionBuild 自动选 positions[0]
            position_arg = opgg_position if opgg_position else "none"

            region = cfg.get(cfg.opggRegion)
            tier = cfg.get(cfg.opggTier)

            # 带超时保护，避免分析阻塞
            data = await asyncio.wait_for(
                opgg.getChampionBuild(region, mode, champion_id, position_arg, tier),
                timeout=2.8
            )
            parsed = (data or {}).get("data", {})
            summary = parsed.get("summary", {})
            items = parsed.get("items", {})

            def _extract_top_items(item_groups, top_n=3):
                res = []
                for g in (item_groups or [])[:top_n]:
                    icons = g.get("icons", []) if isinstance(g, dict) else []
                    if not icons:
                        continue

                    # icon path: .../item icons/3031.png
                    first_icon = icons[0]
                    item_id = 0
                    try:
                        fname = str(first_icon).replace("\\", "/").split("/")[-1]
                        item_id = int(fname.split(".")[0])
                    except Exception:
                        item_id = 0

                    if item_id <= 0:
                        continue

                    item_name = connector.manager.getItemNameById(item_id)
                    zh_name = self._get_zh_item_name(item_id, item_name)
                    res.append({
                        "id": item_id,
                        "name": zh_name,
                    })

                return res

            core_items = _extract_top_items(items.get("coreItems", []), 3)
            start_items = _extract_top_items(items.get("startItems", []), 2)
            boots_items = _extract_top_items(items.get("boots", []), 1)

            return {
                "ok": True,
                "champion_id": champion_id,
                "champion": summary.get("name") or connector.manager.getChampionNameById(champion_id) or str(champion_id),
                "mode": mode,
                "position": summary.get("position") or (opgg_position or "none"),
                "win_rate": summary.get("winRate"),
                "pick_rate": summary.get("pickRate"),
                "core_items": core_items,
                "start_items": start_items,
                "boots_items": boots_items,
            }
        except Exception:
            return {"ok": False, "reason": "opgg_fetch_failed"}

    def _streak_alerts(self, ally_players: List[Dict[str, Any]], enemy_players: List[Dict[str, Any]]) -> List[str]:
        alerts: List[str] = []

        # 我方连败（>=2）需要重点提醒
        for p in ally_players:
            c = int(p.get("streak_count", 0) or 0)
            t = p.get("streak_type", "none")
            if t == "loss" and c >= 2:
                level = "高风险" if c >= 4 else "注意"
                alerts.append(f"⚠️ 我方 {p.get('name','未知')} 已连败 {c} 场（{level}，心态/决策波动可能上升）")

        # 敌方连胜（>=3）提醒威胁
        for p in enemy_players:
            c = int(p.get("streak_count", 0) or 0)
            t = p.get("streak_type", "none")
            if t == "win" and c >= 3:
                level = "高威胁" if c >= 5 else "威胁"
                alerts.append(f"🔥 敌方 {p.get('name','未知')} 已连胜 {c} 场（{level}，建议前期重点针对）")

        # 反向机会：我方连胜 / 敌方连败
        for p in ally_players:
            c = int(p.get("streak_count", 0) or 0)
            t = p.get("streak_type", "none")
            if t == "win" and c >= 3:
                alerts.append(f"✅ 我方 {p.get('name','未知')} 连胜 {c} 场（可围绕其打资源节奏）")

        for p in enemy_players:
            c = int(p.get("streak_count", 0) or 0)
            t = p.get("streak_type", "none")
            if t == "loss" and c >= 2:
                alerts.append(f"📉 敌方 {p.get('name','未知')} 连败 {c} 场（可施压其对线与视野）")

        # 去重并限制长度
        uniq = []
        for x in alerts:
            if x not in uniq:
                uniq.append(x)

        return uniq[:6]

    # -----------------------------
    # 主入口
    # -----------------------------
    async def analyze(
        self,
        ally_summoners: List[Dict[str, Any]],
        champ_select_data: Dict[str, Any],
        queue_id: int = None,
        enemy_summoners: List[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        ally_team = self._team_metrics(ally_summoners or [])
        enemy_team = self._team_metrics(enemy_summoners or [])

        risk_score, lane_risk, risk_factors, opportunities = self._risk_and_factors(ally_team, enemy_team)

        # balance_score 越高越偏向我方
        balance_score = int(self._clamp(50 + (ally_team["strength"] - enemy_team["strength"]) * 90, 10, 95))

        my_team = (champ_select_data or {}).get("myTeam", [])
        their_team = (champ_select_data or {}).get("theirTeam", [])
        ally_locked = sum(1 for x in my_team if int((x or {}).get("championId", 0) or 0) > 0)
        enemy_locked = sum(1 for x in their_team if int((x or {}).get("championId", 0) or 0) > 0)

        coverage_line = f"数据覆盖：我方 {ally_team['covered']}/{ally_team['count']}，敌方 {enemy_team['covered']}/{enemy_team['count']}"
        summary = [
            f"综合对局评分：{balance_score}/100（风险：{lane_risk}，风险分：{risk_score}）",
            coverage_line,
            f"锁定进度：我方 {ally_locked}/5，敌方 {enemy_locked}/5",
        ]

        team_vs = self._team_vs_lines(ally_team, enemy_team)

        ally_players = ally_team["players"]
        enemy_players = enemy_team["players"]

        ally_cores = self._player_brief_lines(ally_players, "我方状态较好", top_n=3, reverse=True)
        ally_risks = self._player_brief_lines(ally_players, "我方风险点", top_n=2, reverse=False)
        enemy_threats = self._player_brief_lines(enemy_players, "敌方高威胁点", top_n=3, reverse=True)

        next_actions = self._build_actions(lane_risk, ally_team, enemy_team, risk_factors, opportunities)
        opgg_build = await self._fetch_opgg_build_hint(champ_select_data, queue_id)
        build_hint = self._build_hint(ally_team, enemy_team, lane_risk)
        build_detail_lines: List[str] = []
        if opgg_build.get("ok"):
            champ = opgg_build.get("champion", "当前英雄")
            mode = opgg_build.get("mode", "ranked")
            pos = opgg_build.get("position", "none")
            core_items = opgg_build.get("core_items", [])
            start_items = opgg_build.get("start_items", [])
            boots_items = opgg_build.get("boots_items", [])

            def _fmt_items(arr):
                return " / ".join([f"{x.get('name','?')}({x.get('id','?')})" for x in arr if isinstance(x, dict)])

            build_hint = f"[{champ} | {mode}/{pos}] OPGG 推荐"
            build_detail_lines = [
                f"🎯 英雄: {champ}",
                f"🧭 模式/位置: {mode}/{pos}",
                f"🍼 起手: {_fmt_items(start_items) if start_items else '无'}",
                f"👟 鞋子: {_fmt_items(boots_items) if boots_items else '无'}",
                f"⚔️ 核心: {_fmt_items(core_items) if core_items else '无'}",
            ]

            wr = opgg_build.get("win_rate")
            pr = opgg_build.get("pick_rate")
            if isinstance(wr, (int, float)):
                build_detail_lines.append(f"📊 OPGG胜率: {wr * 100:.1f}%")
            if isinstance(pr, (int, float)):
                build_detail_lines.append(f"📈 登场率: {pr * 100:.1f}%")

        streak_alerts = self._streak_alerts(ally_players, enemy_players)

        result = {
            "team_balance_score": balance_score,
            "lane_risk": lane_risk,
            "risk_score": risk_score,
            "summary": summary,
            "data_coverage": {
                "ally": {"covered": ally_team["covered"], "total": ally_team["count"]},
                "enemy": {"covered": enemy_team["covered"], "total": enemy_team["count"]},
            },
            "team_compare": {
                "ally": {
                    "winrate": ally_team["wr"],
                    "kda": ally_team["kda"],
                    "rank_score": ally_team["rank_score"],
                    "recent_form": ally_team["recent_form"],
                    "avg_level": ally_team["level"],
                    "strength": ally_team["strength"],
                },
                "enemy": {
                    "winrate": enemy_team["wr"],
                    "kda": enemy_team["kda"],
                    "rank_score": enemy_team["rank_score"],
                    "recent_form": enemy_team["recent_form"],
                    "avg_level": enemy_team["level"],
                    "strength": enemy_team["strength"],
                },
            },
            "risk_factors": risk_factors,
            "opportunities": opportunities,
            "streak_alerts": streak_alerts,
            "ally_state_focus": ally_cores[1:],
            "ally_risk_focus": ally_risks[1:],
            "enemy_threats": enemy_threats[1:],
            "team_vs_lines": team_vs,
            "next_actions": next_actions,
            "build_hint": build_hint,
            "build_detail_lines": build_detail_lines,
        }

        ai = self._ai_rewrite(result)
        if ai:
            result["ai_summary"] = ai

        return result

    # -----------------------------
    # 可选 AI 文案润色
    # -----------------------------
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
                    {"role": "system", "content": "你是LOL对局分析助手。输出必须短、清晰、可执行。"},
                    {"role": "user", "content": "基于以下JSON写4行中文报告：1行结论+1行风险+1行机会+1行执行动作。JSON=" + json.dumps(result, ensure_ascii=False)},
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

    # -----------------------------
    # Telegram 推送
    # -----------------------------
    def push_to_telegram(self, title: str, analysis: Dict[str, Any], match_key: str = "") -> bool:
        if not cfg.get(cfg.enableAiTelegramPush):
            return False
        token = cfg.get(cfg.aiTelegramBotToken)
        chat_id = cfg.get(cfg.aiTelegramChatId)
        if not token or not chat_id:
            return False

        summary = analysis.get("summary", [])
        team_vs = analysis.get("team_vs_lines", [])
        risks = analysis.get("risk_factors", [])
        opps = analysis.get("opportunities", [])
        streak_alerts = analysis.get("streak_alerts", [])
        ally_focus = analysis.get("ally_state_focus", [])
        enemy_threats = analysis.get("enemy_threats", [])
        actions = analysis.get("next_actions", [])
        build_lines = analysis.get("build_detail_lines", [])
        ai_summary = analysis.get("ai_summary", "")

        parts = [
            f"🎮 {title}",
            f"🆔 局标识: {match_key}" if match_key else "",
            "━━━━━━━━━━━━━━",
            f"📌 {summary[0]}" if len(summary) > 0 else "",
            f"🧾 {summary[1]}" if len(summary) > 1 else "",
            f"🔒 {summary[2]}" if len(summary) > 2 else "",
            "",
            "⚖️ 双方实力对比",
            *[f"  • {x}" for x in team_vs[:5]],
            "",
            "🚨 连胜/连败预警",
            *([f"  • {x}" for x in streak_alerts[:5]] if streak_alerts else ["  • 暂无明显连胜/连败异常"]),
            "",
            "👥 我方状态重点",
            *[f"  • {x}" for x in ally_focus[:3]],
            "",
            "🧨 敌方威胁点",
            *[f"  • {x}" for x in enemy_threats[:3]],
            "",
            "⚠️ 风险来源",
            *([f"  • {x}" for x in risks[:3]] if risks else ["  • 未发现明显硬风险"]),
            "",
            "📈 可利用机会",
            *([f"  • {x}" for x in opps[:3]] if opps else ["  • 机会点不明显，建议稳健运营"]),
            "",
            "📌 即时动作",
            *[f"  {i+1}. {x}" for i, x in enumerate(actions[:4])],
            "",
            "🛠 出装提示（OP.GG）",
            *([f"  {x}" for x in build_lines] if build_lines else [f"  {analysis.get('build_hint','')}"]),
            f"🧠 AI补充\n  {ai_summary}" if ai_summary else "",
        ]

        text = "\n".join([x for x in parts if x])

        # 过长时截断，避免 Telegram sendMessage 超限
        if len(text) > 3600:
            text = text[:3550] + "\n...（已截断）"

        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": str(chat_id), "text": text},
                timeout=8,
            )
            return r.status_code == 200
        except Exception:
            return False


analyzer = AIDraftAnalyzer()
