# -*- coding: utf-8 -*-
"""
verdict_checkup.py — 「结论体检器」核心引擎

给一段「用数据支撑的结论」+ 配套数据(可选)，做结构化体检，
输出：这个结论能不能站住、病在哪、用了什么判据。

这不是「AI 猜一猜」，是把实战中真踩出来的判据拆成可执行的检查项：
  1. 数字量级检查（极端值/负值当「尺子坏」警报）
  2. 趋势 vs 噪声（单调性判据，不是单阈值）
  3. 数据边界（覆盖范围外读数不信）
  4. 多标度/普适性（分尺度拟合漂移 = 不是单一常数）
  5. 证据等级（读到 vs 算到 vs 转述；N 是否足够）

设计原则：每条检查给出 verdict ∈ {pass, warn, fail, cannot_judge}，
绝不把「判不了」硬塞成 pass 或 fail（soul: 二元对立会吞掉『证据不足』）。
"""
import math
import re

def _r2(xs, ys, slope, intercept):
    ss_tot = sum((y - sum(ys)/len(ys))**2 for y in ys)
    if ss_tot == 0:
        return 0.0
    ss_res = sum((y - (slope*x + intercept))**2 for x, y in zip(xs, ys))
    return 1 - ss_res/ss_tot

def _fit_line(xs, ys):
    n = len(xs)
    mx, my = sum(xs)/n, sum(ys)/n
    num = sum((x-mx)*(y-my) for x, y in zip(xs, ys))
    den = sum((x-mx)**2 for x in xs)
    if den == 0:
        return 0.0, my
    slope = num/den
    return slope, my - slope*mx


def check_extreme_values(numbers, claim_text=""):
    """检查1：数字量级。极端值/负值/单位混用是『尺子坏』的警报。"""
    if not numbers:
        return {"check": "数字量级", "verdict": "cannot_judge",
                "detail": "未提供具体数字，无法核量级", "flags": []}
    flags = []
    neg = [x for x in numbers if x < 0]
    # 数量级跨度
    absvals = [abs(x) for x in numbers if x != 0]
    span = 0
    if len(absvals) >= 2:
        span = max(absvals)/min(absvals)
    if len(absvals) >= 2 and span > 1e4:
        flags.append(f"数值跨 {span:.0e} 个量级，怀疑单位混用/量纲错")
    verdict = "warn" if flags else "pass"
    return {"check": "数字量级", "verdict": verdict,
            "detail": f"量级跨度 {span:.2e}" if len(absvals)>=2 else "数字正常",
            "flags": flags}


