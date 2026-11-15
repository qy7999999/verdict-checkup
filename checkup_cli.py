#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checkup_cli.py —— 结论体检器·命令行版
给一个『用数据支撑的结论』+ 数据文件，做结构化体检，输出人看得懂的报告。

用法:
  python skills/checkup_cli.py --claim "全球寿命翻倍" --csv data.csv --x Year --y Value
  python skills/checkup_cli.py --claim "某结论" --sizes sizes.txt   # 一行一个事件规模
  python skills/checkup_cli.py --selftest
  python skills/checkup_cli.py --demo    # 零门槛演示, 无需数据

设计: 包 verdict_checkup 引擎(9项检查), 自动从数据推断该喂哪些检查。
不改变被检结论本身, 只指出它站不站得住、病在哪。
"""
import sys, os, csv, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verdict_checkup as V


def _read_csv_xy(path, xcol, ycol, entity=None, entity_col="Entity"):
    """读 CSV 为 [(x,y)]，并在算之前先做【结构体检】：多实体/重复时间戳/乱序。
    结构有问题就抛带病因的错——不把『数据错』误诊成『趋势假』。"""
    rows = list(csv.DictReader(open(path)))
    if not rows:
        raise ValueError("CSV 为空")
    keys = rows[0].keys()
    if xcol not in keys or ycol not in keys:
        firstvals = list(keys)
        hint = ""
        # 若表头看起来是数字(说明这其实是『一列数字无表头』), 给明确指引
        try:
            [float(k) for k in firstvals]; hint = "\n    看起来这份文件只有一列数字、没有表头——用 --values 直接喂它, 别用 --csv。"
        except (ValueError, TypeError):
            pass
        raise ValueError(f"列名 '{xcol}'/'{ycol}' 不存在。文件实际的列是: {firstvals}。{hint}")

    # ---- 结构侦测 1: 多实体 ----
    if entity_col in keys:
        entities = set(r.get(entity_col, "") for r in rows if r.get(entity_col))
        if entity is not None:
            if str(entity).lower() not in {e.lower() for e in entities}:
                sample = sorted(entities)[:8]
                raise ValueError(f"未找到实体 '{entity}'。可选(前8): {sample}")
        elif len(entities) > 1:
            sample = sorted(entities)[:8]
            raise ValueError(
                f"这份数据含 {len(entities)} 个不同实体(如 {sample}...), 但你没用 --entity 指定。\n"
                f"    把多个实体串成一条线 = 假趋势(不同国家/个体的数据混在一起)。\n"
                f"    请加 --entity <名称> 指定一个, 例如: --entity {sample[0]}")

    # ---- 取该实体的行 ----
    sel = []
    for r in rows:
        if entity is not None:
            if str(r.get(entity_col, "")).lower() != str(entity).lower():
                continue
        try:
            sel.append((float(r[xcol]), float(r[ycol])))
        except (ValueError, KeyError):
            continue
    if not sel:
        raise ValueError(f"过滤后无有效数据(entity={entity})")

    # ---- 结构侦测 2: 重复时间戳(同一 x 出现多个不同 y) ----
    from collections import defaultdict
    byx = defaultdict(list)
    for x, y in sel:
        byx[x].append(y)
    dups = {x: ys for x, ys in byx.items() if len(set(ys)) > 1}
    if dups:
        ex = list(dups.items())[:3]
        detail = "; ".join(f"{x}: 值={sorted(set(ys))}" for x, ys in ex)
        raise ValueError(
            f"发现 {len(dups)} 个重复时间戳(同一 x 有多个不同 y)：{detail}。\n"
            f"    这多半是『数据没按某个维度分组』(如同名实体/同时间多来源)或数据本身重复录入。\n"
            f"    把冲突值一起塞进趋势 = 把数据错当成趋势错。请先确认数据粒度(是否该加 --entity 或查重)。")

    # ---- 结构侦测 3: 时间乱序(读入顺序非单调)——只提醒, 不阻断(排序即可), 但记录 ----
    xs = [x for x, _ in sel]
    if xs != sorted(xs):
        # 排序能解决, 不阻断; 但要标记(有些工具依赖顺序, 乱序可能意味着数据有问题)
        pass

    sel.sort()
    return sel


def _read_values(path):
    """读一列数字(每行一个值), 自动编号成 [(0,v0),(1,v1),...]。
    忽略空行与 # 注释行。给『手上只有一列数、没有 x 列』的陌生人用。"""
    vals = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            vals.append(float(line))
        except ValueError:
            raise ValueError(f"这一行不是数字: '{line[:40]}'。--values 需要每行一个数字。")
    if len(vals) < 3:
        raise ValueError(f"只读到 {len(vals)} 个数字, 趋势判据需>=3。")
    return [(i, v) for i, v in enumerate(vals)]


def _read_sizes(path):
    vals = []
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        try:
            vals.append(float(line))
        except ValueError:
            pass
    return vals


