# -*- coding: utf-8 -*-
"""阶段5 Task1: schema 结构等价校验。

对比 schemas_before.json 与 schemas_after.json：
1. 工具集合一致（名字不变）
2. 每个工具的 parameters 结构字节一致（属性名、type、enum、required、嵌套结构）——
   校验方式：递归删除所有 description 字段后，JSON 必须完全相等
3. description 之外不允许任何 diff
"""
import io
import json
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")


def strip_descriptions(node):
    """递归删除 dict 中所有 'description' 键。"""
    if isinstance(node, dict):
        return {k: strip_descriptions(v) for k, v in node.items() if k != "description"}
    if isinstance(node, list):
        return [strip_descriptions(x) for x in node]
    return node


def main():
    with open("scratch/schemas_before.json", encoding="utf-8") as f:
        before = json.load(f)["schemas"]
    with open("scratch/schemas_after.json", encoding="utf-8") as f:
        after = json.load(f)["schemas"]

    fails = []

    # 1. 工具名集合一致
    names_b, names_a = set(before), set(after)
    if names_b != names_a:
        fails.append(f"工具集合不一致: only_before={names_b - names_a}, only_after={names_a - names_b}")

    # 2. 每个工具去 description 后结构完全相等
    for name in sorted(names_b & names_a):
        sb = strip_descriptions(before[name])
        sa = strip_descriptions(after[name])
        jb = json.dumps(sb, ensure_ascii=False, sort_keys=True)
        ja = json.dumps(sa, ensure_ascii=False, sort_keys=True)
        if jb != ja:
            fails.append(f"{name}: 结构不一致!\n  before={jb}\n  after={ja}")
        # 3. 参数名集合 + required 显式确认（冗余保险）
        pb = before[name]["function"].get("parameters", {})
        pa = after[name]["function"].get("parameters", {})
        if set(pb.get("properties", {})) != set(pa.get("properties", {})):
            fails.append(f"{name}: 参数名集合不一致")
        if pb.get("required") != pa.get("required"):
            fails.append(f"{name}: required 不一致")

    # 4. 格式规范检查：工具 description ≤200 字、参数 description ≤60 字
    for name, sch in sorted(after.items()):
        fn = sch["function"]
        dlen = len(fn.get("description", ""))
        if dlen > 200:
            fails.append(f"{name}: 工具 description {dlen} 字 > 200")
        for pn, pd in fn.get("parameters", {}).get("properties", {}).items():
            plen = len(pd.get("description", ""))
            if plen > 60:
                fails.append(f"{name}.{pn}: 参数 description {plen} 字 > 60")

    if fails:
        print(f"FAILED ({len(fails)} problems):")
        for x in fails:
            print(" -", x)
        raise SystemExit(1)
    print(f"OK: {len(before)} 工具结构等价（名字/参数/required/enum 全部不变），格式规范通过")


if __name__ == "__main__":
    main()
