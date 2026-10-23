1. 在环境文件配置单 Key：

   ```dotenv
   TAVILY_API_KEY=你的正式Key
   ```

   或多个备用 Key，非空列表优先：

   ```dotenv
   TAVILY_API_KEYS=["key1","key2","key3"]
   ```

2. 在 App 的 `tools_config_json` 原列表中追加：

   ```json
   {"tool_id":"web_search","visible":true}
   ```

3. API 和 Chat Worker 加载相同配置后重启。