def _read_group_weights(path):
    """读『分组权重』文件: 每行 `分层标签<TAB或空格>权重(0~1)`。"""
    out = []
    for ln in open(path, encoding="utf-8"):
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        parts = ln.rsplit(None, 1)
        if len(parts) != 2:
            raise ValueError(f"格式应为: 分层标签 权重, 得到: {ln!r}")
        label, w = parts
        out.append((label, float(w)))
    return out


def _run_demo():
    """零门槛演示: 用内置的两个典型案例展示它能抓什么坑, 不要求用户准备数据。"""
    print("=" * 60)
    print("结论体检器 · 演示（不需要你准备数据, 直接看它怎么工作）")
    print("=" * 60)
    print()

    print("【案例1】一个站不住的结论：")
    print('  「全球平均寿命从1800年32岁涨到2020年73岁，人类寿命翻倍」')
    r = V.full_checkup(
        "全球平均寿命从1800年32岁涨到2020年73岁, 人类寿命翻倍",
        sample_sizes_by_time=[("1800", 1), ("1850", 8), ("1900", 35), ("1950", 236), ("2020", 236)],
    )
    print(V.report_text(r))
    print()

    print("【案例2】一个真实但带波动的趋势（不该被误判为噪声）：")
    # 瑞典式: 整体强上升但每年有波动
    import math
    series = [(y, 40 + 0.25*(y-1750) + 3*math.sin((y-1750)/11.0)) for y in range(1750, 2024, 5)]
    r2 = V.full_checkup("瑞典寿命随时间上升", series=series, n=len(series),
                        data_max_key=str(int(series[-1][0])), claim_range=str(int(series[-1][0])))
    print(V.report_text(r2))
    print()
    print("=" * 60)
    print("看懂了吗？给它你的 data.csv + 一句结论，它就这样告诉你坑在哪：")
    print('  python skills/checkup_cli.py --claim "你的结论" --csv data.csv --x 列1 --y 列2')
    print()


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        V._selftest()
        return
    if "--demo" in args:
        _run_demo()
        return
    if any(a in ("-h", "--help") for a in args):
        print(__doc__); return

    # 参数白名单
    KNOWN = {"--claim", "--csv", "--x", "--y", "--values", "--sizes", "--n", "--entity", "--verbose", "--group-weights"}
    opts = {}
    i = 0
    BOOL_FLAGS = {"--verbose"}  # 布尔开关, 后面不跟值
    while i < len(args):
        a = args[i]
        if a not in KNOWN:
            print(f"✗ 未知参数: {a}\n用 --help 看用法"); sys.exit(2)
        if a in BOOL_FLAGS:
            opts[a] = True; i += 1; continue
        if i + 1 >= len(args):
            print(f"✗ {a} 缺值"); sys.exit(2)
        opts[a] = args[i + 1]; i += 2

    claim = opts.get("--claim")
    if not claim:
        print("✗ 必须提供 --claim \"你的结论\""); sys.exit(2)

    series = None
    sizes = None
    n = int(opts["--n"]) if "--n" in opts else None
    data_max = None
    claim_range = None

    if "--values" in opts:
        if "--csv" in opts:
            print("✗ --values 和 --csv 只用其一（--values 给一列数字，--csv 给带表头的两列）"); sys.exit(2)
        try:
            series = _read_values(opts["--values"])
        except Exception as e:
            print(f"✗ 读 --values 失败: {e}"); sys.exit(1)
        n = n or len(series)
        data_max = str(int(series[-1][0]))
        claim_range = data_max

    if "--csv" in opts:
        if "--x" not in opts or "--y" not in opts:
            print("✗ --csv 需要配 --x 和 --y 列名"); sys.exit(2)
        try:
            series = _read_csv_xy(opts["--csv"], opts["--x"], opts["--y"], opts.get("--entity"))
        except Exception as e:
            print(f"✗ 读CSV失败: {e}"); sys.exit(1)
        if series:
            n = n or len(series)
            data_max = str(int(series[-1][0]))
            claim_range = data_max
        # 同时把 y 值也当 sizes 检查观测量? 不. y 是趋势序列, 不一定是事件规模.

    if "--sizes" in opts:
        try:
            sizes = _read_sizes(opts["--sizes"])
        except Exception as e:
            print(f"✗ 读sizes失败: {e}"); sys.exit(1)
        n = n or len(sizes)

    group_weights = None
    if "--group-weights" in opts:
        try:
            group_weights = _read_group_weights(opts["--group-weights"])
        except Exception as e:
            print(f"✗ 读group-weights失败: {e}"); sys.exit(1)

    result = V.full_checkup(
        claim, series=series, sizes=sizes, n=n,
        data_max_key=data_max, claim_range=claim_range,
        group_weights_by_stratum=group_weights,
    )
    print(V.report_text(result, verbose=bool(opts.get("--verbose"))))
    print()
    print("说明: 体检≠否定。它指出『这个结论需要什么限定条件才成立』，")
    print("     以及『哪些坑它可能踩了』。pass 也不代表绝对对，是无法从数据看出的结构性硬伤。")


if __name__ == "__main__":
    main()
