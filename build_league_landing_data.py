#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AnySport 联赛落地页数据采集脚本
================================
给定一个或多个 league_id，拉取构建 SEO 落地页所需的全部数据块，输出结构化 JSON。

内容块（对应文档接口）：
  - 联赛概览        /leagues            (en + zh，名称/国家本地化)
  - 赛季列表        /seasons            (含 has_results / has_standings 标记)
  - 当前积分榜      /standings          (en + zh，队名本地化)
  - 历史积分榜      /standings          (逐赛季，用于历年冠军 + per-season 子页)
  - 历年冠军榜      派生自历史 standings position=1  ← 核心独特内容
  - 荣誉榜(夺冠次数) 派生自历年冠军
  - 射手/助攻/纪律榜 /topscorers        (goals/assists/cards × en+zh)
  - 当季赛程赛果    /matches            (当前赛季全量，分页；用于赛果 + 派生统计)
  - 未来赛程        /matches/window     (0~45 天，休赛期可能为空)
  - 派生统计        本地计算(场均进球/最大比分/主客胜率等)
  - 队名映射        team_name_map，供前端把历史/比赛数据本地化

用法：
  python build_league_landing_data.py                 # 跑内置的德甲 + 法罗甲
  ANYSPORT_API_KEY=xxx python build_league_landing_data.py
  # 后续跑全部联赛：见文件底部 run_all() 说明
