# API 测试技能

## 触发条件
当用户需要测试 API 接口、调试接口返回结果、或验证 API 端点时。

## 步骤
1. **了解 API 基本信息**
   - 确认目标 URL、HTTP 方法、认证方式
2. **构造和发送请求**
   ```python
   import requests, json

   # GET 请求
   resp = requests.get(url, headers=headers, params=params, timeout=10)

   # POST 请求
   resp = requests.post(url, headers=headers, json=body, timeout=10)

   # 输出结果
   print(f"Status: {resp.status_code}")
   print(f"Headers: {dict(resp.headers)}")
   print(f"Body: {json.dumps(resp.json(), indent=2, ensure_ascii=False)}")
   ```
3. **验证响应**
   - 状态码是否正确（200/201/204 等）
   - 响应格式是否符合预期
   - 必填字段是否存在，数据类型是否正确
4. **批量测试**
   - 正常参数测试
   - 边界值测试（空字符串、超长字符串、特殊字符）
   - 错误参数测试（缺少必填字段、类型错误）
   - 认证测试（无 token、过期 token）
5. **生成测试报告**，标记通过/失败的用例

## 输出格式
- 以 Markdown 表格展示每个用例的测试结果
- 标注 ✅ 通过 / ❌ 失败

## 涉及工具
- `execute_python` — 构造和发送 HTTP 请求
- `read_file` — 读取 API 配置文件
