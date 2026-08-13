from __future__ import annotations

"""Generate schema-faithful, synthetic ChangeOrder JSON documents.

The outer paths and record cardinalities match the confirmed ChangeOrder v2
adapter contract. All identifiers, addresses, metrics and outcomes are invented
for an offline demo and must never be treated as production instructions.
"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "sample_data" / "realistic_change_orders"


def _task(
    ticket_id: str,
    index: int,
    item: dict[str, Any],
    *,
    region: str,
) -> dict[str, Any]:
    return {
        "task_id": f"{ticket_id}-TASK-{index:02d}",
        "task_name": item["name"],
        "task_type": item.get("task_type", "CHANGE"),
        "sequence": index,
        "region": region,
        "availability_zone": item.get("az"),
        "target_resource": item["resource"],
        "tool_name": item.get("tool", "cloud_network.apply_plan"),
        "operation": item["operation"],
        "parameters": {
            "synthetic_only": True,
            "desired_state": item["expected"],
            "change_ticket": ticket_id,
        },
        "timeout_seconds": item.get("timeout", 300),
        "expected_result": item["expected"],
        "owner": item.get("owner", "network-operator-a"),
    }


def _procedure_step(
    ticket_id: str,
    group: str,
    index: int,
    item: dict[str, Any],
    *,
    previous_step_id: str | None,
) -> dict[str, Any]:
    step_id = f"{ticket_id}-{group}-{index:02d}"
    operation = item.get("operation", group)
    resource = item.get("resource", "change-order")
    expected = item.get("expected", "检查结果符合变更门槛")
    return {
        "step_id": step_id,
        "step_name": item["name"],
        "step_description": item.get("description", item["name"]),
        "step_type": group,
        "sequence": index,
        "executor_role": item.get("owner", "network-operator-a"),
        "estimated_minutes": item.get("minutes", 5),
        "risk_level": item.get("risk", "MEDIUM"),
        "manual_confirmation_required": item.get("confirm", group == "IMPLEMENT"),
        "timeout_seconds": item.get("timeout", 300),
        "success_criteria": expected,
        "failure_action": item.get(
            "failure_action",
            "停止后续步骤并依据已审批计划进入回退判断",
        ),
        "tool_name": item.get("tool", "cloud_network.apply_plan"),
        "target_resources": [resource],
        "input_parameters": {
            "synthetic_only": True,
            "operation": operation,
            "desired_state": expected,
        },
        "preconditions": item.get(
            "preconditions",
            ["当前环境快照与方案生成时一致", "前序依赖步骤已经成功"],
        ),
        "commands": [
            f"SIMULATED_ONLY {item.get('tool', 'cloud_network.apply_plan')} "
            f"{operation} {resource}"
        ],
        "validation_rules": item.get(
            "validation_rules",
            [expected, "状态哈希和操作日志均已记录"],
        ),
        "evidence_requirements": item.get(
            "evidence_requirements",
            ["工具返回码", "变更前后快照", "监控采样记录"],
        ),
        "dependencies": [previous_step_id] if previous_step_id else [],
    }


def _steps(ticket_id: str, group: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    previous: str | None = None
    for index, item in enumerate(items, start=1):
        step = _procedure_step(
            ticket_id,
            group,
            index,
            item,
            previous_step_id=previous,
        )
        result.append(step)
        previous = str(step["step_id"])
    return result


def _group_tasks(tasks: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    first = (len(tasks) + 2) // 3
    second = (len(tasks) - first + 1) // 2
    return {
        "preparation_and_canary": deepcopy(tasks[:first]),
        "primary_change_wave": deepcopy(tasks[first : first + second]),
        "verification_and_closeout": deepcopy(tasks[first + second :]),
    }


def _state_hash(ticket_id: str, suffix: str) -> str:
    return hashlib.sha256(f"SYNTHETIC::{ticket_id}::{suffix}".encode()).hexdigest()


def _execution_result(spec: dict[str, Any]) -> dict[str, Any]:
    ticket_id = spec["ticket_id"]
    implementation_count = len(spec["implementation"])
    rolled_back = bool(spec["rollback_triggered"])
    before_hash = _state_hash(ticket_id, "before")
    after_hash = before_hash if rolled_back else _state_hash(ticket_id, "after")
    return {
        "execution_status": spec["execution_status"],
        "started_at": spec["window_start"],
        "ended_at": spec["window_end"],
        "operator": "network-operator-a",
        "approver": "change-manager-a",
        "total_steps": implementation_count,
        "succeeded_steps": spec.get("succeeded_steps", implementation_count),
        "failed_steps": spec.get("failed_steps", 0),
        "rollback_triggered": rolled_back,
        "rollback_status": "SUCCEEDED" if rolled_back else "NOT_REQUIRED",
        "pre_state_hash": before_hash,
        "post_state_hash": after_hash,
        "summary": spec["execution_summary"],
        "incident_id": spec.get("incident_id"),
        "result_items": spec["result_items"],
    }


def _build_change_order(spec: dict[str, Any]) -> dict[str, Any]:
    tasks = [
        _task(spec["ticket_id"], index, item, region=spec["region"])
        for index, item in enumerate(spec["implementation"], start=1)
    ]
    return {
        "code": 0,
        "provider_code": "SYNTHETIC_OK",
        "msg": "synthetic demo change order",
        "data": {
            "synthetic_demo_data": True,
            "data_classification": "SYNTHETIC_DEIDENTIFIED_DEMO",
            "ticket_id": spec["ticket_id"],
            "title": f"[合成演示] {spec['title']}",
            "original_system": "synthetic-internal-change-platform",
            "create_time": spec["create_time"],
            "cloud_service": spec["cloud_service"],
            "service": spec["service"],
            "micro_service": spec["micro_service"],
            "affected_service": spec["affected_service"],
            "change_scene": spec["change_scene"],
            "change_notes": spec["change_notes"],
            "special_change_type": spec["special_change_type"],
            "change_guide": spec["change_guide"],
            "severity": spec["severity"],
            "change_level": spec["change_level"],
            "customer_sensed": spec["customer_sensed"],
            "affected_customer": spec["affected_customer"],
            "risk_level": spec["risk_level"],
            "impact_risk_level": spec["impact_risk_level"],
            "region": spec["region"],
            "expected_start_time": spec["window_start"],
            "expected_end_time": spec["window_end"],
            "expected_total_time": spec["expected_total_time"],
            "executors": ["network-operator-a", "network-operator-b"],
            "cooperators": ["application-sre-a", "monitoring-oncall-a"],
            "reviewers": ["change-manager-a", "service-owner-a"],
            "approval_status": "APPROVED_HISTORICAL_SYNTHETIC",
            "high_risk_check": "PASSED_SYNTHETIC",
            "authorization_reference": "AUTH-SYNTHETIC-ONLY",
            "notification_status": "SIMULATED_COMPLETED",
            "action_list": tasks,
            "change_tool_relate_action": _group_tasks(tasks),
            "sop_change_step": {
                "check_before_change": _steps(
                    spec["ticket_id"], "PRECHECK", spec["precheck"]
                ),
                "change_implement": _steps(
                    spec["ticket_id"], "IMPLEMENT", spec["implementation"]
                ),
                "change_verified": _steps(
                    spec["ticket_id"], "VALIDATE", spec["validation"]
                ),
                "change_rollback": _steps(
                    spec["ticket_id"], "ROLLBACK", spec["rollback"]
                ),
            },
            "change_plan": [{"result": _execution_result(spec)}],
        },
    }


def _item(
    name: str,
    resource: str,
    operation: str,
    expected: str,
    *,
    az: str | None = None,
    description: str | None = None,
    task_type: str = "CHANGE",
    tool: str = "cloud_network.apply_plan",
    risk: str = "MEDIUM",
    minutes: int = 5,
) -> dict[str, Any]:
    return {
        "name": name,
        "resource": resource,
        "operation": operation,
        "expected": expected,
        "az": az,
        "description": description or name,
        "task_type": task_type,
        "tool": tool,
        "risk": risk,
        "minutes": minutes,
    }


def _checks(items: list[tuple[str, str, str]]) -> list[dict[str, Any]]:
    return [
        _item(
            name,
            resource,
            "INSPECT",
            expected,
            task_type="CHECK",
            tool="cloud_network.inspect",
            risk="LOW",
            minutes=3,
        )
        for name, resource, expected in items
    ]


def _validations(items: list[tuple[str, str, str]]) -> list[dict[str, Any]]:
    return [
        _item(
            name,
            resource,
            "VALIDATE",
            expected,
            task_type="VALIDATE",
            tool="cloud_network.validate",
            risk="LOW",
            minutes=4,
        )
        for name, resource, expected in items
    ]


def _rollbacks(items: list[tuple[str, str, str]]) -> list[dict[str, Any]]:
    return [
        _item(
            name,
            resource,
            "RESTORE_PREVIOUS_STATE",
            expected,
            task_type="ROLLBACK",
            risk="HIGH",
            minutes=5,
        )
        for name, resource, expected in items
    ]


def case_specs() -> list[dict[str, Any]]:
    common = {
        "create_time": "2026-08-01T09:30:00+08:00",
        "cloud_service": "Cloud Network",
        "service": "production-network-foundation",
        "micro_service": "network-control-plane",
        "severity": "S2",
        "change_level": "HIGH",
        "customer_sensed": "POSSIBLE_TRANSIENT_JITTER",
        "affected_customer": "synthetic-internal-services",
        "risk_level": "HIGH",
        "impact_risk_level": "MEDIUM",
        "special_change_type": "PLANNED_HIGH_RISK_SYNTHETIC",
        "change_guide": "分批执行、每批验证、异常立即停止并按逆序回退",
    }

    route_impl = [
        _item("冻结 AZ-A 路由表并记录版本", "rtb-prod-app-a", "LOCK_AND_SNAPSHOT", "已取得独占变更锁和基线哈希", az="az-a", risk="LOW"),
        _item("切换 AZ-A 数据库路由下一跳", "rtb-prod-app-a:172.20.32.0/20", "MODIFY_NEXT_HOP", "下一跳为 dc-standby 且路由状态 ACTIVE", az="az-a", risk="HIGH"),
        _item("放行 AZ-A 10% 合成探测流量", "probe-order-pay-a", "ENABLE_CANARY", "10% 探测连续两个周期达标", az="az-a"),
        _item("冻结 AZ-B 路由表并记录版本", "rtb-prod-app-b", "LOCK_AND_SNAPSHOT", "已取得独占变更锁和基线哈希", az="az-b", risk="LOW"),
        _item("切换 AZ-B 数据库路由下一跳", "rtb-prod-app-b:172.20.32.0/20", "MODIFY_NEXT_HOP", "下一跳为 dc-standby 且路由状态 ACTIVE", az="az-b", risk="HIGH"),
        _item("恢复全量业务探测并解除冻结", "vpc-prod-core", "ENABLE_FULL_TRAFFIC_AND_UNLOCK", "双 AZ 探测达标且路由表已解锁"),
    ]
    nat_impl = [
        _item("校验绿色 NAT 网关配置版本", "nat-prod-green", "VERIFY_TARGET_CONFIG", "绿色 NAT 配置版本与审批包一致", risk="LOW"),
        _item("关联绿色出口 EIP", "eip-prod-egress-green", "ASSOCIATE_EIP", "EIP 已绑定 nat-prod-green"),
        _item("同步生产 SNAT 规则", "nat-prod-green:snat-rules", "UPSERT_SNAT_RULES", "8 条 SNAT 规则与旧网关一致"),
        _item("切换 AZ-A 默认出口路由", "rtb-prod-egress-a:0.0.0.0/0", "MODIFY_NEXT_HOP", "下一跳为 nat-prod-green", az="az-a", risk="HIGH"),
        _item("验证 AZ-A 出口与固定源地址", "egress-probe-a", "VALIDATE_EGRESS", "HTTPS 成功率不低于 99.9% 且源 EIP 正确", az="az-a"),
        _item("切换 AZ-B 默认出口路由", "rtb-prod-egress-b:0.0.0.0/0", "MODIFY_NEXT_HOP", "下一跳为 nat-prod-green", az="az-b", risk="HIGH"),
        _item("验证 AZ-B 出口与固定源地址", "egress-probe-b", "VALIDATE_EGRESS", "HTTPS 成功率不低于 99.9% 且源 EIP 正确", az="az-b"),
        _item("将旧 NAT 网关置为热备", "nat-prod-blue", "SET_STANDBY", "旧 NAT 保留配置且不承载新连接", risk="LOW"),
    ]
    vpn_impl = [
        _item("校验 KMS 预共享密钥引用", "kms-ref:vpn-partner-2026q3", "VERIFY_SECRET_REFERENCE", "密钥引用有效且正文未出现在工单中", risk="LOW"),
        _item("更新备用隧道 IKE 参数", "vpn-partner-tunnel-b", "UPDATE_IKE_POLICY", "备用隧道使用新策略版本", risk="HIGH"),
        _item("更新备用隧道 IPsec 参数", "vpn-partner-tunnel-b", "UPDATE_IPSEC_POLICY", "备用隧道协商成功", risk="HIGH"),
        _item("建立备用隧道 BGP 邻居", "bgp-partner-b", "ENABLE_BGP_PEER", "BGP 状态 ESTABLISHED 且前缀数为 24"),
        _item("提高备用隧道路由优先级", "vpn-route-policy-b", "SET_ROUTE_PREFERENCE", "合作方网段有效下一跳为 tunnel-b", risk="HIGH"),
        _item("执行备用隧道业务采样", "partner-probe-b", "RUN_CANARY", "TCP 成功率不低于 99.5% 且丢包不高于 1%"),
        _item("更新主隧道 IKE 参数", "vpn-partner-tunnel-a", "UPDATE_IKE_POLICY", "主隧道使用新策略版本", risk="HIGH"),
        _item("更新主隧道 IPsec 参数", "vpn-partner-tunnel-a", "UPDATE_IPSEC_POLICY", "主隧道协商成功", risk="HIGH"),
        _item("恢复主隧道 BGP 邻居", "bgp-partner-a", "ENABLE_BGP_PEER", "BGP 状态 ESTABLISHED 且前缀数为 24"),
        _item("恢复双隧道路由策略", "vpn-route-policy", "RESTORE_ECMP_POLICY", "双隧道 ECMP 生效且路径对称", risk="HIGH"),
    ]
    sg_impl = [
        _item("创建订单服务目标安全组版本", "sg-order-v3", "CREATE_RULESET_VERSION", "目标版本 v3 已创建但未绑定", risk="LOW"),
        _item("创建支付服务目标安全组版本", "sg-payment-v3", "CREATE_RULESET_VERSION", "目标版本 v3 已创建但未绑定", risk="LOW"),
        _item("绑定订单 AZ-A 只读影子策略", "eni-order-a", "ATTACH_SHADOW_RULESET", "影子命中日志无新增拒绝", az="az-a"),
        _item("绑定支付 AZ-A 只读影子策略", "eni-payment-a", "ATTACH_SHADOW_RULESET", "影子命中日志无新增拒绝", az="az-a"),
        _item("启用订单 AZ-A 强制策略", "eni-order-a", "ENFORCE_RULESET", "订单健康检查和数据库访问正常", az="az-a", risk="HIGH"),
        _item("启用支付 AZ-A 强制策略", "eni-payment-a", "ENFORCE_RULESET", "支付健康检查和数据库访问正常", az="az-a", risk="HIGH"),
        _item("绑定订单 AZ-B 只读影子策略", "eni-order-b", "ATTACH_SHADOW_RULESET", "影子命中日志无新增拒绝", az="az-b"),
        _item("绑定支付 AZ-B 只读影子策略", "eni-payment-b", "ATTACH_SHADOW_RULESET", "影子命中日志无新增拒绝", az="az-b"),
        _item("启用订单 AZ-B 强制策略", "eni-order-b", "ENFORCE_RULESET", "订单健康检查和数据库访问正常", az="az-b", risk="HIGH"),
        _item("启用支付 AZ-B 强制策略", "eni-payment-b", "ENFORCE_RULESET", "支付健康检查和数据库访问正常", az="az-b", risk="HIGH"),
        _item("移除旧版宽泛规则", "sg-legacy-shared", "DISABLE_LEGACY_RULES", "0.0.0.0/0 宽泛入站规则已禁用", risk="HIGH"),
        _item("固化规则版本并开启持续审计", "sg-policy-audit", "COMMIT_AND_MONITOR", "策略版本已固化且审计告警启用", risk="LOW"),
    ]
    tgw_impl = [
        _item("创建目标路由域策略版本", "tgw-domain-prod-v2", "CREATE_POLICY_VERSION", "策略版本 v2 已创建未发布", risk="LOW"),
        _item("迁移运维 Spoke-A 关联", "spoke-ops-a", "MOVE_ASSOCIATION", "关联至 tgw-domain-prod-v2", az="az-a", risk="HIGH"),
        _item("迁移运维 Spoke-B 关联", "spoke-ops-b", "MOVE_ASSOCIATION", "关联至 tgw-domain-prod-v2", az="az-b", risk="HIGH"),
        _item("迁移共享服务 Spoke-A 关联", "spoke-shared-a", "MOVE_ASSOCIATION", "关联至 tgw-domain-prod-v2", az="az-a", risk="HIGH"),
        _item("迁移共享服务 Spoke-B 关联", "spoke-shared-b", "MOVE_ASSOCIATION", "关联至 tgw-domain-prod-v2", az="az-b", risk="HIGH"),
        _item("迁移订单 Spoke-A 关联", "spoke-order-a", "MOVE_ASSOCIATION", "关联至 tgw-domain-prod-v2", az="az-a", risk="HIGH"),
        _item("迁移订单 Spoke-B 关联", "spoke-order-b", "MOVE_ASSOCIATION", "关联至 tgw-domain-prod-v2", az="az-b", risk="HIGH"),
        _item("迁移支付 Spoke-A 关联", "spoke-payment-a", "MOVE_ASSOCIATION", "关联至 tgw-domain-prod-v2", az="az-a", risk="HIGH"),
        _item("迁移支付 Spoke-B 关联", "spoke-payment-b", "MOVE_ASSOCIATION", "关联至 tgw-domain-prod-v2", az="az-b", risk="HIGH"),
        _item("迁移数据 Spoke-A 关联", "spoke-data-a", "MOVE_ASSOCIATION", "关联至 tgw-domain-prod-v2", az="az-a", risk="HIGH"),
        _item("迁移数据 Spoke-B 关联", "spoke-data-b", "MOVE_ASSOCIATION", "关联至 tgw-domain-prod-v2", az="az-b", risk="HIGH"),
        _item("发布目标路由域传播策略", "tgw-domain-prod-v2", "PUBLISH_PROPAGATION", "所有期望前缀完成传播", risk="HIGH"),
        _item("降低旧路由域优先级", "tgw-domain-prod-v1", "DEPRIORITIZE_POLICY", "旧路由域只保留回退路径", risk="HIGH"),
        _item("固化关联并解除变更锁", "tgw-prod-core", "COMMIT_AND_UNLOCK", "关联快照已固化且变更锁释放", risk="LOW"),
    ]

    return [
        common
        | {
            "ticket_id": "CHG-SYN-NET-001",
            "title": "生产 VPC 专线路由双 AZ 主备切换",
            "region": "cn-north-4",
            "affected_service": "order-service,payment-service",
            "change_scene": "DIRECT_CONNECT_ROUTE_FAILOVER",
            "change_notes": "主专线连续抖动，按 AZ 灰度切换至健康备用专线。",
            "window_start": "2026-08-02T01:00:00+08:00",
            "window_end": "2026-08-02T02:00:00+08:00",
            "expected_total_time": 60,
            "precheck": _checks([
                ("核对双 AZ 路由资源", "vpc-prod-core", "两张路由表和目标 CIDR 均存在"),
                ("核对当前主链路下一跳", "dc-primary", "当前下一跳与基线快照一致"),
                ("检查备用专线健康度", "dc-standby", "BFD 正常且连续 10 分钟无丢包"),
                ("检查备用专线容量", "dc-standby", "峰值利用率低于 60%"),
                ("检查变更窗口和业务冻结", "CHG-SYN-NET-001", "窗口有效且无冲突变更"),
            ]),
            "implementation": route_impl,
            "validation": _validations([
                ("验证 AZ-A 有效下一跳", "rtb-prod-app-a", "172.20.32.0/20 有效下一跳为 dc-standby"),
                ("验证 AZ-A 业务指标", "probe-order-pay-a", "TCP 443/5432 成功率不低于 99.5%"),
                ("验证 AZ-B 有效下一跳", "rtb-prod-app-b", "172.20.32.0/20 有效下一跳为 dc-standby"),
                ("验证 AZ-B 业务指标", "probe-order-pay-b", "丢包不高于 1% 且 P95 时延不高于 30ms"),
                ("核对全局状态哈希", "vpc-prod-core", "最终快照与目标计划哈希一致"),
            ]),
            "rollback": _rollbacks([
                ("恢复 AZ-B 原下一跳", "rtb-prod-app-b", "下一跳恢复 dc-primary"),
                ("验证 AZ-B 回退状态", "probe-order-pay-b", "业务指标恢复基线"),
                ("恢复 AZ-A 原下一跳", "rtb-prod-app-a", "下一跳恢复 dc-primary"),
                ("验证整体回退哈希", "vpc-prod-core", "状态哈希等于变更前哈希"),
            ]),
            "execution_status": "SUCCEEDED",
            "rollback_triggered": False,
            "execution_summary": "双 AZ 依次切换成功，业务指标连续三个采样周期达标。",
            "result_items": ["AZ-A route switched", "AZ-B route switched", "No rollback required"],
        },
        common
        | {
            "ticket_id": "CHG-SYN-NET-002",
            "title": "生产 NAT 网关 EIP 与 SNAT 出口蓝绿迁移",
            "region": "cn-east-3",
            "affected_service": "public-api,batch-worker",
            "change_scene": "NAT_EGRESS_BLUE_GREEN_MIGRATION",
            "change_notes": "将双 AZ 公网出口从蓝色 NAT 迁移至绿色 NAT，保留旧网关热备。",
            "window_start": "2026-08-03T00:30:00+08:00",
            "window_end": "2026-08-03T02:00:00+08:00",
            "expected_total_time": 90,
            "precheck": _checks([
                ("核对绿色 NAT 资源", "nat-prod-green", "网关、EIP 和子网状态 AVAILABLE"),
                ("对账 SNAT 规则", "nat-prod-blue", "源规则共 8 条且无重复 CIDR"),
                ("检查 EIP 信誉与带宽", "eip-prod-egress-green", "无封禁且带宽余量超过 50%"),
                ("检查外部白名单", "partner-allowlist", "绿色 EIP 已加入合作方白名单"),
                ("检查连接排空条件", "nat-prod-blue", "长连接数量低于门槛"),
                ("检查变更互斥锁", "vpc-prod-egress", "没有并发路由或防火墙变更"),
            ]),
            "implementation": nat_impl,
            "validation": _validations([
                ("验证双 AZ 默认路由", "vpc-prod-egress", "两张默认路由均指向 nat-prod-green"),
                ("验证公网 HTTPS", "public-api-probe", "成功率不低于 99.9%"),
                ("验证 DNS 与 NTP 出口", "infra-egress-probe", "UDP/TCP 探测均成功"),
                ("验证源 EIP", "egress-observer", "所有新连接源地址为绿色 EIP"),
                ("验证 SNAT 端口容量", "nat-prod-green", "端口利用率低于 50%"),
                ("验证旧 NAT 无新增连接", "nat-prod-blue", "连续两周期无新增生产连接"),
            ]),
            "rollback": _rollbacks([
                ("恢复 AZ-B 默认路由", "rtb-prod-egress-b", "下一跳恢复 nat-prod-blue"),
                ("恢复 AZ-A 默认路由", "rtb-prod-egress-a", "下一跳恢复 nat-prod-blue"),
                ("恢复旧 NAT 承载状态", "nat-prod-blue", "旧 NAT 状态 ACTIVE"),
                ("停止绿色 NAT 新连接", "nat-prod-green", "绿色 NAT 不再接收新连接"),
                ("解除绿色 EIP 绑定", "eip-prod-egress-green", "EIP 回到 RESERVED"),
                ("核对回退后出口", "vpc-prod-egress", "源 EIP 和状态哈希恢复基线"),
            ]),
            "execution_status": "SUCCEEDED",
            "rollback_triggered": False,
            "execution_summary": "八个实施任务完成，绿色 NAT 承载稳定，旧 NAT 保持热备。",
            "result_items": ["8 SNAT rules synchronized", "Both AZ routes switched", "Old NAT kept as standby"],
        },
        common
        | {
            "ticket_id": "CHG-SYN-NET-003",
            "title": "合作方双隧道 IPsec VPN 密钥与 BGP 路由轮换",
            "region": "cn-south-1",
            "affected_service": "partner-order-ingress",
            "change_scene": "IPSEC_VPN_KEY_AND_BGP_CUTOVER",
            "change_notes": "使用 KMS 引用轮换双隧道策略；备用隧道采样丢包超阈值后按计划回退。",
            "window_start": "2026-08-04T02:00:00+08:00",
            "window_end": "2026-08-04T04:00:00+08:00",
            "expected_total_time": 120,
            "precheck": _checks([
                ("核对 KMS 密钥引用", "kms-ref:vpn-partner-2026q3", "密钥引用可用且无明文泄漏"),
                ("核对双隧道当前策略", "vpn-partner", "当前 IKE/IPsec 版本与快照一致"),
                ("检查合作方维护窗口", "partner-change-window", "双方窗口和联系人已确认"),
                ("检查 BGP 前缀基线", "bgp-partner", "双邻居前缀数均为 24"),
                ("检查隧道容量", "vpn-partner", "任一单隧道可承载当前峰值"),
                ("检查路由对称性", "vpn-flow-logs", "基线不存在非对称路径"),
                ("检查自动回退条件", "CHG-SYN-NET-003", "连续两个采样周期丢包超过 1% 即回退"),
            ]),
            "implementation": vpn_impl,
            "validation": _validations([
                ("验证备用隧道协商", "vpn-partner-tunnel-b", "IKE SA 和 IPsec SA 均为 UP"),
                ("验证备用 BGP 邻居", "bgp-partner-b", "状态 ESTABLISHED 且前缀数 24"),
                ("验证合作方 API", "partner-api-probe", "HTTPS 成功率不低于 99.5%"),
                ("验证链路丢包", "vpn-packet-loss", "连续两个周期丢包不高于 1%"),
                ("验证链路时延", "vpn-latency", "P95 时延不高于 80ms"),
                ("验证双隧道对称性", "vpn-flow-logs", "回程和去程隧道策略一致"),
                ("验证最终路由哈希", "vpn-route-policy", "哈希与审批目标一致"),
            ]),
            "rollback": _rollbacks([
                ("恢复双隧道路由优先级", "vpn-route-policy", "有效下一跳恢复主隧道"),
                ("关闭备用 BGP 新策略", "bgp-partner-b", "邻居恢复旧策略版本"),
                ("恢复备用 IPsec 策略", "vpn-partner-tunnel-b", "IPsec 使用旧版本"),
                ("恢复备用 IKE 策略", "vpn-partner-tunnel-b", "IKE 使用旧版本"),
                ("恢复主 BGP 策略", "bgp-partner-a", "主邻居 ESTABLISHED"),
                ("恢复主 IPsec 策略", "vpn-partner-tunnel-a", "主隧道 IPsec 使用旧版本"),
                ("恢复主 IKE 策略", "vpn-partner-tunnel-a", "主隧道 IKE 使用旧版本"),
                ("核对回退状态哈希", "vpn-partner", "最终哈希等于变更前哈希"),
            ]),
            "execution_status": "ROLLED_BACK",
            "rollback_triggered": True,
            "succeeded_steps": 6,
            "failed_steps": 1,
            "incident_id": "INC-SYN-VPN-003",
            "execution_summary": "备用隧道第二个采样周期丢包 2.4%，触发逆序回退并恢复基线哈希。",
            "result_items": ["Canary packet loss 2.4%", "Rollback triggered", "Pre-state hash restored"],
        },
        common
        | {
            "ticket_id": "CHG-SYN-NET-004",
            "title": "订单与支付服务安全组微隔离规则收敛",
            "region": "cn-north-4",
            "affected_service": "order-service,payment-service",
            "change_scene": "SECURITY_GROUP_MICRO_SEGMENTATION",
            "change_notes": "按双 AZ 影子策略、强制策略、旧规则下线三阶段收敛东西向访问。",
            "window_start": "2026-08-05T00:00:00+08:00",
            "window_end": "2026-08-05T02:00:00+08:00",
            "expected_total_time": 120,
            "precheck": _checks([
                ("对账 CMDB 服务关系", "cmdb-order-payment", "生产 ENI 和依赖关系完整"),
                ("检查现有流日志", "vpc-flow-logs", "最近七天无未登记必要流量"),
                ("检查规则配额", "sg-order-v3,sg-payment-v3", "规则配额余量超过 30%"),
                ("检查紧急管理通道", "sg-breakglass", "受控应急规则可用"),
                ("检查业务发布冻结", "order-payment-release", "应用侧无并发发布"),
                ("校验目标规则哈希", "sg-policy-package", "规则哈希与审批包一致"),
            ]),
            "implementation": sg_impl,
            "validation": _validations([
                ("验证订单健康检查", "order-service", "双 AZ 健康实例比例 100%"),
                ("验证支付健康检查", "payment-service", "双 AZ 健康实例比例 100%"),
                ("验证订单到数据库", "order-db-probe", "TCP 5432 成功率不低于 99.9%"),
                ("验证支付到数据库", "payment-db-probe", "TCP 5432 成功率不低于 99.9%"),
                ("验证服务间 mTLS", "order-payment-mtls", "握手成功率不低于 99.9%"),
                ("检查拒绝日志", "vpc-flow-logs", "无高频未知拒绝流"),
                ("检查宽泛规则", "sg-legacy-shared", "0.0.0.0/0 宽泛入站已清零"),
                ("核对规则版本哈希", "sg-policy-audit", "实际规则哈希与目标一致"),
            ]),
            "rollback": _rollbacks([
                (f"恢复实施步骤 {index:02d} 的前态", item["resource"], f"{item['resource']} 恢复原规则或绑定")
                for index, item in reversed(list(enumerate(sg_impl, start=1)))
            ]),
            "execution_status": "SUCCEEDED",
            "rollback_triggered": False,
            "execution_summary": "十二步分波次执行成功，宽泛规则下线后业务和拒绝日志均正常。",
            "result_items": ["12 implementation steps completed", "No unexpected denies", "Legacy broad rules disabled"],
        },
        common
        | {
            "ticket_id": "CHG-SYN-NET-005",
            "title": "Hub-Spoke 中转路由域十四步分波迁移",
            "region": "cn-east-3",
            "affected_service": "ops,shared,order,payment,data",
            "change_scene": "TRANSIT_HUB_ROUTE_DOMAIN_MIGRATION",
            "change_notes": "按运维、共享、应用、数据四波迁移；数据域验证失败后恢复原关联。",
            "window_start": "2026-08-06T00:00:00+08:00",
            "window_end": "2026-08-06T03:00:00+08:00",
            "expected_total_time": 180,
            "precheck": _checks([
                ("核对全部 Spoke 关联", "tgw-prod-core", "十个 Spoke 当前关联与快照一致"),
                ("检查目标路由域配额", "tgw-domain-prod-v2", "关联和传播配额余量超过 40%"),
                ("检查前缀冲突", "tgw-route-analyzer", "目标域不存在更具体冲突前缀"),
                ("检查双 AZ 附件健康", "tgw-attachments", "全部附件状态 AVAILABLE"),
                ("检查防火墙服务链", "ew-firewall-chain", "目标域传播包含安全检查路径"),
                ("检查数据域维护授权", "data-domain-approval", "数据域迁移已获双人确认"),
                ("检查监控采样", "tgw-flow-monitor", "基线采样和告警通道正常"),
                ("检查回退操作日志", "tgw-operation-journal", "全部关联具备幂等回退记录"),
            ]),
            "implementation": tgw_impl,
            "validation": _validations([
                ("验证运维域互通", "spoke-ops", "SSH 跳板探测成功率 100%"),
                ("验证共享服务域", "spoke-shared", "DNS、NTP 和制品库均可达"),
                ("验证订单双 AZ", "spoke-order", "健康检查成功率不低于 99.9%"),
                ("验证支付双 AZ", "spoke-payment", "健康检查成功率不低于 99.9%"),
                ("验证数据双 AZ", "spoke-data", "数据库连接成功率不低于 99.5%"),
                ("验证东西向防火墙", "ew-firewall-chain", "所有跨域流量经过服务链"),
                ("验证路由前缀数量", "tgw-domain-prod-v2", "传播前缀数量等于审批基线"),
                ("验证非对称路由", "tgw-flow-monitor", "未发现非对称会话"),
                ("验证 P95 时延", "tgw-latency", "跨域 P95 时延不高于 15ms"),
                ("核对全局状态哈希", "tgw-prod-core", "最终哈希与目标计划一致"),
            ]),
            "rollback": _rollbacks([
                (f"逆序恢复迁移项 {index:02d}", item["resource"], f"{item['resource']} 恢复原路由域关联")
                for index, item in reversed(list(enumerate(tgw_impl[1:13], start=1)))
            ]),
            "execution_status": "ROLLED_BACK",
            "rollback_triggered": True,
            "succeeded_steps": 10,
            "failed_steps": 1,
            "incident_id": "INC-SYN-TGW-005",
            "execution_summary": "数据域连接成功率降至 98.7%，停止后续步骤并逆序恢复十二项关联，状态哈希恢复。",
            "result_items": ["Data-domain success rate 98.7%", "12 reverse operations completed", "Pre-state hash restored"],
        },
    ]


def generate(output_dir: Path = DEFAULT_OUTPUT) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index, spec in enumerate(case_specs(), start=1):
        payload = _build_change_order(spec)
        slug = str(spec["change_scene"]).lower()
        path = output_dir / f"{index:02d}-{slug}.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        paths.append(path)
    return paths


if __name__ == "__main__":
    generated = generate()
    for item in generated:
        print(item)