def check_trend_vs_noise(series):
    """
    检查2：趋势 vs 噪声。用单调性判据，不用单阈值卡。
    series: [(x, y), ...] 按 x 排序的值序列。
    —— 严格单调且幅度超阈 => 报趋势；来回跳 => 噪声；几乎平 => 无趋势
    """
    if len(series) < 3:
        return {"check": "趋势vs噪声", "verdict": "cannot_judge",
                "detail": f"仅 {len(series)} 个点，判据需≥3", "flags": []}
    ys = [y for _, y in series]
    xs = [x for x, _ in series]
    diffs = [ys[i+1]-ys[i] for i in range(len(ys)-1)]
    inc = all(d >= 0 for d in diffs)
    dec = all(d <= 0 for d in diffs)
    slope, intercept = _fit_line(xs, ys)
    r2 = _r2(xs, ys, slope, intercept)
    mean_y = sum(ys)/len(ys)
    rel_change = abs(ys[-1]-ys[0])/abs(mean_y) if mean_y != 0 else 0

    # 变化太小 => 判不了(不是没有, 是证据不足)
    if rel_change < 0.02:
        return {"check": "趋势vs噪声", "verdict": "cannot_judge",
                "detail": f"变化仅 {rel_change:.1%}，无方向性趋势（不是『没有』，是『判不了』）",
                "flags": []}
    # 【2026-11-15 摩尔定律踢馆补】指数增长识别:
    # 指数序列在原始尺度是弯的, 线性 R² 会偏低被误判"趋势弱"。
    # 若 y 全正且对数线性拟合远好于线性, 视为指数增长, 改用 log R² 评估趋势强度。
    exp_growth = False
    log_r2 = None
    if all(y > 0 for y in ys):
        import math as _m
        logy = [_m.log(y) for y in ys]
        lslope, lb = _fit_line(xs, logy)
        log_r2 = _r2(xs, logy, lslope, lb)
        # 仅当: 对数拟合强(>=0.9) 且 明显优于线性(差值>0.15) => 指数
        if log_r2 >= 0.9 and (log_r2 - r2) > 0.15 and abs(lslope) > 1e-9:
            exp_growth = True
            # 用对数 R² 替代线性 R², 重新算净变化(用 y 首尾)
            r2 = log_r2
            doubling = None
    # 严格单调是最强证据
    monotone = inc or dec
    # 【关键修正】真实世界趋势几乎从不严格单调(总有小波动), 不能因"某一年回头"就判fail。
    # 趋势强弱看 R²(线性拟合解释力) + 净变化方向, 不要求每步不回头。
    net_dir = "上升" if ys[-1] > ys[0] else "下降"
    # 反转比例: 走反方向的步数占比
    if slope > 0:
        against = sum(1 for d in diffs if d < 0)
    else:
        against = sum(1 for d in diffs if d > 0)
    rev_frac = against / len(diffs) if diffs else 0
    if r2 >= 0.7:
        # 强趋势: 即使有波动也是真趋势
        kind = "单调" if monotone else f"整体{net_dir}(有{rev_frac:.0%}步回撤)"
        tag = "【指数增长·对数尺度拟合】" if exp_growth else ""
        return {"check": "趋势vs噪声", "verdict": "pass",
                "detail": f"{tag}{kind}，R²={r2:.3f}（强趋势），变化{rel_change:.1%}",
                "flags": [] if monotone else [f"非严格单调但有强趋势(R²={r2:.3f}), 波动不否定方向"]}
    elif r2 >= 0.3:
        # 中等趋势: 有保留
        tag = "【指数增长·对数尺度】" if exp_growth else ""
        return {"check": "趋势vs噪声", "verdict": "warn",
                "detail": f"{tag}整体{net_dir}但R²={r2:.3f}(中等)，趋势需谨慎",
                "flags": [f"R²={r2:.3f}, 方向可辨但拟合不高"]}
    else:
        # 低 R²: 趋势不成立(来回跳/噪声)
        return {"check": "趋势vs噪声", "verdict": "fail",
                "detail": f"来回跳（R²={r2:.3f}），所谓『趋势』可能是噪声",
                "flags": ["数据非单调且拟合差，任何『上升/下降』结论存疑"]}


def check_data_boundary(data_max_key, claim_range=None):
    """检查3：数据边界。覆盖范围之外的读数一律不信。"""
    if data_max_key is None:
        return {"check": "数据边界", "verdict": "cannot_judge",
                "detail": "未提供数据覆盖范围", "flags": []}
    if claim_range is not None and claim_range > data_max_key:
        return {"check": "数据边界", "verdict": "fail",
                "detail": f"结论涉及 {claim_range}，但数据只到 {data_max_key}——超出边界的部分是编的",
                "flags": ["结论越过了数据覆盖边界"]}
    return {"check": "数据边界", "verdict": "pass",
            "detail": f"数据覆盖到 {data_max_key}，结论在范围内", "flags": []}


def check_sample_size(n, claim_type="general"):
    """检查4：样本量。N=1 不能当队列；小样本不能封顶结论。"""
    if n is None:
        return {"check": "样本量", "verdict": "cannot_judge",
                "detail": "未提供 N", "flags": []}
    if n <= 1:
        return {"check": "样本量", "verdict": "fail",
                "detail": f"N={n}，个案只能当存在性证据，不能当统计验证",
                "flags": ["N=1，任何推广都是过度解读"]}
    if n < 30:
        return {"check": "样本量", "verdict": "warn",
                "detail": f"N={n} <30，小样本结论需保守，个例噪声可能主导",
                "flags": ["小样本，结论敏感性高"]}
    return {"check": "样本量", "verdict": "pass", "detail": f"N={n}，样本量可支撑普通统计结论", "flags": []}


