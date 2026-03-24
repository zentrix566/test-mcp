import os
import json
import requests
import threading
import logging
from flask import Flask, request, jsonify
from kubernetes import client, config

# --- 日志配置：同时输出到控制台和文件 ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# --- 环境变量加载 ---
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET")

# 初始化 K8s
try:
    config.load_kube_config()
except:
    config.load_incluster_config()
k8s_v1 = client.CoreV1Api()

# --- 飞书通讯模块 ---
def get_tenant_access_token():
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    try:
        r = requests.post(url, json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}, timeout=5)
        token = r.json().get("tenant_access_token")
        if not token: logger.error(f"获取飞书 Token 失败: {r.text}")
        return token
    except Exception as e:
        logger.error(f"飞书 Auth 异常: {e}")
        return None

def send_feishu_msg(open_id, text):
    token = get_tenant_access_token()
    if not token: return
    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"receive_id": open_id, "msg_type": "text", "content": json.dumps({"text": text})}
    
    logger.info(f"📤 发送回复至飞书: {text[:50]}...")
    requests.post(url, headers=headers, json=payload, timeout=5)

# --- 通用工具函数 ---
def k8s_list_pods(namespace="default"):
    logger.info(f"⚙️ 执行 K8s 工具: list_pods, 命名空间: {namespace}")
    try:
        pods = k8s_v1.list_namespaced_pod(namespace)
        data = [{"name": p.metadata.name, "status": p.status.phase} for p in pods.items]
        logger.info(f"✅ K8s 返回数据条数: {len(data)}")
        return json.dumps(data)
    except Exception as e:
        logger.error(f"❌ K8s 执行报错: {e}")
        return str(e)

TOOLS_MAP = {"k8s_list_pods": k8s_list_pods}

# --- AI 核心决策流 ---
def process_ai_logic(open_id, user_query):
    logger.info(f"🧠 [开始处理] 用户提问: {user_query}")
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    
    messages = [
        {"role": "system", "content": "你是一个名为 OpsAgentAI 的 K8s 专家。"},
        {"role": "user", "content": user_query}
    ]
    tools = [{
        "type": "function",
        "function": {
            "name": "k8s_list_pods",
            "description": "获取指定命名空间的 Pod 状态列表",
            "parameters": {
                "type": "object",
                "properties": {"namespace": {"type": "string", "default": "default"}}
            }
        }
    }]

    try:
        # 第一轮：AI 决策
        logger.info("📡 发起 DeepSeek 第一次请求 (决策)...")
        resp = requests.post("https://api.deepseek.com/v1/chat/completions", 
                             headers=headers, 
                             json={"model": "deepseek-chat", "messages": messages, "tools": tools}, timeout=30).json()
        
        msg = resp['choices'][0]['message']
        
        # 记录 AI 的出参决策
        if msg.get("tool_calls"):
            tool_call = msg["tool_calls"][0]
            func_name = tool_call["function"]["name"]
            args = tool_call["function"]["arguments"]
            logger.info(f"🤖 AI 决定调用工具: {func_name} | 参数: {args}")
            
            # 执行工具
            observation = TOOLS_MAP[func_name](**json.loads(args))
            
            # 第二轮：总结
            messages.append(msg)
            messages.append({"role": "tool", "tool_call_id": tool_call["id"], "name": func_name, "content": observation})
            
            logger.info("📡 发起 DeepSeek 第二次请求 (总结)...")
            final_resp = requests.post("https://api.deepseek.com/v1/chat/completions", 
                                       headers=headers, 
                                       json={"model": "deepseek-chat", "messages": messages}, timeout=30).json()
            answer = final_resp['choices'][0]['message']['content']
        else:
            logger.info("🤖 AI 认为无需调用工具，直接回复。")
            answer = msg["content"]
            
        send_feishu_msg(open_id, answer)
    except Exception as e:
        logger.error(f"💥 AI 逻辑处理崩溃: {e}", exc_info=True)
        send_feishu_msg(open_id, f"⚠️ 系统处理出错: {str(e)}")

# --- Flask Webhook ---
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    # 打印原始入参（脱敏掉 token 比较好，但调试阶段可以全开）
    # logger.debug(f"📩 收到 Webhook 原始入参: {json.dumps(data)}")

    if data.get("type") == "url_verification":
        return jsonify({"challenge": data.get("challenge")})
    
    if "event" in data and "message" in data["event"]:
        event = data["event"]
        open_id = event["sender"]["sender_id"]["open_id"]
        
        try:
            content_json = json.loads(event["message"]["content"])
            user_text = content_json.get("text", "")
            logger.info(f"💬 收到来自 {open_id} 的消息: {user_text}")
            
            thread = threading.Thread(target=process_ai_logic, args=(open_id, user_text))
            thread.start()
        except Exception as e:
            logger.error(f"解析消息内容失败: {e}")

    return jsonify({"code": 0})

if __name__ == "__main__":
    logger.info("🚀 OpsAgentAI 服务启动，监听 5000 端口...")
    app.run(host='0.0.0.0', port=5000)