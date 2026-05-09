"""
Model evaluation engine — PPL, BLEU, ROUGE, and generation metrics.
Supports evaluating trained/finetuned models from local checkpoints
as well as online API models via LLMClient.
"""
import os
import json
import math
import time
import threading
from typing import Optional, Callable


def compute_ppl(model_path: str, dataset_path: str = None,
                dataset_texts: list = None, max_samples: int = 500,
                stride: int = 512, max_length: int = 1024,
                progress_cb: Optional[Callable] = None,
                use_llm_client: bool = False,
                model_id: str = "") -> dict:
    """Compute perplexity (PPL) on a text corpus.

    Uses sliding-window evaluation on the dataset.
    Supports both local model paths (HuggingFace) and LLMClient models.

    Args:
        model_path: Path to local HF model dir, or model_id for LLMClient
        dataset_path: Path to JSONL dataset (uses 'text' field or raw lines)
        dataset_texts: Direct list of text strings (alternative to dataset_path)
        max_samples: Max number of sliding windows to evaluate
        stride: Sliding window stride
        max_length: Max sequence length for the model
        progress_cb: Optional callback(ratio, label)
        use_llm_client: If True, use LLMClient instead of local HF loading
        model_id: Model ID for LLMClient (when use_llm_client=True)

    Returns:
        dict with ppl, avg_loss, num_samples, eval_time_seconds
    """
    import torch
    from torch.nn import CrossEntropyLoss

    t_start = time.time()

    # ── Load texts ──
    texts = dataset_texts or []
    if not texts and dataset_path and os.path.exists(dataset_path):
        with open(dataset_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    t = d.get("text", d.get("output", d.get("content", "")))
                    if isinstance(t, str) and len(t) > 50:
                        texts.append(t)
                except json.JSONDecodeError:
                    if len(line) > 50:
                        texts.append(line)

    if not texts:
        # Fallback: use wikitext test set from HuggingFace if available
        try:
            from datasets import load_dataset
            if progress_cb:
                progress_cb(0.02, "从 HuggingFace 加载 WikiText-2 测试集...")
            ds = load_dataset("Salesforce/wikitext", "wikitext-2-raw-v1", split="test")
            texts = [t for t in ds["text"] if len(t.strip()) > 50][:1000]
        except ImportError:
            raise Exception("无法加载评测数据：缺少 texts 参数且 datasets 库未安装")
        except Exception as e:
            raise Exception(f"加载评测数据失败: {e}")

    if progress_cb:
        progress_cb(0.05, f"已加载 {len(texts)} 段文本")

    # ── Tokenize ──
    if use_llm_client:
        # For API models, we approximate PPL via per-token API calls
        return _compute_ppl_via_api(model_id, texts, max_samples, stride,
                                    max_length, progress_cb, t_start)
    else:
        return _compute_ppl_local(model_path, texts, max_samples, stride,
                                  max_length, progress_cb, t_start)


def _compute_ppl_local(model_path, texts, max_samples, stride, max_length,
                       progress_cb, t_start):
    """Compute PPL by loading model locally.

    Evaluates each text segment individually to avoid creating massive
    tensors that exceed the model's context window.
    """
    import torch
    from torch.nn import CrossEntropyLoss
    from transformers import AutoModelForCausalLM, AutoTokenizer

    if progress_cb:
        progress_cb(0.08, "加载分词器...")
    tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    if progress_cb:
        progress_cb(0.12, "加载模型...")
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = AutoModelForCausalLM.from_pretrained(
        model_path, trust_remote_code=True,
        torch_dtype=torch.float16 if device.type == "cuda" else torch.float32,
    ).to(device)
    model.eval()

    # Clamp to model's actual context window
    model_max_len = getattr(model.config, "max_position_embeddings",
                            getattr(model.config, "n_positions", max_length))
    eval_len = min(max_length, model_max_len - 2)
    tokenizer.model_max_length = model_max_len

    if progress_cb:
        progress_cb(0.16, f"模型上下文: {model_max_len}, 评估窗口: {eval_len}, 文本段: {len(texts)}")

    stride_tokens = max(eval_len // 2, stride)

    loss_fn = CrossEntropyLoss(reduction="mean")
    nlls = []
    total_tokens = 0
    chunk_count = 0
    max_chunks = max_samples

    for t_idx, text in enumerate(texts):
        if chunk_count >= max_chunks:
            break

        # Tokenize each text individually with truncation
        try:
            enc = tokenizer(text, return_tensors="pt", truncation=True,
                           max_length=model_max_len - 2,
                           return_overflowing_tokens=False)
        except Exception:
            continue
        ids = enc.input_ids[0]  # 1D tensor, already truncated

        L = ids.size(0)
        if L < 50:
            continue  # too short to evaluate

        # Sliding window over this text segment
        for start in range(0, max(L - eval_len, 1), stride_tokens):
            if chunk_count >= max_chunks:
                break
            end = min(start + eval_len, L)
            if end - start < 50:
                break

            chunk = ids[start:end].unsqueeze(0).to(device)  # (1, chunk_len)

            with torch.no_grad():
                outputs = model(chunk, labels=chunk)
                loss = outputs.loss

            if loss is not None and not torch.isnan(loss) and loss.item() > 0:
                nlls.append(loss.item())
                chunk_count += 1
                total_tokens += (end - start)

        if progress_cb and t_idx % 10 == 0:
            pct = min(0.20 + 0.70 * (chunk_count / max_chunks), 0.90)
            cur_ppl = math.exp(sum(nlls) / max(len(nlls), 1)) if nlls else 999
            progress_cb(pct, f"评估中... PPL≈{cur_ppl:.1f} ({chunk_count}/{max_chunks})")

    if not nlls:
        raise Exception("PPL 计算失败：无法在给定文本上评估模型。请检查模型和数据集兼容性。")

    avg_nll = sum(nlls) / len(nlls)
    ppl = math.exp(avg_nll)
    elapsed = time.time() - t_start

    if progress_cb:
        progress_cb(1.0, f"PPL={ppl:.2f} | {len(nlls)} chunks | {elapsed:.0f}s")

    return {
        "ppl": round(ppl, 2),
        "avg_nll": round(avg_nll, 4),
        "num_windows": len(nlls),
        "total_tokens_processed": total_tokens,
        "eval_time_seconds": round(elapsed, 1),
        "method": "per_text_sliding_window",
        "stride": stride,
        "max_length": eval_len,
    }


def _compute_ppl_via_api(model_id, texts, max_samples, stride, max_length,
                         progress_cb, t_start):
    """Approximate PPL via LLMClient API calls (token-level logprobs)."""
    # This is an approximation — API models may not return full logprobs.
    # We use a simplified approach: compute per-token cross-entropy from
    # the API logprobs if available, otherwise fall back to a heuristic.
    try:
        from core.llm_client import LLMClient
    except ImportError:
        raise Exception("LLMClient 不可用")

    client = LLMClient(default_model=model_id)
    full_text = "\n\n".join(texts[:max_samples])
    words = full_text.split()
    total_loss = 0.0
    total_tokens = 0

    # For API models, we use a simpler approach: ask the model to score
    # text sequences and estimate PPL from the loss-like signal
    chunk_size = 256
    chunks = [full_text[i:i+chunk_size] for i in range(0, min(len(full_text), chunk_size * 100), chunk_size)]

    if progress_cb:
        progress_cb(0.10, f"通过 API 评估 ({len(chunks)} chunks)...")

    # Note: Most API models don't expose logprobs needed for true PPL.
    # This is a best-effort approximation using completion APIs.
    for idx, chunk in enumerate(chunks):
        try:
            response, _ = client.chat(messages=[
                {"role": "user", "content": f"Continue the following text exactly as is, without adding anything:\n\n{chunk}"}
            ])
            # Heuristic: compare response length with expected
            if response and hasattr(response, "usage"):
                usage = response.usage
                total_tokens += usage.total_tokens if usage else 0
        except Exception:
            pass
        if progress_cb and idx % 5 == 0:
            progress_cb(0.10 + 0.80 * (idx / len(chunks)), f"API评估 {idx}/{len(chunks)}")

    elapsed = time.time() - t_start
    # This is a rough approximation; flag it as such
    return {
        "ppl": None,  # Cannot compute true PPL via API
        "avg_nll": None,
        "num_windows": len(chunks),
        "total_tokens_processed": total_tokens,
        "eval_time_seconds": round(elapsed, 1),
        "method": "api_approximation",
        "warning": "PPL 无法通过 API 精确计算，请使用本地模型进行评估。",
    }


def compute_generation_metrics(model_path: str, dataset_path: str,
                               max_samples: int = 100,
                               progress_cb: Optional[Callable] = None) -> dict:
    """Compute BLEU and ROUGE-L on a reference-hypothesis dataset.

    Dataset should be JSONL with 'input' (or 'question') and 'output' (or 'answer') fields.
    The model generates a response for each input, then compares with the reference.
    """
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    if not os.path.exists(dataset_path):
        raise Exception(f"数据集不存在: {dataset_path}")

    # Load samples
    samples = []
    with open(dataset_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                inp = d.get("input", d.get("question", d.get("instruction", "")))
                ref = d.get("output", d.get("answer", d.get("response", "")))
                if inp and ref:
                    samples.append((str(inp), str(ref)))
            except json.JSONDecodeError:
                pass
    samples = samples[:max_samples]

    if not samples:
        raise Exception("数据集中未找到 'input'/'output' 或 'question'/'answer' 字段对")

    if progress_cb:
        progress_cb(0.05, f"加载模型...")

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        model_path, trust_remote_code=True,
        torch_dtype=torch.float16 if device.type == "cuda" else torch.float32,
    ).to(device)
    model.eval()

    references = []
    hypotheses = []
    total_time = 0.0

    for idx, (inp, ref) in enumerate(samples):
        t0 = time.time()
        try:
            inputs = tokenizer(inp, return_tensors="pt", truncation=True,
                              max_length=512).to(device)
            with torch.no_grad():
                outputs = model.generate(
                    **inputs, max_new_tokens=256, do_sample=False,
                    pad_token_id=tokenizer.pad_token_id,
                )
            gen_text = tokenizer.decode(outputs[0][inputs.input_ids.shape[1]:],
                                        skip_special_tokens=True)
            references.append(ref)
            hypotheses.append(gen_text)
            total_time += time.time() - t0
        except Exception as e:
            print(f"[Eval] Generation failed for sample {idx}: {e}")

        if progress_cb and idx % 5 == 0:
            progress_cb(0.10 + 0.80 * ((idx + 1) / len(samples)),
                       f"生成中 {idx+1}/{len(samples)}")

    if not hypotheses:
        raise Exception("所有样本生成失败")

    # Compute metrics
    metrics = _compute_all_metrics(references, hypotheses)
    metrics["num_samples"] = len(hypotheses)
    metrics["avg_gen_time_ms"] = round((total_time / len(hypotheses)) * 1000, 1)

    if progress_cb:
        progress_cb(1.0, f"BLEU={metrics.get('bleu', 0):.1f} | {len(hypotheses)} samples")

    return metrics


def _compute_all_metrics(references: list, hypotheses: list) -> dict:
    """Compute BLEU, ROUGE-L, and exact-match metrics."""
    try:
        from nltk.translate.bleu_score import sentence_bleu, SmoothingFunction
        import nltk
        try:
            nltk.data.find('tokenizers/punkt')
        except LookupError:
            nltk.download('punkt', quiet=True)
        smooth = SmoothingFunction().method1
        bleu_scores = []
        for ref, hyp in zip(references, hypotheses):
            ref_tokens = [ref.lower().split()]
            hyp_tokens = hyp.lower().split()
            try:
                bleu_scores.append(sentence_bleu(ref_tokens, hyp_tokens,
                                                  smoothing_function=smooth))
            except Exception:
                bleu_scores.append(0)
        bleu = sum(bleu_scores) / max(len(bleu_scores), 1) * 100
    except ImportError:
        bleu = 0
        bleu_scores = [0]

    # ROUGE-L (simple implementation)
    rouge_l_scores = []
    for ref, hyp in zip(references, hypotheses):
        r = _rouge_l(ref, hyp)
        rouge_l_scores.append(r)
    rouge_l = sum(rouge_l_scores) / max(len(rouge_l_scores), 1) * 100

    # Exact match
    em = sum(1 for r, h in zip(references, hypotheses)
             if r.strip().lower() == h.strip().lower()) / max(len(hypotheses), 1) * 100

    return {
        "bleu": round(bleu, 2),
        "rouge_l": round(rouge_l, 2),
        "exact_match": round(em, 2),
    }


def _rouge_l(reference: str, hypothesis: str) -> float:
    """Compute ROUGE-L F1 score (longest common subsequence)."""
    ref_words = reference.lower().split()
    hyp_words = hypothesis.lower().split()
    if not ref_words or not hyp_words:
        return 0.0

    # LCS using DP
    m, n = len(ref_words), len(hyp_words)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if ref_words[i-1] == hyp_words[j-1]:
                dp[i][j] = dp[i-1][j-1] + 1
            else:
                dp[i][j] = max(dp[i-1][j], dp[i][j-1])
    lcs = dp[m][n]

    if lcs == 0:
        return 0.0
    recall = lcs / m
    precision = lcs / n
    if recall + precision == 0:
        return 0.0
    return 2 * recall * precision / (recall + precision)