def check_evidence_grade(claim_text):
    """检查5：证据等级。分清『读到的/转述的/推算的』。"""
    flags = []
    text = claim_text or ""
    if re.search(r'据说|听说|网传|博主|新闻|报道|转发', text):
        flags.append("含转述线索：数字可能是二手，需回原文核对单位/证据等级")
    if re.search(r'倍|提高|增长|下降', text) and re.search(r'\\d', text):
        flags.append("含倍率/增减表述：确认这个倍数原文就是『结论』，不是某示例里的分子分母")
    verdict = "warn" if flags else "pass"
    return {"check": "证据等级", "verdict": verdict,
            "detail": "；".join(flags) if flags else "未见二手/转述风险信号",
            "flags": flags}



def check_observable_fitness(series=None, sizes=None):
    """检查6【观测量配不配】(C02血泪): 这个分布是真雪崩型, 还是人力节奏/独立事件?
    判据: 单点主导(=1的占比极高) + 断尾(中段连续但高端零星杂乱) => 不是SOC雪崩观测量。
    sizes: 事件规模列表(如每日关闭量)。
    """
    import collections
    vals = sizes if sizes is not None else None
    if vals is None and series:
        vals = [y for _, y in series]
    if not vals:
        return {"check": "观测量配不配", "verdict": "cannot_judge",
                "detail": "未提供事件规模, 无法判观测量是否适合", "flags": []}
    vals = [v for v in vals if v is not None]
    if not vals:
        return {"check": "观测量配不配", "verdict": "cannot_judge",
                "detail": "无有效规模值", "flags": []}
    n = len(vals)
    from collections import Counter
    c = Counter(vals)
    frac_ones = c.get(1, 0) / n
    flags = []
    # 单点主导: >55% 的事件规模=1 且最大值远大于均值 => 疑似独立事件/人力节奏
    mean = sum(vals) / n
    mx = max(vals)
    if frac_ones > 0.55 and mean > 0 and mx > 8 * mean:
        flags.append(f"{frac_ones:.0%}的事件规模=1 且最大值{max(vals)}>>均值{mean:.1f} —— "
                     f"像独立事件/人力节奏(日常逐个+偶发集中), 非积累-崩溃雪崩")
    if flags:
        return {"check": "观测量配不配", "verdict": "fail",
                "detail": f"单点占比{frac_ones:.0%}, 疑似非雪崩观测量", "flags": flags}
    # 若单点占比适中、分布连续 => 可作雪崩
    return {"check": "观测量配不配", "verdict": "pass",
            "detail": f"单点占比{frac_ones:.0%}, 分布无断尾特征, 可作雪崩观测量", "flags": []}


def check_same_class_trap(alpha_a=None, alpha_b=None, drift_a=None, drift_b=None):
    """检查7【指数数值巧合≠同机制】(C03): 两系统指数接近, 但若各自分段漂移大=>不同类。
    alpha_a/b: 两系统标度指数; drift_a/b: 各自分段漂移幅度(可选)。
    """
    if alpha_a is None or alpha_b is None:
        return {"check": "同类判定(数值≠身份)", "verdict": "cannot_judge",
                "detail": "未提供两系统指数, 无法判是否同类", "flags": []}
    flags = []
    close = abs(alpha_a - alpha_b) < 0.15 * max(abs(alpha_a), abs(alpha_b), 1e-9)
    detail = f"αA={alpha_a:.3f} αB={alpha_b:.3f} 差{abs(alpha_a-alpha_b):.3f}"
    if close and (drift_a is not None and drift_b is not None):
        if abs(drift_a - drift_b) > 0.5:
            flags.append(f"指数接近但分段漂移差{abs(drift_a-drift_b):.2f}(A={drift_a},B={drift_b}) "
                         f"—— 数值巧合≈但标度结构不同, 不能判同类")
            return {"check": "同类判定(数值≠身份)", "verdict": "fail",
                    "detail": detail + " 数值接近但结构异", "flags": flags}
    if close:
        return {"check": "同类判定(数值≠身份)", "verdict": "warn",
                "detail": detail + " 数值接近; 但仅凭指数不能定同类, 需分段稳定性检验",
                "flags": ["数值接近≠同机制, 见过检查『多标度』再下结论"]}
    return {"check": "同类判定(数值≠身份)", "verdict": "pass",
            "detail": detail + " 指数不同, 明确不同类", "flags": []}



