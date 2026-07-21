# 阶段 3：训练插件升级 — 实施计划

> 来源：总方案阶段 3。目标：小规模训练/微调验证闭环可用、正确、可对比。
> 范围说明：训练插件 `plugins/open-agc-train/` 是 **git 子模块**（改动在子模块内，由控制者统一提交）；主仓文件仅在任务明确列出时可改。
> 全局约束：
> - 插件保持可选形态，重依赖（torch/transformers 等）不进入主程序 import 路径
> - 不做 git 操作（控制者统一提交）；文件 UTF-8
> - 每个任务完成后：`python -m pytest tests/ -q` 不回归；插件 Python 文件过 `python -c "import ast; ast.parse(...)"`；`npm run build`（如涉及前端）通过
> - 参考证据：全量审查报告中的训练插件条目（行号可能漂移，以代码为准）

## Task 1：训练引擎正确性修复（plugins/open-agc-train/engine.py 为主）

1. **早停失效**：engine.py 每个 batch 用训练 loss 刷新 `best_loss` 并重置 `no_improve_epochs`（约 :814-816），而早停判定（约 :911-915）拿验证 loss 与同一个 `best_loss` 比较 —— 训练 loss 几乎每 batch 都在降，早停计数器被持续清零永不触发。修复：拆成 `best_train_loss` 与 `best_val_loss` 两个变量，早停只看验证指标。
2. **val_ratio 不生效**：routes.py（约 :396）把 `req.val_ratio` 写进 `run_record["val_ratio"]`，engine.py（约 :486）却从 `training_params_json` 读 `params.get("val_ratio", 0.1)`。修复：engine 优先读 `run_record.get("val_ratio")`，缺省回落 params 再回落 0.1。
3. **abort→start 双线程竞态**：abort（约 :401-409）只置 `active=False`，旧线程要等下一 batch 才检查 `_abort_flag`；立即 `start_training` 会 `clear()` abort flag（约 :363），旧线程发现 flag 已清继续训练 → 两个训练线程并发共享 `self._state`/`_act_buffer`/hooks。修复：start 前确认旧线程已退出（join 带超时；仍在跑则拒绝启动并返回明确错误）。
4. **_state 迭代竞态**：训练线程运行中给 `_state` 新增 key（`total_epochs`、`steps_per_epoch`，约 :810-811），API 线程 `get_state()` 的 `dict(self._state)`（约 :351）迭代时遇插入可抛 `RuntimeError`。修复：`_state` 在初始化时预建全部 key；`get_state()` 加锁或 `dict(list(self._state.items()))` 快照。
5. **import 期强改 HF_ENDPOINT**：engine.py（约 :18）`os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"` 在模块导入时无条件执行，覆盖用户配置。修复：改 `os.environ.setdefault`。
6. **迁移统计错误即删旧表**（db.py 约 :179-198）：`INSERT OR IGNORE` 对约束冲突静默跳过但 `migrated += 1` 仍计数，随后 `migrated > 0` 就 DROP 旧表——部分行没插进去时旧表已被删。修复：用 `cursor.rowcount` 统计真实插入数；不一致时改 DROP 为 RENAME 备份（表名加 `_legacy_` 前缀 + 时间戳）。
7. **stride 计算方向反**（eval.py 约 :122）：`stride_tokens = max(eval_len // 2, stride)` 用户传小 stride 求精细评估会被钳大；返回值里 `"stride"` 上报的是请求值而非实际使用值。修复：`min(stride, eval_len)`（或直接用 stride），如实上报。
8. **import json 条件分支**（engine.py 约 :472-474 vs :693）：`import json` 只在 params 是 str 的分支里执行；dict + val_dataset 路径会 NameError。修复：函数顶部统一 import。

验证：以上每点在报告里给出 before/after 代码位置与理由；能写最小单测的（早停计数逻辑、stride、_state 快照）写到 `plugins/open-agc-train/tests/`（若目录不存在则新建，pytest 可直接跑，不依赖 torch —— 把被测逻辑抽成纯函数或用 monkeypatch 替身）。

## Task 2：自定义架构接口兼容 + exec 安全（plugins/open-agc-train/）