"""
import glob
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone

# Windows 控制台默认 GBK，强制 UTF-8 输出避免打印中文/符号报错
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa
    pass

# ---------------------------------------------------------------- 配置
API_KEY = os.environ.get(
    "ANYSPORT_API_KEY",
    "7d358433d1cfd196a7eb0403ce9316bd0dc96178f0e698df87f058ac556c661b",
)
BASE = "https://data.anysport.io/v1/soccer"
OUT_DIR = os.path.dirname(os.path.abspath(__file__))
# 每次请求后固定间隔。默认 1.1 兼容免费/基础(<=1 req/s)；高级套餐(6 req/s、20000/hr)
# 跑全量时用 ANYSPORT_SLEEP=0.2 覆盖（约 5 req/s，同时低于每秒与每小时上限）。
SLEEP = float(os.environ.get("ANYSPORT_SLEEP", "1.1"))
LANGS = ("en", "zh-CN")

# 本次目标联赛（slug -> league_id）。slug 用于文件名和后续 URL。
LEAGUES = {
    "bundesliga": "asl_30c1e028124f46529e230d780bbe0e9e",          # 德国甲级联赛
    "faroe-islands-premier-league": "asl_b56effba61534f26ab9587becdfb5376",  # 法罗群岛甲级联赛
}

# ---------------------------------------------------------------- 请求层
_stats = {"calls": 0}


def api_get(path, **params):
    """GET 一个接口，返回解析后的 JSON dict；失败返回 None。含 429 退避。"""
    params = {k: v for k, v in params.items() if v is not None}
    qs = urllib.parse.urlencode(params)
    url = f"{BASE}{path}?{qs}" if qs else f"{BASE}{path}"
    req = urllib.request.Request(url, headers={"apikey": API_KEY})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                _stats["calls"] += 1
                data = json.loads(resp.read().decode("utf-8"))
                time.sleep(SLEEP)
                return data
        except urllib.error.HTTPError as e:
            _stats["calls"] += 1
            if e.code == 429:
                wait = 5 * (attempt + 1)
                print(f"    · 429 限流，等待 {wait}s 后重试")
                time.sleep(wait)
                continue
            body = e.read().decode("utf-8", "ignore")[:200]
            print(f"    ! HTTP {e.code} {path} {params} -> {body}")
            return None
        except Exception as e:  # noqa
            print(f"    ! 请求异常 {path} {params}: {e}")
            time.sleep(2)
    return None


def get_data(path, **params):
    j = api_get(path, **params)
    return (j.get("data") or []) if j else []


def get_all_pages(path, **params):
    """自动翻页，返回全部 data 拼接。"""
    out, page = [], 1
    while True:
        j = api_get(path, page=page, limit=100, **params)
        if not j:
            break
        data = j.get("data") or []
        out.extend(data)
        total_pages = (j.get("meta") or {}).get("total_pages", 1) or 1
        if page >= total_pages or not data:
            break
        page += 1
    return out


# ---------------------------------------------------------------- 单联赛构建
def build_league(slug, league_id):
    print(f"\n=== 构建 [{slug}] {league_id} ===")
    name_map = defaultdict(dict)  # team_id -> {en, zh}

    # 1) 联赛概览（双语）
    le = (get_data("/leagues", league_id=league_id, lang="en") or [{}])[0]
    lz = (get_data("/leagues", league_id=league_id, lang="zh-CN") or [{}])[0]
    meta = {
        "name": {"en": le.get("name"), "zh": lz.get("name")},
        "country": {"en": le.get("country"), "zh": lz.get("country")},
        "country_id": le.get("country_id"),
        "is_cup": le.get("is_cup"),
    }
    print(f"    联赛: {meta['name']['zh']} / {meta['name']['en']}")

    # 2) 赛季列表
    seasons = get_data("/seasons", league_id=league_id)
    seasons_with_standings = [s["season"] for s in seasons if s.get("has_standings")]
    print(f"    赛季总数 {len(seasons)}，其中有积分榜 {len(seasons_with_standings)}")

    # 3) 历史积分榜（逐赛季，en）。注意：不少赛季虽标 has_standings 但实际返回空表。
    historical_standings = {}
    for season in seasons_with_standings:  # 已按赛季倒序
        tbl = get_data("/standings", league_id=league_id, season=season, lang="en")
        historical_standings[season] = tbl
        for row in tbl:
            tid = row.get("team_id")
            if tid:
                name_map[tid]["en"] = row.get("team")

    # 3b) 选“当前赛季”= 最新的、真正踢过球(played>0)的赛季，跳过未开踢的占位表
    current_season = None
    for season in seasons_with_standings:
        tbl = historical_standings.get(season) or []
        if any((r.get("played") or 0) > 0 for r in tbl):
            current_season = season
            break

    # 3c) 拉当前赛季比赛，判断该赛季是否仍在进行中（存在未完赛比赛）
    #     注意：/matches 的跨年赛季用斜杠(2025/2026)，与 /seasons 的连字符(2025-2026)不同
    matches = get_all_pages("/matches", league_id=league_id, season=_matches_season(current_season), lang="zh-CN") if current_season else []
    current_in_progress = any(_is_unfinished(m.get("status")) for m in matches)
    print(f"    当前赛季 = {current_season}（{'进行中' if current_in_progress else '已结束'}），"
          f"当季 {len(matches)} 场")

    # 3d) 历年冠军：排除空壳赛季 & 仍在进行中的当前赛季（其榜首只是当前领头羊，不是冠军）
    champions = []
    for season in seasons_with_standings:
        tbl = historical_standings.get(season) or []
        if not tbl:
            continue
        champ = min(tbl, key=lambda r: r.get("position", 999))
        if (champ.get("played") or 0) == 0:
            continue  # 未开踢的占位表
        if not champ.get("team_id"):
            continue  # 脏数据：榜首行缺 team_id，跳过
        if season == current_season and current_in_progress:
            continue  # 赛季未决出冠军
        champions.append({
            "season": season,
            "team_id": champ["team_id"],
            "team": {"en": champ.get("team"), "zh": None},
            "points": champ.get("points"),
            "played": champ.get("played"),
            "wins": champ.get("wins"),
            "draws": champ.get("draws"),
            "losses": champ.get("losses"),
            "goals_for": champ.get("goals_for"),
            "goals_against": champ.get("goals_against"),
            "goal_difference": champ.get("goal_difference"),
        })
    print(f"    历年冠军记录 {len(champions)} 条")

    # 4) 当前赛季积分榜（zh，补齐中文队名）
    standings_zh = get_data("/standings", league_id=league_id, season=current_season, lang="zh-CN") if current_season else []
    for row in standings_zh:
        tid = row.get("team_id")
        if tid:
            name_map[tid]["zh"] = row.get("team")
    standings_en = historical_standings.get(current_season, [])

    # 5) 补齐历年冠军的中文名（当前赛季的已在 name_map，历史冠军按 team_id 单查一次）
    for c in champions:
        tid = c["team_id"]
        if "zh" not in name_map[tid]:
            t = get_data("/teams", team_id=tid, lang="zh-CN")
            if t:
                name_map[tid]["zh"] = t[0].get("name")
        c["team"]["zh"] = name_map[tid].get("zh")

    # 6) 荣誉榜（夺冠次数排行）
    title_counter = Counter(c["team_id"] for c in champions)
    honours = [
        {
            "team_id": tid,
            "team": {"en": name_map[tid].get("en"), "zh": name_map[tid].get("zh")},
            "titles": n,
            "seasons": [c["season"] for c in champions if c["team_id"] == tid],
        }
        for tid, n in title_counter.most_common()
    ]

    # 7) 射手 / 助攻 / 纪律榜（双语）
    top_scorers = {}
    for kind in ("goals", "assists", "cards"):
        top_scorers[kind] = {
            "en": get_data("/topscorers", league_id=league_id, season=current_season, kind=kind, lang="en") if current_season else [],
            "zh": get_data("/topscorers", league_id=league_id, season=current_season, kind=kind, lang="zh-CN") if current_season else [],
        }

    # 8) 当季赛程赛果已在 3c 拉取；此处补记球队名称到 name_map（比赛接口名称仅 en）
    for m in matches:
        for side in ("home", "away"):
            tid = m.get(side, {}).get("team_id")
            if tid and "en" not in name_map[tid]:
                name_map[tid]["en"] = m[side].get("name")

    # 9) 未来 45 天赛程（休赛期可能为空）
    upcoming = get_data("/matches/window", from_days=0, to_days=45, league_id=league_id, lang="zh-CN")

    # 10) 派生统计（当前赛季，基于已完赛比分）
    stats = compute_stats(matches, standings_en)

    # 11) 覆盖深度汇总（各联赛数据类型一致，差异在可追溯年份 → 深度即卖点）
    #     用“实际返回非空的赛季”，而非 has_standings 标记（标记常虚高）
    seasons_std = [
        s for s in seasons_with_standings
        if any((r.get("played") or 0) > 0 for r in (historical_standings.get(s) or []))
    ]
    results_seasons = [s["season"] for s in seasons if s.get("has_results")]
    # 球队总数（去重）：所有有积分榜数据的赛季里出现过的 team_id。
    # 口径=积分榜，保证 FAQ "覆盖 XX 支球队" 里每支队都真的查得到完整数据。零额外请求。
    all_team_ids = {
        row["team_id"]
        for tbl in historical_standings.values()
        for row in (tbl or [])
        if row.get("team_id")
    }
    coverage = {
        "seasons_total": len(seasons),
        "seasons_with_standings": len(seasons_std),   # 实测非空
        "seasons_with_results": len(results_seasons),
        "earliest_standings_season": seasons_std[-1] if seasons_std else None,
        "latest_standings_season": seasons_std[0] if seasons_std else None,
        "champion_seasons": len(champions),
        "teams_all_time": len(all_team_ids),          # 历年出现过的不重复球队数
    }

    # 12) 原始响应样例：每个接口留一条“未裁剪”的真实返回，供落地页 Sample Response 代码框直接展示
    finished_match = next((m for m in matches if not _is_unfinished(m.get("status"))), None)
    samples = {
        "league": le or None,
        "season": seasons[0] if seasons else None,
        "standings_row": standings_en[0] if standings_en else None,
        "topscorer_row": (top_scorers["goals"]["en"] or [None])[0],
        "match": finished_match or (matches[0] if matches else None),
        "champion": champions[0] if champions else None,
    }

    # 12b) 端点样例卡（Access Full Data 板块）：8 个端点全部取当前联赛真实数据，build 时固化。
    #      详见 build_endpoint_samples（含 season 回退 / odds/live 全站通用样例）。
    endpoint_samples = build_endpoint_samples(
        league_id, meta["name"]["en"], current_season, seasons_with_standings,
        standings_en, top_scorers["goals"]["en"], matches, upcoming, finished_match,
    )

    result = {
        "slug": slug,
        "league_id": league_id,
        "updated_at": _today(),   # 页面“最近更新日期”，每次生成/刷新盖当天(UTC)
        "meta": meta,
        "current_season": current_season,
        "current_season_in_progress": current_in_progress,
        "seasons": seasons,
        "standings": {"season": current_season, "en": standings_en, "zh": standings_zh},
        "top_scorers": top_scorers,
        "fixtures": {"season": current_season, "matches": matches, "upcoming": upcoming},
        "champions": champions,
        "honours": honours,
        "historical_standings": historical_standings,
        "team_name_map": {k: dict(v) for k, v in name_map.items()},
        "stats": stats,
        "coverage": coverage,
        "samples": samples,
        "endpoint_samples": endpoint_samples,
        "generated": {
            "seasons_total": len(seasons),
            "seasons_with_standings": len(seasons_with_standings),
            "champions": len(champions),
            "distinct_title_holders": len(honours),
            "teams_all_time": len(all_team_ids),
            "standings_rows_current": len(standings_en),
            "matches_current_season": len(matches),
            "upcoming_matches": len(upcoming),
        },
    }
    return result


def _today():
    """页面“最近更新日期”，UTC，形如 2026/07/29。"""
    return datetime.now(timezone.utc).strftime("%Y/%m/%d")


def _matches_season(season):
    """/matches 接口里跨年赛季用斜杠(2025/2026)，/seasons 用连字符(2025-2026)。"""
    return season.replace("-", "/") if season else season


def _envelope(rows, n):
    """把行数据重建成 API 响应信封(success/data/meta)，用于端点样例卡。
    只取前 n 条做展示，meta.total 反映真实全量条数。"""
    rows = rows or []
    return {"success": True, "data": rows[:n], "meta": {"total": len(rows)}}


# 站点根（BASE 去掉 /v1/soccer 版本前缀）；endpoint_samples 里 path 已含 /v1/soccer
HOST = BASE.split("/v1/")[0]  # https://data.anysport.io


def _mk_url(path, params):
    """据 path(含 /v1/soccer) + params 拼出可直接展示的完整请求 URL。"""
    clean = {k: v for k, v in params.items() if v is not None}
    qs = urllib.parse.urlencode(clean)
    return f"{HOST}{path}?{qs}" if qs else f"{HOST}{path}"


def _trim_odds_row(row, n_markets=4):
    """赔率单场可含 70~80 个盘口，样例只留前 n_markets 个，避免样例卡过长。"""
    if row and isinstance(row.get("markets"), list):
        row = {**row, "markets": row["markets"][:n_markets]}
    return row


# 全站滚球赔率通用样例：一次 build 只调一次，所有联赛复用（滚球是瞬时数据，
# 无法固化到具体联赛，故取全站任一在踢比赛做通用样例）。
_live_odds_cache = {"fetched": False, "sample": None}


def get_global_live_odds():
    if not _live_odds_cache["fetched"]:
        rows = get_data("/odds/live", lang="en", limit=1)
        _live_odds_cache["sample"] = _trim_odds_row(rows[0]) if rows else None
        _live_odds_cache["fetched"] = True
    return _live_odds_cache["sample"]


# 另两个时间敏感端点(livescore/odds_prematch)与 topscorers 的通用兜底样例：
# 本联赛取不到数据(休赛期/数据缺失)时复用，均一次 build 只调一次、全联赛共享。
_prematch_cache = {"fetched": False, "sample": None}
_livescore_cache = {"fetched": False, "sample": None}
# topscorers 通用兜底：首见缓存——运行中第一个有射手榜数据的联赛，记住其 rows/联赛/赛季
# 供后续无数据联赛复用；若开局就遇到无数据联赛，从德甲最近赛季逐季探一条兜底。
_topscorers_cache = {"rows": None, "league_id": None, "season": None}
_FALLBACK_TS_LID = "asl_30c1e028124f46529e230d780bbe0e9e"  # 德甲
_FALLBACK_TS_SEASONS = ("2025-2026", "2024-2025", "2023-2024", "2022-2023")


def get_global_prematch_odds():
    if not _prematch_cache["fetched"]:
        rows = get_data("/odds/prematch", lang="en", limit=1)
        _prematch_cache["sample"] = _trim_odds_row(rows[0]) if rows else None
        _prematch_cache["fetched"] = True
    return _prematch_cache["sample"]


def get_global_livescore():
    """复用全站滚球样例的 match_id 拉一条真实“进行中”比分做通用样例。"""
    if not _livescore_cache["fetched"]:
        live = get_global_live_odds()
        mid = live.get("match_id") if live else None
        rows = get_data("/livescore", match_id=mid, lang="en") if mid else []
        _livescore_cache["sample"] = rows[0] if rows else None
        _livescore_cache["fetched"] = True
    return _livescore_cache["sample"]


def note_topscorers(rows, league_id, season):
    """记录首个有数据的联赛射手榜，作为后续无数据联赛的通用兜底源。"""
    if rows and _topscorers_cache["rows"] is None:
        _topscorers_cache.update(rows=list(rows[:3]), league_id=league_id, season=season)


def get_global_topscorers():
    """返回通用兜底射手榜 {rows, league_id, season}；未首见则从德甲最近赛季探一条。"""
    if _topscorers_cache["rows"] is None:
        for s in _FALLBACK_TS_SEASONS:
            rows = get_data("/topscorers", league_id=_FALLBACK_TS_LID, season=s, kind="goals", lang="en")
            if rows:
                _topscorers_cache.update(rows=rows[:3], league_id=_FALLBACK_TS_LID, season=s)
                break
        if _topscorers_cache["rows"] is None:
            _topscorers_cache.update(rows=[], league_id=_FALLBACK_TS_LID, season=_FALLBACK_TS_SEASONS[0])
    return _topscorers_cache


def build_endpoint_samples(league_id, name_en, current_season, seasons_desc,
                           standings_en, topscorers_en, matches, upcoming, finished_match):
    """构建 Access Full Data 板块的 8 个端点样例：全部取当前联赛真实数据；
    season 类端点当前赛季空则倒序回退到上一个有数据的赛季；
    odds/live 用全站通用样例(scope=generic)。"""
    EN = "en"

    def first_nonempty(fetch, max_try=6):
        for s in (seasons_desc or [])[:max_try]:
            data = fetch(s)
            if data:
                return s, data
        return current_season, []

    def sample(path, params, rows, n=3, scope="league"):
        # scope: "league"=本联赛真实数据；"generic"=本联赛取不到、退回的全站通用样例
        return {"method": "GET", "path": path, "params": params, "scope": scope,
                "url": _mk_url(path, params), "response": _envelope(rows, n)}

    out = {}

    # === 页面内容型端点：只用本联赛数据，不做 generic 兜底 ===
    # 1) 积分榜：优先内存里的当前赛季，空则回退
    if standings_en:
        s_season, s_rows = current_season, standings_en
    else:
        s_season, s_rows = first_nonempty(
            lambda s: get_data("/standings", league_id=league_id, season=s, lang=EN))
    out["standings"] = sample("/v1/soccer/standings",
        {"league_id": league_id, "season": s_season, "lang": EN}, s_rows, 3)

    # 2) 赛程赛果（en，+1 调用）：/matches 赛季用斜杠；优先取已完赛的更好看
    m_season, m_rows = first_nonempty(
        lambda s: get_data("/matches", league_id=league_id, season=_matches_season(s), lang=EN, limit=10))
    m_finished = [m for m in m_rows if not _is_unfinished(m.get("status"))] or m_rows
    out["matches"] = sample("/v1/soccer/matches",
        {"league_id": league_id, "season": _matches_season(m_season), "lang": EN}, m_finished, 3)

    # 3) 球队列表（en，+1 调用）：季度回退。注意 /teams 跨年赛季用斜杠(2025/2026)，
    #    与 /matches 同、与 /standings 连字符不同；连字符会静默返回 0。
    tm_season, tm_rows = first_nonempty(
        lambda s: get_data("/teams", league_id=league_id, season=_matches_season(s), lang=EN))
    out["teams"] = sample("/v1/soccer/teams",
        {"league_id": league_id, "season": _matches_season(tm_season), "lang": EN}, tm_rows, 3)

    # 4) 球员资料（en，+1 调用）：取积分榜榜首球队的阵容（带 league_id 附返 season_stats）
    p_team = s_rows[0].get("team_id") if s_rows else None
    p_rows = get_data("/players", team_id=p_team, league_id=league_id, lang=EN) if p_team else []
    out["players"] = sample("/v1/soccer/players",
        {"team_id": p_team, "league_id": league_id, "lang": EN}, p_rows, 3)

    # === 纯样例型端点：本联赛取不到就退回全站通用样例(scope=generic)，保证 tab 不空 ===
    # 5) 射手榜：优先内存当前赛季→赛季回退→通用兜底。topscorers 是叶子(无下游派生)，
    #    仅作样例 tab；空只影响此卡（如法罗甲无射手榜数据）。
    if topscorers_en:
        t_season, t_rows = current_season, topscorers_en
    else:
        t_season, t_rows = first_nonempty(
            lambda s: get_data("/topscorers", league_id=league_id, season=s, kind="goals", lang=EN))
    if t_rows:
        note_topscorers(t_rows, league_id, t_season)  # 供后续无数据联赛复用
        out["topscorers"] = sample("/v1/soccer/topscorers",
            {"league_id": league_id, "season": t_season, "kind": "goals", "lang": EN}, t_rows, 3)
    else:
        g = get_global_topscorers()
        out["topscorers"] = sample("/v1/soccer/topscorers",
            {"league_id": g["league_id"], "season": g["season"], "kind": "goals", "lang": EN},
            g["rows"], 3, scope="generic")

    # 6) 实时比分（+1 调用）：/livescore 是轮询端点只留近期比赛，选“最近一场已完赛”
    #    最大化命中；休赛期常空→退回全站任一在踢比赛。
    finished = sorted(
        (m for m in (matches or []) if not _is_unfinished(m.get("status"))),
        key=lambda m: m.get("date") or "", reverse=True)
    ls_mid = (finished[0] if finished else (finished_match or {})).get("match_id")
    if not ls_mid and matches:
        ls_mid = matches[0].get("match_id")
    ls_rows = get_data("/livescore", match_id=ls_mid, lang=EN) if ls_mid else []
    if ls_rows:
        out["livescore"] = sample("/v1/soccer/livescore",
            {"match_id": ls_mid, "lang": EN}, ls_rows, 1)
    else:
        g = get_global_livescore()
        out["livescore"] = sample("/v1/soccer/livescore",
            {"match_id": (g or {}).get("match_id"), "lang": EN}, [g] if g else [], 1, scope="generic")

    # 7) 赛前赔率（+1 调用）：优先未来赛程的 match_id；无未来赛程(休赛期)→退回全站通用样例。
    pm_mid = upcoming[0].get("match_id") if upcoming else None
    pm_rows = [_trim_odds_row(r) for r in
               (get_data("/odds/prematch", match_id=pm_mid, lang=EN) if pm_mid else [])]
    if pm_rows:
        out["odds_prematch"] = sample("/v1/soccer/odds/prematch",
            {"match_id": pm_mid, "lang": EN}, pm_rows, 1)
    else:
        g = get_global_prematch_odds()
        out["odds_prematch"] = sample("/v1/soccer/odds/prematch",
            {"lang": EN}, [g] if g else [], 1, scope="generic")

    # 8) 滚球赔率：全站通用样例（滚球瞬时数据无法固化到联赛，恒 generic）
    live = get_global_live_odds()
    out["odds_live"] = sample("/v1/soccer/odds/live", {"lang": EN},
        [live] if live else [], 1, scope="generic")

    return out


_FINISHED = {"FT", "AET", "Pen.", "Awarded", "WO"}
_ABANDONED = {"Postp.", "Delayed", "Cancl.", "Cancelled", "Abandoned",
              "Aban.", "Susp.", "Int."}


def _is_unfinished(status):
    """判断一场比赛是否“尚未完赛”（未开踢或进行中）。
    完赛=FT/AET/Pen./Awarded/WO；作废类不计入‘进行中’。其余(HH:MM/纯数字分钟/Not Started/空)视为未完赛。"""
    if status is None:
        return True
    s = str(status).strip()
    if s in _FINISHED or s in _ABANDONED:
        return False
    return True  # HH:MM / 纯数字分钟 / Not Started / TBA / '' / HT / ET / P ...


def parse_score(s):
    """'2-1' -> (2,1)；无效返回 None。"""
    if not s or "-" not in s:
        return None
    try:
        a, b = s.split("-", 1)
        return int(a), int(b)
    except ValueError:
        return None


def _full_score(m):
    """当前实际数据用 reg_score 表示全场比分（文档写的 ft_score 已过时）；两者兜底。"""
    return m.get("reg_score") or m.get("ft_score")


def compute_stats(matches, standings):
    played = 0
    total_goals = 0
    home_w = draw = away_w = 0
    biggest_win = None       # 最大分差
    highest_scoring = None   # 单场总进球最多
    for m in matches:
        sc = parse_score(_full_score(m))
        if not sc:
            continue
        h, a = sc
        played += 1
        total_goals += h + a
        if h > a:
            home_w += 1
        elif h < a:
            away_w += 1
        else:
            draw += 1
        margin = abs(h - a)
        if biggest_win is None or margin > biggest_win["margin"]:
            biggest_win = {"margin": margin, "match": _match_brief(m)}
        if highest_scoring is None or (h + a) > highest_scoring["goals"]:
            highest_scoring = {"goals": h + a, "match": _match_brief(m)}
    leader = standings[0] if standings else None
    return {
        "matches_completed": played,
        "total_goals": total_goals,
        "avg_goals_per_match": round(total_goals / played, 2) if played else None,
        "home_wins": home_w,
        "draws": draw,
        "away_wins": away_w,
        "home_win_pct": round(100 * home_w / played, 1) if played else None,
        "biggest_win": biggest_win,
        "highest_scoring_match": highest_scoring,
        "leader": {"team": leader.get("team"), "points": leader.get("points"), "played": leader.get("played")} if leader else None,
    }


def _match_brief(m):
    return {
        "match_id": m.get("match_id"),
        "date": m.get("date"),
        "home_id": m.get("home", {}).get("team_id"),
        "home": m.get("home", {}).get("name"),
        "away_id": m.get("away", {}).get("team_id"),
        "away": m.get("away", {}).get("name"),
        "score": _full_score(m),
    }


# ---------------------------------------------------------------- 主流程
def run(leagues):
    index, failures = [], []
    for slug, lid in leagues.items():
        try:
            data = build_league(slug, lid)
            out_path = os.path.join(OUT_DIR, f"landing_{slug}.json")
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            g = data["generated"]
            print(f"    ✓ 已写入 {os.path.basename(out_path)}  "
                  f"(赛季{g['seasons_total']} / 冠军{g['champions']} / "
                  f"当季{g['matches_current_season']}场 / 累计API调用{_stats['calls']})")
            index.append({"slug": slug, "league_id": lid, "file": f"landing_{slug}.json", **g})
        except Exception as e:  # noqa  单联赛失败不阻断全量；记录后继续（可 skip_existing 续跑）
            import traceback
            print(f"    !! 构建失败 [{slug}] {lid}: {type(e).__name__}: {e}")
            traceback.print_exc()
            failures.append({"slug": slug, "league_id": lid, "error": f"{type(e).__name__}: {e}"})

    idx_path = os.path.join(OUT_DIR, "landing_index.json")
    with open(idx_path, "w", encoding="utf-8") as f:
        json.dump({"leagues": index, "total_api_calls": _stats["calls"]}, f, ensure_ascii=False, indent=2)
    if failures:
        with open(os.path.join(OUT_DIR, "landing_failures.json"), "w", encoding="utf-8") as f:
            json.dump(failures, f, ensure_ascii=False, indent=2)
    print(f"\n全部完成：成功 {len(index)} 个，失败 {len(failures)} 个，"
          f"共 {_stats['calls']} 次 API 调用。索引 landing_index.json"
          + ("，失败清单 landing_failures.json" if failures else ""))


# ---------------------------------------------------------------- 全量联赛（后续使用）
def slugify(name, league_id):
    """把联赛英文名转成 URL slug；失败则回退用 league_id 尾部。"""
    if name:
        s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        if s:
            return s
    return league_id.replace("asl_", "")[:12]


def get_all_leagues():
    """拉取平台全部联赛（英文名），返回 {slug: league_id} 有序字典。"""
    rows = get_all_pages("/leagues", lang="en")
    out = {}
    for r in rows:
        lid = r.get("league_id")
        if not lid:
            continue
        slug = slugify(r.get("name"), lid)
        # slug 去重
        base, n = slug, 2
        while slug in out:
            slug = f"{base}-{n}"; n += 1
        out[slug] = lid
    print(f"平台联赛总数：{len(out)}")
    return out


def run_all(skip_existing=True, limit=None):
    """拉取全部联赛。skip_existing=True 时跳过已生成 landing_*.json 的联赛，可断点续跑。
       ⚠️ 联赛可能上百/上千个，每个约 40~80 次 API 调用，注意每小时额度；建议分批 limit=N 跑。"""
    leagues = get_all_leagues()
    if limit:
        leagues = dict(list(leagues.items())[:limit])
    todo = {}
    for slug, lid in leagues.items():
        if skip_existing and os.path.exists(os.path.join(OUT_DIR, f"landing_{slug}.json")):
            continue
        todo[slug] = lid
    print(f"待构建 {len(todo)} 个（已跳过 {len(leagues) - len(todo)} 个已存在）")
    run(todo)


# ---------------------------------------------------------------- 增量刷新
#
# 全量重建每联赛 40~80 次 API 调用（含所有历史赛季，而历史永不变=浪费）。
# 增量只重拉“当前赛季”的动态块（积分榜/射手/赛程/未来赛程/派生统计），历史/冠军/
# 荣誉/覆盖/端点样例的非当季部分沿用现有 JSON —— 每联赛约 11~13 次调用。
#
# ⚠️ 增量不处理“赛季更替”（新赛季开踢 / 旧赛季夺冠入历史）：current_season 沿用现有值。
#    赛季边界靠周期性 `all` 全量重建（或对该联赛单独全量）来纠正。


def _extract_league_id(path):
    """只读文件头，用正则抠出 league_id，避免解码整份大 JSON。"""
    try:
        with open(path, "rb") as f:
            head = f.read(512).decode("utf-8", "ignore")
    except OSError:
        return None
    m = re.search(r'"league_id"\s*:\s*"(asl_[0-9a-fA-F]+)"', head)
    return m.group(1) if m else None


def existing_files_by_id(directory):
    """扫描目录下现有 landing_*.json，返回 {league_id: path}。"""
    out = {}
    for p in glob.glob(os.path.join(directory, "landing_*.json")):
        b = os.path.basename(p)
        if b in ("landing_index.json", "landing_failures.json"):
            continue
        lid = _extract_league_id(p)
        if lid and lid not in out:
            out[lid] = p
    return out


def active_league_ids(days):
    """返回近 days 天内有比赛的联赛 id 集合（一次 window 查询覆盖全站，分页）。
    这是压 GitHub Actions 分钟数的关键杠杆：大多数联赛并非每天踢球，只刷有比赛的即可。"""
    rows = get_all_pages("/matches/window", from_days=-abs(int(days)), to_days=0, lang="en")
    ids = {r.get("league_id") for r in rows if r.get("league_id")}
    print(f"    近 {days} 天有比赛的联赛：{len(ids)} 个（扫描 {len(rows)} 场）")
    return ids


def _refresh_dynamic_samples(existing_samples, league_id, season, standings_en, topscorers_en):
    """增量刷新端点样例中“可由已拉数据直接重建”的两个（standings/topscorers），
    其余（matches/teams/players/livescore/odds*）沿用现有，避免额外 API 调用。"""
    samples = dict(existing_samples or {})
    EN = "en"
    if standings_en:
        params = {"league_id": league_id, "season": season, "lang": EN}
        samples["standings"] = {"method": "GET", "path": "/v1/soccer/standings", "params": params,
                                "scope": "league", "url": _mk_url("/v1/soccer/standings", params),
                                "response": _envelope(standings_en, 3)}
    if topscorers_en:
        params = {"league_id": league_id, "season": season, "kind": "goals", "lang": EN}
        samples["topscorers"] = {"method": "GET", "path": "/v1/soccer/topscorers", "params": params,
                                 "scope": "league", "url": _mk_url("/v1/soccer/topscorers", params),
                                 "response": _envelope(topscorers_en, 3)}
    return samples


def refresh_league(existing, league_id):
    """就地增量刷新一份已有的落地页数据；current_season 缺失时返回 None（应改走全量）。"""
    season = existing.get("current_season")
    if not season:
        return None

    name_map = defaultdict(dict)
    for tid, v in (existing.get("team_name_map") or {}).items():
        name_map[tid] = dict(v)

    # 当前赛季积分榜（en/zh）。若返回空（数据抖动/赛季空档）则沿用现有，避免把页面刷没。
    st_en = get_data("/standings", league_id=league_id, season=season, lang="en") \
        or (existing.get("standings", {}).get("en") or [])
    st_zh = get_data("/standings", league_id=league_id, season=season, lang="zh-CN") \
        or (existing.get("standings", {}).get("zh") or [])
    for row in st_en:
        tid = row.get("team_id")
        if tid:
            name_map[tid]["en"] = row.get("team")
    for row in st_zh:
        tid = row.get("team_id")
        if tid:
            name_map[tid]["zh"] = row.get("team")

    # 三榜（双语）
    top_scorers = {}
    for kind in ("goals", "assists", "cards"):
        top_scorers[kind] = {
            "en": get_data("/topscorers", league_id=league_id, season=season, kind=kind, lang="en"),
            "zh": get_data("/topscorers", league_id=league_id, season=season, kind=kind, lang="zh-CN"),
        }

    # 当季赛程赛果 + 未来赛程
    matches = get_all_pages("/matches", league_id=league_id, season=_matches_season(season), lang="zh-CN")
    for m in matches:
        for side in ("home", "away"):
            tid = m.get(side, {}).get("team_id")
            if tid and "en" not in name_map[tid]:
                name_map[tid]["en"] = m[side].get("name")
    upcoming = get_data("/matches/window", from_days=0, to_days=45, league_id=league_id, lang="zh-CN")

    stats = compute_stats(matches, st_en)
    in_progress = any(_is_unfinished(m.get("status")) for m in matches)

    # 写回：只覆盖动态块；历史/冠军/荣誉/覆盖/seasons 等静态块保留。
    existing["standings"] = {"season": season, "en": st_en, "zh": st_zh}
    existing["top_scorers"] = top_scorers
    existing["fixtures"] = {"season": season, "matches": matches, "upcoming": upcoming}
    existing["stats"] = stats
    existing["current_season_in_progress"] = in_progress
    existing["endpoint_samples"] = _refresh_dynamic_samples(
        existing.get("endpoint_samples"), league_id, season, st_en, top_scorers["goals"]["en"])
    existing["team_name_map"] = {k: dict(v) for k, v in name_map.items()}
    existing["updated_at"] = _today()   # 每次增量刷新盖当天日期
    g = existing.get("generated", {}) or {}
    g.update({
        "standings_rows_current": len(st_en),
        "matches_current_season": len(matches),
        "upcoming_matches": len(upcoming),
    })
    existing["generated"] = g
    return existing


def run_refresh(ids=None, active_days=None):
    """增量刷新。ids 指定则刷这些；active_days 指定则先查有比赛的联赛；否则刷全部现有。
    注意：**不重建 manifest**——manifest 的 {file, display} 只依赖联赛名/国家(静态)，增量刷新不改它；
    且托管子集时 manifest 应是"全量 1009 条"的固定副本，用它重建会缩水丢失消歧。
    manifest 仅在联赛集合变化时用 `manifest` 命令、基于**全量**目录重建。"""
    by_id = existing_files_by_id(OUT_DIR)
    if active_days is not None:
        ids = active_league_ids(active_days)
    if ids:
        targets = [i for i in ids if i in by_id]
    else:
        targets = list(by_id.keys())
    print(f"增量刷新 {len(targets)} 个联赛（现有 {len(by_id)} 个）")

    ok = fail = 0
    for lid in targets:
        path = by_id[lid]
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            updated = refresh_league(existing, lid)
            if updated is None:
                print(f"    - 跳过（无 current_season，需全量）：{os.path.basename(path)}")
                continue
            with open(path, "w", encoding="utf-8") as f:
                json.dump(updated, f, ensure_ascii=False, indent=2)
            ok += 1
        except Exception as e:  # noqa
            fail += 1
            print(f"    ! 刷新失败 {os.path.basename(path)}: {type(e).__name__}: {e}")

    print(f"\n增量完成：成功 {ok}，失败 {fail}，API 调用 {_stats['calls']} 次。（manifest 未改动）")


# ---------------------------------------------------------------- manifest（Python 版）
# 与 tools/build-manifest.php 等价：扫描全部 landing_*.json → league_id => {file, display}，
# 并对**重名联赛**加国家前缀消歧（Premier League ×42 等）。放在这里让 CI 纯 Python 即可。


# 通用/非国家的 country 值：不加前缀（洲际/国际赛事名字通常已够独特）。
_GENERIC_COUNTRY = {"", "intl", "international", "world", "worldcup", "world cup",
                    "europe", "africa", "asia", "oceania", "north america", "south america"}


def _prefix_en(name, country):
    c = (country or "").strip()
    if not c or c.lower() in _GENERIC_COUNTRY:
        return name
    if c.lower() in name.lower():   # 名字已含国家则不重复加
        return name
    return f"{c} {name}"


def _prefix_zh(name, country):
    if not country:
        return name
    if country in name:
        return name
    return f"{country}{name}"


def build_manifest(directory):
    files = [p for p in glob.glob(os.path.join(directory, "landing_*.json"))
             if os.path.basename(p) not in ("landing_index.json", "landing_failures.json")]
    entries, cnt_en, cnt_zh = {}, {}, {}
    for p in files:
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:  # noqa
            continue
        lid = d.get("league_id")
        if not lid or lid in entries:
            continue
        meta = d.get("meta", {}) or {}
        ne = html.unescape((meta.get("name", {}).get("en") or "").strip())
        nz = html.unescape((meta.get("name", {}).get("zh") or "").strip())
        ce = html.unescape((meta.get("country", {}).get("en") or "").strip())
        cz = html.unescape((meta.get("country", {}).get("zh") or "").strip())
        # 列表 shortcode 用的摘要字段（避免列表渲染时逐个加载完整 JSON）
        cov = d.get("coverage") or {}
        st_n = len((d.get("standings", {}) or {}).get("en") or [])
        ch_n = len(d.get("champions") or [])
        mc_n = (d.get("stats") or {}).get("matches_completed") or 0
        entries[lid] = {
            "file": os.path.basename(p), "ne": ne, "nz": nz, "ce": ce, "cz": cz,
            "is_cup": bool(meta.get("is_cup")),
            "seasons": cov.get("seasons_with_standings") or 0,
            "since": cov.get("earliest_standings_season") or "",
            "teams": cov.get("teams_all_time") or 0,
            "has_data": (st_n > 0 or ch_n > 0 or mc_n > 0),
        }
        if ne:
            cnt_en[ne.lower()] = cnt_en.get(ne.lower(), 0) + 1
        if nz:
            cnt_zh[nz] = cnt_zh.get(nz, 0) + 1

    leagues, disamb = {}, 0
    for lid, e in entries.items():
        dup_zh = e["nz"] and cnt_zh.get(e["nz"], 0) > 1
        de = _prefix_en(e["ne"], e["ce"])          # 英文一律加国家前缀（消歧+统一）
        dz = _prefix_zh(e["nz"], e["cz"]) if dup_zh else e["nz"]  # 中文名多已含国家，仅重名时加
        if de != e["ne"] or dz != e["nz"]:
            disamb += 1
        leagues[lid] = {
            "file": e["file"],
            "display": {"en": de, "zh": dz},
            "country": {"en": e["ce"], "zh": e["cz"]},
            "is_cup": e["is_cup"],
            "seasons": e["seasons"],
            "since": e["since"],
            "teams": e["teams"],
            "has_data": e["has_data"],
        }

    leagues = dict(sorted(leagues.items()))
    manifest = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(leagues),
        "disambiguated": disamb,
        "leagues": leagues,
    }
    out = os.path.join(directory, "aslp-manifest.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"    manifest：{len(leagues)} 个联赛，{disamb} 个消歧 → {out}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "all":
        # 全量重建全部联赛（含历史）。建议先 all 5 试跑。
        run_all(limit=int(sys.argv[2]) if len(sys.argv) > 2 else None)
        build_manifest(OUT_DIR)
    elif cmd == "refresh":
        # 增量刷新：refresh [league_id ...]；不带 id 则刷全部现有文件。
        run_refresh(ids=(sys.argv[2:] or None))
    elif cmd == "refresh-active":
        # 增量刷新“近 N 天有比赛”的联赛（默认 2 天）。GitHub Actions 定时任务用这个。
        run_refresh(active_days=int(sys.argv[2]) if len(sys.argv) > 2 else 2)
    elif cmd == "manifest":
        build_manifest(OUT_DIR)
    else:
        run(LEAGUES)