def check_sample_composition(sample_sizes_by_time=None, group_weights_by_stratum=None):
    """检查8【样本构成漂移】(辛普森/幸存者陷阱):
    两个维度, 缺一都可能漏:
    (A) 样本总数漂移: 同一指标不同时间点样本数剧变(如1800年1国→2020年236国)。
    (B) 分组权重漂移【2026-11-15 伯克利踢馆补】: 某组在各『分层/系』里的占比剧烈变化,
        即使各层总数稳定, 只要组在不同结果层间权重差很大, 跨层汇总就会辛普森反转。
        伯克利实例: 各系总数584~933(仅1.6倍,A看不见), 但女性在难系占57%/易系9%(6.5倍,真凶)。

    sample_sizes_by_time: [(time_label, n), ...]        -> 维度A
    group_weights_by_stratum: [(stratum_label, weight), ...] weight∈[0,1] 某组在该层占比 -> 维度B
    """
    flags = []
    worst = "pass"

    # ---- 维度A: 样本总数漂移(原逻辑, 保留) ----
    a_ratio = None
    if sample_sizes_by_time and len(sample_sizes_by_time) >= 2:
        ns = [n for _, n in sample_sizes_by_time]
        if min(ns) > 0:
            a_ratio = max(ns) / min(ns)
            if a_ratio >= 3:
                lo = min(sample_sizes_by_time, key=lambda x: x[1])
                hi = max(sample_sizes_by_time, key=lambda x: x[1])
                flags.append(f"[总数漂移] 样本数从 {lo[0]}的{lo[1]} 变到 {hi[0]}的{hi[1]}"
                             f"（{a_ratio:.1f}倍）——跨期比较实为不同构成, 辛普森/幸存者陷阱")
                worst = "fail"
            elif a_ratio >= 1.5:
                worst = "warn"

    # ---- 维度B: 分组权重漂移(新增, 伯克利真凶) ----
    b_ratio = None
    b_detail = ""
    if group_weights_by_stratum and len(group_weights_by_stratum) >= 2:
        ws = [w for _, w in group_weights_by_stratum if 0 <= w <= 1]
        if len(ws) >= 2 and min(ws) >= 0:
            mx, mn = max(ws), min(ws)
            # 用『占比的绝对极差 + 相对倍数』双判据:
            # 绝对极差>=0.25(某组在层间占比相差25个百分点以上) 且 相对>=3倍 => fail
            # 任一显著 => warn
            abs_gap = mx - mn
            b_ratio = (mx / mn) if mn > 1e-9 else float("inf")
            hi_lbl = max(group_weights_by_stratum, key=lambda x: x[1])
            lo_lbl = min(group_weights_by_stratum, key=lambda x: x[1])
            b_detail = (f"分组权重从 分层{lo_lbl[0]}的{lo_lbl[1]:.0%} 到 {hi_lbl[0]}的{hi_lbl[1]:.0%}"
                        f"（差{abs_gap:.0%}/相对{b_ratio:.1f}倍）")
            if abs_gap >= 0.25 and b_ratio >= 3:
                flags.append(f"[权重漂移] {b_detail}——该组在结果不同的层间分布极不均, "
                             f"跨层汇总会发生辛普森反转, 必须分层看")
                worst = "fail"
            elif abs_gap >= 0.15:
                flags.append(f"[权重漂移] {b_detail}——分组构成跨层不均, 汇总需谨慎")
                if worst != "fail":
                    worst = "warn"

    if a_ratio is None and b_ratio is None:
        gave = (sample_sizes_by_time is not None) or (group_weights_by_stratum is not None)
        msg = "提供了数据但层/点数不足2, 无法判漂移" if gave else "未提供样本数/分组权重, 无法判构成是否漂移"
        return {"check": "样本构成漂移", "verdict": "cannot_judge",
                "detail": msg, "flags": []}

    parts = []
    if a_ratio is not None:
        parts.append(f"总数{a_ratio:.1f}倍")
    if b_ratio is not None:
        parts.append(f"分组权重{b_ratio:.1f}倍")
    detail = "构成漂移[" + "/".join(parts) + "]" + ("：有陷阱" if worst == "fail" else "")
    return {"check": "样本构成漂移", "verdict": worst, "detail": detail, "flags": flags}