1. **自定义/Mamba/DiT 架构与 HF 训练循环不兼容**：engine.py（约 :770-771）调用 `model(input_ids=..., attention_mask=..., labels=labels)` 并取 `outputs.loss`（HF 风格）；但 codegen.py（约 :249）生成的 `CustomModel.forward(input_ids, attention_mask=None)` 不接受 `labels` 且返回裸 tensor；architectures.py（约 :211）的 `MambaBuilder` 只返回单个 block。修复：生成的模型实现 HF 兼容签名（接受 `labels`，返回带 `.loss` 的对象——可定义轻量 `ModelOutput` dataclass）；Mamba/DiT builder 包装成完整 LM（embedding + blocks + lm_head + 同样的 forward 签名）。
2. **exec 代码生成插值校验**：`CustomBuilder.build_model`（architectures.py 约 :281）对生成代码直接 `exec`，而代码由用户提交的 config_json 经 f-string 插值生成（codegen.py 多处，如 :173 `cfg.get('vocab_size')` 原样拼进源码）——字符串型恶意值即注入任意 Python。修复：插值前对所有数值字段做类型校验/强转（int/float/bool/白名单枚举字符串），拒绝其他类型与含引号/换行的字符串值。
3. **重活移出事件循环**：routes.py 的 `test_trained_model`（约 :440-479，同步 `from_pretrained` + `generate`）与 `eval_generation_metrics`（约 :245，逐样本 generate）在 async handler 里同步执行可阻塞事件循环数分钟到数小时。修复：改为后台线程执行 + 立即返回 job id + WS/轮询推送进度（参照 eval-ppl 已有的后台模式，先读现有实现复用同一套 job 机制）。

验证：ast.parse；能起最小模型 smoke 的写 smoke（CPU 上 2 层小模型前向一步，无 GPU 依赖则标记 skip）；报告说明每处修改的兼容性影响。

## Task 3：split-brain 消除（插件路由模块状态共享）

1. **插件重复 init 主程序路由模块**：`plugins/open-agc-train/routes.py`（约 :34-42、:49-57）的 `create_router()` 再次调用 `api.routes.benchmark.init_benchmark_routes` 与 `api.routes.downloads.init_download_routes`，把主程序已在 `api/server.py:112-129` 注入过的模块级全局（`_db_path`、`_llamacpp_download_state`、`_training_install_state`、`_get_training_engine`）整体替换：`_db_path` 改指插件的 training.db、`_llamacpp_download_state` 换成新 dict（插件加载晚于主初始化，插件胜出）→ 主程序与插件持有两个不同状态。
   修复方向（二选一，先读代码评估后说明理由）：
   a. 插件不再调用主程序模块的 init，改为只注册自己需要的路由，共享主程序已注入的状态（如 benchmark/downloads 主程序端点本来就覆盖其需求）；
   b. 把 benchmark/downloads 路由模块的"模块级全局 + init"改为显式依赖注入（类或 AppState），主程序与插件各持一份且不互相覆盖。
   同时修 `_llamacpp_download_state` 的 rebinding 问题（主仓 `api/routes/routes_settings.py` 用 `global` 整体赋值，改为原地 `.clear(); .update()`）。
2. **陈旧副本删除**：`plugins/open-agc-train/routes_datasets.py`、`routes_benchmark.py` 是与主程序漂移的未挂载副本（`routes_datasets.py:659` 还 import 不存在的 `core.training_engine`），删除。

验证：`python -c "import api.server"` 干净；调用主程序 `/api/downloads` 与插件对应端点，确认状态一致（报告给证据）；pytest 不回归。

## Task 4：实验对比视图（插件前端 + 后端）

1. **runs 与 benchmarks 关联**：检查插件 db（plugins/open-agc-train/db.py）两表结构，确保可按模型/run 聚合查询；缺关联字段则补（如 benchmark 记录 run_id 或 model_path），含轻量迁移。
2. **对比端点**：新增 `GET /api/plugin/open-agc-train/runs/compare?ids=1,2,3`，返回各 run 的指标时间序列（loss/lr 按 step）+ 最终指标 + 关联 benchmark 结果，供前端叠图。
3. **对比视图**（`plugins/open-agc-train/static/vue/history.js` 或新 compare.js）：多选 run → 指标曲线叠加（纯 SVG/Canvas 手绘，不引图表库）+ 指标对比表格。
4. 训练曲线实时性：monitor 视图 loss 曲线接入对比视图同一数据源。

验证：npm run build；对比端点 curl 实测；报告含 UI 结构说明。

## Task 5：一键验证流程模板 ×3

在插件 UI（scratch/finetune 视图）加"快速验证"预设按钮：
1. **LoRA 微调冒烟**：小基座 + 推荐小数据集 + 几百步 + LoRA 低 rank，参数一键填入（用户仍可改再启动）
2. **玩具预训练**：从零自定义小模型（2 层小 hidden）+ 小数据集 + 短步数，验证架构配置与训练链路
3. **微调前后 PPL 对比**：向导式两步（先评基座 PPL → 训练 → 再评产物 PPL），第二步自动带入基座结果做对比展示
实现：预设只是前端参数模板 + 现有端点组合调用，不新增训练后端能力（第 3 个允许加一个"PPL 对比"的简单结果展示组件）。

验证：npm run build；报告列出每个预设的参数表与调用端点。
