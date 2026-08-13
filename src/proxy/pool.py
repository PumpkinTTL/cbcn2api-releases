"""账号池路由：按 platform 返回对应的转发池实例。

架构：网关统一管理所有平台的账号（accounts 表 + platform 字段），
但转发池因上游业务不同各自独立（token_rotator 耦合 WorkBuddy 的额度结构/
11140 封号/阈值；Grok 用独立的 GrokPool）。通用账号管理方法
（set_account_status / set_priority_account / refresh_all / delete_* 等）
通过 get_pool(platform) 路由到对应池，做到「数据 + 管理统一，池各自实现」。

新增平台只需在这里挂一个分支 + 实现对齐的池接口。
"""
from src.proxy.token_rotator import token_rotator


def get_pool(platform: str):
    """返回 platform 对应的转发池。

    workbuddy / codebuddy → token_rotator（单例，WorkBuddy 业务）
    grok                   → grok_pool（Grok Build 转发）
    其它                   → 默认 token_rotator
    """
    if platform == "grok":
        from src.grok.provider import grok_pool
        return grok_pool
    return token_rotator