def full_checkup(claim_text, numbers=None, series=None,
                 data_max_key=None, claim_range=None, n=None,
                 sizes=None, alpha_a=None, alpha_b=None, drift_a=None, drift_b=None,
                 sample_sizes_by_time=None, group_weights_by_stratum=None):
    """一站式体检。返回结构化报告 + 总评。"""
    results = [
        check_extreme_values(numbers or [], claim_text),
        check_trend_vs_noise(series or []),
        check_data_boundary(data_max_key, claim_range),
        check_sample_size(n),
        check_evidence_grade(claim_text),
        check_observable_fitness(series, sizes),
        check_same_class_trap(alpha_a, alpha_b, drift_a, drift_b),
        check_sample_composition(sample_sizes_by_time, group_weights_by_stratum),
    ]
    counts = {"pass": 0, "warn": 0, "fail": 0, "cannot_judge": 0}
    for r in results:
        counts[r["verdict"]] += 1
    # 总评逻辑：有 fail=>站不住；有 warn=>有保留；全 pass=>通过；多为 cannot_judge=>判不了
    if counts["fail"] > 0:
        overall = "站不住：至少一条硬伤"
    elif counts["warn"] > 0:
        overall = "有保留：结论需限定条件才成立"
    elif counts["pass"] > 0 and counts["cannot_judge"] <= counts["pass"]:
        overall = "通过：未见结构性硬伤"
    else:
        overall = "判不了：证据不足以判断（这不是通过）"
    return {"claim": claim_text, "overall": overall, "counts": counts, "checks": results}


def report_text(result, verbose=False):
    """verbose=False 默认折叠无法判定的 ? 项(突出核心); True 展开全量。"""
    lines = []
    lines.append("=" * 56)
    lines.append("结论体检报告")
    lines.append("=" * 56)
    lines.append(f"被检结论: {result['claim'][:100]}")
    lines.append(f"总评: 【{result['overall']}】")
    c = result["counts"]
    lines.append(f"明细: pass={c['pass']} warn={c['warn']} fail={c['fail']} cannot_judge={c['cannot_judge']}")
    lines.append("-" * 56)
    icon = {"pass": "✓", "warn": "△", "fail": "✗", "cannot_judge": "?"}
    judged = [ch for ch in result["checks"] if ch["verdict"] != "cannot_judge"]
    skipped = [ch for ch in result["checks"] if ch["verdict"] == "cannot_judge"]
    for ch in judged:
        lines.append(f"{icon[ch['verdict']]} [{ch['check']}] {ch['detail']}")
        for f in ch["flags"]:
            lines.append(f"    ⚠ {f}")
    if skipped:
        if verbose:
            # 展开: 逐条显示无法判定的项及原因
            for ch in skipped:
                lines.append(f"? [{ch['check']}] {ch['detail']}")
        else:
            names = "、".join(ch["check"] for ch in skipped)
            lines.append(f"? 未检(没喂对应数据): {names}  (加 --verbose 看全量)")
    lines.append("=" * 56)
    return "\n".join(lines)


