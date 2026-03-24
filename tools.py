# tools.py
import json
import logging
from kubernetes import client, config

logger = logging.getLogger(__name__)

# 加载 K8s 配置
try:
    config.load_kube_config()
except:
    config.load_incluster_config()

v1 = client.CoreV1Api()
apps_v1 = client.AppsV1Api()

def k8s_universal_getter(resource_type, name=None, namespace="default"):
    """通用查询工具：支持 pods, nodes, services, deployments"""
    try:
        if resource_type == "pods":
            if name: 
                pod = v1.read_namespaced_pod(name, namespace)
                # 仅提取关键状态信息，避免 YAML 过大撑爆内存或 Token
                return json.dumps({
                    "name": pod.metadata.name,
                    "status": pod.status.phase,
                    "restarts": pod.status.container_statuses[0].restart_count if pod.status.container_statuses else 0,
                    "conditions": [c.type for c in pod.status.conditions if c.status == "True"]
                })
            return json.dumps([{"name": p.metadata.name, "status": p.status.phase} for p in v1.list_namespaced_pod(namespace).items])
        
        elif resource_type == "nodes":
            nodes = v1.list_node().items
            return json.dumps([{"name": n.metadata.name, "status": n.status.conditions[-1].type} for n in nodes])
        
        elif resource_type == "deployments":
            deps = apps_v1.list_namespaced_deployment(namespace).items
            return json.dumps([{"name": d.metadata.name, "replicas": f"{d.status.ready_replicas}/{d.spec.replicas}"} for d in deps])
            
        return f"暂不支持资源类型: {resource_type}"
    except Exception as e:
        logger.error(f"K8s Getter Error: {e}")
        return f"查询失败: {str(e)}"

def get_pod_logs(pod_name, namespace="default", lines=30):
    """获取指定 Pod 的日志"""
    try:
        return v1.read_namespaced_pod_log(name=pod_name, namespace=namespace, tail_lines=lines)
    except Exception as e:
        return f"日志获取失败: {str(e)}"