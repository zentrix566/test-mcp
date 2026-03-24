# main.py
import os
import json
import requests
import threading
import logging
from flask import Flask, request, jsonify
from tools import k8s_universal_getter, get_pod_logs

# 日志配置
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# 环境变量 (请确保已写入 ~/.bashrc)
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET")

# --- 飞书通讯 ---
def send_feishu_msg(open_id, text):
    # 获取 Token
    auth_url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    token_resp = requests.post(auth_url, json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}).json()
    token = token_resp.get("tenant_access_token")
    
    # 发送消息
    msg_url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    # 自动过滤 DeepSeek 偶尔输出的 DSML 标记
    clean_text = text.replace("<｜DSML｜>", "").replace("<｜DSML｜invoke", "").strip()
    payload = {"receive_id": open_id, "msg_type": "text", "content": json.dumps({"text": clean_text})}
    requests.post(msg_url, headers=headers, json=payload, timeout=10)

# --- 工具映射 ---
TOOLS_MAP = {
    "k8s_universal_getter": k8s_universal_getter,
    "get_pod_logs": get_pod_logs
}

# --- DeepSeek 推理流 ---
def process_ai_logic(open_id, user_query):
    logger.info(f"🧠 处理请求: {user_query}")
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    
    # 声明工具
    tools_schema = [
        {
            "type": "function",
            "function": {
                "name": "k8s_universal_getter",
                "description": "通用 K8s 查询工具。支持查询 pods, nodes, deployments 的列表或详情。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "resource_type": {"type": "string", "enum": ["pods", "nodes", "deployments"]},
                        "name": {"type": "string", "description": "资源名称，不传则列出所有"},
                        "namespace": {"type": "string", "default": "default"}
                    },
                    "required": ["resource_type"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_pod_logs",
                "description": "获取 Pod 的最新日志以诊断故障",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pod_name": {"type": "string"},
                        "namespace": {"type": "string", "default": "default"}
                    },
                    "required": ["pod_name"]
                }
            }
        }
    ]

    messages = [
        {"role": "system", "content": "你是一个名为 OpsAgentAI 的 K8s 专家。请通过工具查询结果回答问题。回答要专业且精炼。如果你已经拿到了工具返回的数据，请直接总结，不要再次调用工具。"},
        {"role": "user", "content": user_query}
    ]

    try:
        # 第一轮请求
        r1 = requests.post("https://api.deepseek.com/v1/chat/completions", 
                           headers=headers, 
                           json={"model": "deepseek-chat", "messages": messages, "tools": tools_schema}, timeout=30).json()
        
        choice = r1['choices'][0]['message']
        
        if choice.get("tool_calls"):
            tool_call = choice["tool_calls"][0]
            func_name = tool_call["function"]["name"]
            args = json.loads(tool_call["function"]["arguments"])
            
            # 执行对应的工具函数
            logger.info(f"🛠 调用工具: {func_name} | 参数: {args}")
            observation = TOOLS_MAP[func_name](**args)
            
            # 第二轮请求：总结
            messages.append(choice)
            messages.append({"role": "tool", "tool_call_id": tool_call["id"], "name": func_name, "content": observation})
            
            r2 = requests.post("https://api.deepseek.com/v1/chat/completions", 
                               headers=headers, 
                               json={"model": "deepseek-chat", "messages": messages}, timeout=30).json()
            answer = r2['choices'][0]['message']['content']
        else:
            answer = choice["content"]
            
        send_feishu_msg(open_id, answer)

    except Exception as e:
        logger.error(f"Logic Error: {e}")
        send_feishu_msg(open_id, f"抱歉，排查过程中遇到点麻烦: {str(e)}")

# --- Flask Webhook ---
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    if data.get("type") == "url_verification":
        return jsonify({"challenge": data.get("challenge")})
    
    if "event" in data and "message" in data["event"]:
        event = data["event"]
        open_id = event["sender"]["sender_id"]["open_id"]
        content = json.loads(event["message"]["content"])
        
        # 异步处理防止飞书超时
        threading.Thread(target=process_ai_logic, args=(open_id, content.get("text", ""))).start()

    return jsonify({"code": 0})

if __name__ == "__main__":
    logger.info("🚀 OpsAgentAI 工程化版本启动...")
    # 800MB 内存下，debug 设为 False 减少内存占用
    app.run(host='0.0.0.0', port=5000, debug=False)