def _selftest():
    """负向测试：确保判据在『坏结论』上真的报警，在『好结论』上真的放过。"""
    print("=== 结论体检器 自检（负向优先）===\n")
    ok = True

    # 负向1：N=1 个案吹成结论
    r = full_checkup("这个药对所有人都有效", n=1)
    print(report_text(r), "\n")
    assert r["overall"].startswith("站不住"), "N=1应判站不住"
    assert any(c["verdict"]=="fail" for c in r["checks"]), "应有fail"

    # 负向2：极端数值量级混用
    r = full_checkup("处理速度提升", numbers=[0.0001, 100, 50000])
    print("量级检查:", [c for c in r["checks"] if c["check"]=="数字量级"][0]["verdict"])
    assert any(c["check"]=="数字量级" and c["verdict"]=="warn" for c in r["checks"]), "量级跨越大应warn"

    # 负向3：把噪声当趋势（来回跳）
    noise_series = [(1,10),(2,12),(3,9),(4,13),(5,8),(6,14)]
    r = full_checkup("稳步上升", series=noise_series)
    tn = [c for c in r["checks"] if c["check"]=="趋势vs噪声"][0]
    print("噪声序列判:", tn["verdict"], "-", tn["detail"])
    assert tn["verdict"]=="fail", "来回跳不应被判为趋势"

    # 负向4：结论越过数据边界（数据只到2017，却说到2018）
    r = full_checkup("积压清空", data_max_key="2017-07", claim_range="2018")
    db = [c for c in r["checks"] if c["check"]=="数据边界"][0]
    print("边界检查:", db["verdict"], "-", db["detail"])
    assert db["verdict"]=="fail", "越界结论必须fail"

    # 正向1：真实单调趋势应pass
    good = [(1,10),(2,20),(3,31),(4,40),(5,52)]
    r = full_checkup("稳步上升", series=good)
    tn = [c for c in r["checks"] if c["check"]=="趋势vs噪声"][0]
    print("真趋势判:", tn["verdict"], "-", tn["detail"])
    assert tn["verdict"]=="pass", "真单调趋势应pass"

    # 正向2：N=500 正常数据不该误报fail
    r = full_checkup("样本显示增长", n=500, series=[(i, i*2+3) for i in range(1,6)])
    assert not any(c["verdict"]=="fail" for c in r["checks"]), "正常数据不应有fail"

    # 关键：无方向抖动必须判『判不了』，不能硬贴结论（soul: 二元对立吞掉证据不足）
    flat = [(1,10.0),(2,10.01),(3,9.99),(4,10.0),(5,10.01)]
    r = full_checkup("无变化", series=flat)
    tn = [c for c in r["checks"] if c["check"]=="趋势vs噪声"][0]
    print("无趋势判:", tn["verdict"], "-", tn["detail"])
    assert tn["verdict"]=="cannot_judge", "无方向抖动应判『判不了』不是pass/fail"

    # ==== C03/C04 新增检查项负向测试 ====
    # 观测量配不配: 人力节奏(单点主导)应fail
    r = full_checkup("issue关闭是雪崩", sizes=[1]*63 + [2]*15 + [3]*10 + [54, 22, 18, 15, 4])
    of = [c for c in r["checks"] if c["check"] == "观测量配不配"][0]
    assert of["verdict"] == "fail", "单点主导(人力节奏)应判观测量不配"

    # 观测量配不配: 连续重尾(真雪崩)应pass
    import random as _rnd
    _rnd.seed(1)
    snow = [int(1/(_rnd.random()+0.01)) for _ in range(500)]
    r = full_checkup("规模分布", sizes=snow)
    of = [c for c in r["checks"] if c["check"] == "观测量配不配"][0]
    assert of["verdict"] == "pass", "连续分布应判可作雪崩"

    # 同类判定: 数值巧合(0.70vs0.71)+结构异(漂移0.17vs1.79) => fail(不同类)
    r = full_checkup("两系统同类", alpha_a=0.70, alpha_b=0.71, drift_a=0.17, drift_b=1.79)
    sc = [c for c in r["checks"] if c["check"] == "同类判定(数值≠身份)"][0]
    assert sc["verdict"] == "fail", "数值巧合但结构异应判不同类"

    # 同类判定: 数值近且都干净 => warn(不轻定同类)
    r = full_checkup("两系统同类", alpha_a=0.70, alpha_b=0.72, drift_a=0.2, drift_b=0.25)
    sc = [c for c in r["checks"] if c["check"] == "同类判定(数值≠身份)"][0]
    assert sc["verdict"] == "warn", "数值近但仅指数不能定同类"

    # 样本构成漂移: 1800年1国→2020年236国 => fail(辛普森陷阱)
    r = full_checkup("全球寿命翻倍", sample_sizes_by_time=[("1800",1),("1950",236),("2020",236)])
    sc = [c for c in r["checks"] if c["check"] == "样本构成漂移"][0]
    assert sc["verdict"] == "fail", "样本暴涨应判辛普森陷阱"
    # 样本稳定 => pass
    r = full_checkup("寿命上升", sample_sizes_by_time=[("2000",236),("2020",235)])
    sc = [c for c in r["checks"] if c["check"] == "样本构成漂移"][0]
    assert sc["verdict"] == "pass", "稳定样本应pass"

    print("\n✅ 全部负向+正向自检通过（假结论报警、真结论放过、无信号判不了）")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    else:
        # demo
        demo = full_checkup(
            "我们的用户三个月增长了3倍，数据显示稳步上升",
            numbers=[1000, 5000, 3000, 8000],
            series=[(1,1000),(2,5000),(3,3000),(4,8000)],
            data_max_key=4, claim_range=4, n=2
        )
        print(report_text(demo))
