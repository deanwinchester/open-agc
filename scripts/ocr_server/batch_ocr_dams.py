# -*- coding: utf-8 -*-
"""批量 OCR 报纸 PDF 并回写 DAMS historypdf 库（骨架脚本，默认 dry-run）。

流程：输入一个 PDF 目录 → 逐文件用 pymupdf 渲染每页 → POST 到远程
Unlimited-OCR 服务（scripts/ocr_server/ocr_service.py 部署的那台）出全文
→ 拼接后 POST 回 DAMS historypdf 库。

DAMS 环境（内网免鉴权）：
    API base : http://192.168.200.151:8370/dams/api/v1
    folderId : 63d9cf887a9a1b708dc55e6e  （historypdf 库）

⚠️ 回写字段是按 zxsai-server 的 DamsSearchService 读取口径拟的骨架——
字段名（title/content/date 等）标注了 TODO，**正式跑之前务必与 DAMS
管理端核对一次真实字段口径**，否则写进去检索不到。

用法：
    # 预演（默认 dry-run，只打印不写入）
    python batch_ocr_dams.py /path/to/pdf_dir \
        --ocr-url http://192.168.148.200:8377

    # 正式回写
    python batch_ocr_dams.py /path/to/pdf_dir \
        --ocr-url http://192.168.148.200:8377 --apply
"""
import argparse
import base64
import os
import re
import sys
import time

import requests

DAMS_API_BASE = "http://192.168.200.151:8370/dams/api/v1"
DAMS_FOLDER_ID = "63d9cf887a9a1b708dc55e6e"  # historypdf 库

_PDF_DPI = 170          # 与 open-agc 端 tools/pdf_ocr.py 同一渲染口径
_OCR_TIMEOUT = 120
_DAMS_TIMEOUT = 30


def render_pdf_pages(pdf_path: str, max_pages: int = 0):
    """pymupdf 渲染 PDF 每页为 PNG 字节。max_pages=0 表示全部页。"""
    import pymupdf
    doc = pymupdf.open(pdf_path)
    try:
        total = doc.page_count
        n = total if max_pages <= 0 else min(total, max_pages)
        mat = pymupdf.Matrix(_PDF_DPI / 72, _PDF_DPI / 72)
        return [doc.load_page(i).get_pixmap(matrix=mat).tobytes("png")
                for i in range(n)], total
    finally:
        doc.close()


def ocr_pages(ocr_url: str, pages_png, label: str = "") -> str:
    """逐页调远程 OCR 服务，按页序拼全文。"""
    texts = []
    for i, png in enumerate(pages_png, 1):
        resp = requests.post(
            f"{ocr_url.rstrip('/')}/ocr",
            json={"image_b64": base64.b64encode(png).decode("ascii"), "page": i},
            timeout=_OCR_TIMEOUT,
        )
        resp.raise_for_status()
        texts.append(resp.json().get("text") or "")
        print(f"  [{label}] 第 {i}/{len(pages_png)} 页完成", flush=True)
    return "\n\n".join(t for t in texts if t)


def guess_issue_meta(filename: str):
    """从文件名猜报纸期号/日期（如 人民日报_19850312.pdf）。

    TODO: 与 DAMS 管理端核对日期格式与期号口径后完善。
    """
    stem = os.path.splitext(os.path.basename(filename))[0]
    m = re.search(r"(19\d{2}|20\d{2})[-_.年]?(\d{2})[-_.月]?(\d{2})日?", stem)
    date_str = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""
    title = re.sub(r"[-_.]?\d{6,8}日?", "", stem).strip("_-. ") or stem
    return title, date_str


def build_dams_payload(pdf_path: str, full_text: str) -> dict:
    """组装 DAMS 回写记录。

    TODO: 以下字段名按 zxsai-server DamsSearchService 读取口径拟的骨架，
    正式跑之前与 DAMS 管理端核对一次：
      - title   : 标题 = 报纸名称 + 期号/日期
      - content : 全文（OCR 结果）
      - date    : 出版日期（检索过滤字段）
      - folderId: historypdf 库固定 63d9cf887a9a1b708dc55e6e
    其余 DAMS 侧要求的必填字段（如 source/type/metadata 结构）待核对后补齐。
    """
    title, date_str = guess_issue_meta(pdf_path)
    return {
        "folderId": DAMS_FOLDER_ID,
        "title": f"{title} {date_str}".strip(),   # TODO: 核对标题口径
        "content": full_text,                      # TODO: 核对全文字段名
        "date": date_str,                          # TODO: 核对日期字段名/格式
    }


def post_to_dams(payload: dict) -> dict:
    """写入 DAMS historypdf 库。

    TODO: 端点路径（/docs? /records?）与请求方法待与 DAMS 管理端核对。
    """
    resp = requests.post(f"{DAMS_API_BASE}/docs", json=payload,
                         timeout=_DAMS_TIMEOUT)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def main():
    ap = argparse.ArgumentParser(description="批量 OCR 报纸 PDF 并回写 DAMS historypdf 库")
    ap.add_argument("pdf_dir", help="PDF 文件目录")
    ap.add_argument("--ocr-url", default="http://192.168.148.200:8377",
                    help="Unlimited-OCR 服务地址")
    ap.add_argument("--max-pages", type=int, default=0,
                    help="每个 PDF 最多 OCR 页数（0=全部）")
    ap.add_argument("--apply", action="store_true",
                    help="正式写入 DAMS（默认 dry-run 只打印）")
    args = ap.parse_args()

    pdfs = sorted(f for f in os.listdir(args.pdf_dir)
                  if f.lower().endswith(".pdf"))
    if not pdfs:
        print(f"目录下没有 PDF: {args.pdf_dir}")
        return 1
    mode = "正式写入" if args.apply else "DRY-RUN（不写入 DAMS）"
    print(f"共 {len(pdfs)} 个 PDF，模式: {mode}")

    ok, fail = 0, 0
    for idx, name in enumerate(pdfs, 1):
        path = os.path.join(args.pdf_dir, name)
        print(f"[{idx}/{len(pdfs)}] {name}", flush=True)
        try:
            pages, total = render_pdf_pages(path, args.max_pages)
            t0 = time.time()
            full_text = ocr_pages(args.ocr_url, pages, label=name)
            payload = build_dams_payload(path, full_text)
            print(f"  OCR 完成：{len(pages)}/{total} 页，"
                  f"{len(full_text)} 字符，{time.time() - t0:.1f}s")
            if args.apply:
                post_to_dams(payload)
                print("  已回写 DAMS")
            else:
                print(f"  [dry-run] 将回写: title={payload['title']!r} "
                      f"date={payload['date']!r} content={len(payload['content'])} 字符")
            ok += 1
        except Exception as e:
            print(f"  失败: {e}", file=sys.stderr)
            fail += 1
    print(f"完成：成功 {ok}，失败 {fail}")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
