from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from typing import Any

from knowledge_platform.schema import (
    CardStatus,
    ComparisonDecision,
    ComparisonResult,
    KnowledgeCardDraft,
)
from knowledge_platform.store import KnowledgeStore


@dataclass(frozen=True)
class ChangeCase:
    """A synthetic, internally consistent cloud-network change scenario."""

    case_id: str
    ticket_id: str
    label: str
    category: str
    description: str
    risk_level: str
    risk_score: int
    region: str
    vpc_id: str
    vpc_cidr: str
    destination: str
    route_type: str
    route_tables: tuple[dict[str, str], dict[str, str]]
    from_next_hop: str
    from_next_hop_type: str
    from_status: str
    from_capacity_percent: float
    to_next_hop: str
    to_next_hop_type: str
    to_status: str
    to_capacity_percent: float
    affected_services: tuple[str, ...]
    service_ports: tuple[int, ...]
    title: str
    summary: str
    knowledge_title: str
    knowledge_summary: str
    procedure_steps: tuple[str, ...]
    risks: tuple[str, ...]
    rollback_triggers: tuple[str, ...]
    communication_plan: tuple[str, ...]
    historical_outcome: str
    search_query: str

    def public_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["route_tables"] = [dict(item) for item in self.route_tables]
        payload["affected_services"] = list(self.affected_services)
        payload["service_ports"] = list(self.service_ports)
        payload["procedure_steps"] = list(self.procedure_steps)
        payload["risks"] = list(self.risks)
        payload["rollback_triggers"] = list(self.rollback_triggers)
        payload["communication_plan"] = list(self.communication_plan)
        return payload


CHANGE_CASES: tuple[ChangeCase, ...] = (
    ChangeCase(
        case_id="dc-route-failover",
        ticket_id="CHG-DEMO-ROUTE-001",
        label="专线路由主备切换",
        category="专线容灾",
        description="核心数据库专线路由从劣化主链路灰度切换到备用链路。",
        risk_level="高",
        risk_score=82,
        region="cn-north-4",
        vpc_id="vpc-prod-core",
        vpc_cidr="10.20.0.0/16",
        destination="172.20.32.0/20",
        route_type="direct_connect",
        route_tables=(
            {"id": "rtb-prod-app-a", "az": "az-a", "subnet": "subnet-prod-app-a"},
            {"id": "rtb-prod-app-b", "az": "az-b", "subnet": "subnet-prod-app-b"},
        ),
        from_next_hop="dc-primary",
        from_next_hop_type="direct_connect",
        from_status="DEGRADED",
        from_capacity_percent=72.0,
        to_next_hop="dc-standby",
        to_next_hop_type="direct_connect",
        to_status="UP",
        to_capacity_percent=35.0,
        affected_services=("order-api", "payment-api"),
        service_ports=(443, 5432),
        title="生产 VPC 核心数据库专线路由主备切换（合成演示）",
        summary="将 vpc-prod-core 双 AZ 路由中 172.20.32.0/20 的下一跳由 dc-primary 灰度切换至 dc-standby。",
        knowledge_title="历史案例：双 AZ 专线路由主备切换（合成）",
        knowledge_summary="先切 AZ-A 并观察业务指标，通过后再切 AZ-B；异常时按相反顺序回退。",
        procedure_steps=("保存环境与路由快照", "切换 AZ-A 并验证", "切换 AZ-B 并验证", "核对最终状态哈希"),
        risks=("备用专线不可用会导致核心数据库访问中断", "双 AZ 同时修改会扩大故障面"),
        rollback_triggers=("有效下一跳不符合计划", "连续两个采样周期越过健康阈值"),
        communication_plan=("变更前通知网络与订单、支付值守", "灰度后同步中间结果", "结束后发布最终结论"),
        historical_outcome="历史合成演练按 AZ-A、AZ-B 顺序完成，TCP 成功率 99.9%，未触发回退。",
        search_query="生产 VPC 专线 主备 路由 灰度 切换 回退 核心数据库",
    ),
    ChangeCase(
        case_id="nat-egress-bluegreen",
        ticket_id="CHG-DEMO-NAT-002",
        label="NAT 网关蓝绿切换",
        category="互联网出口",
        description="电商前台默认路由从容量接近上限的旧 NAT 迁移至绿色 NAT。",
        risk_level="高",
        risk_score=78,
        region="cn-east-3",
        vpc_id="vpc-prod-commerce",
        vpc_cidr="10.42.0.0/16",
        destination="0.0.0.0/0",
        route_type="nat_gateway",
        route_tables=(
            {"id": "rtb-prod-web-a", "az": "az-a", "subnet": "subnet-prod-web-a"},
            {"id": "rtb-prod-web-b", "az": "az-b", "subnet": "subnet-prod-web-b"},
        ),
        from_next_hop="nat-old",
        from_next_hop_type="nat_gateway",
        from_status="DEGRADED",
        from_capacity_percent=86.0,
        to_next_hop="nat-green",
        to_next_hop_type="nat_gateway",
        to_status="UP",
        to_capacity_percent=28.0,
        affected_services=("checkout-api", "catalog-api"),
        service_ports=(443, 5432),
        title="生产电商 VPC NAT 网关蓝绿切换（合成演示）",
        summary="将 vpc-prod-commerce 双 AZ 默认路由由 nat-old 灰度切换至 nat-green，缓解出口容量风险。",
        knowledge_title="历史案例：生产 NAT 网关蓝绿迁移（合成）",
        knowledge_summary="默认路由迁移必须逐 AZ 进行，并验证公网探测、支付回调及出口容量。",
        procedure_steps=("锁定旧 NAT 与弹性公网 IP 快照", "切换 AZ-A 默认路由", "验证公网与回调", "切换 AZ-B 并观察"),
        risks=("默认路由错误会影响全部公网访问", "SNAT 会话重建可能造成短时抖动"),
        rollback_triggers=("公网探测成功率低于阈值", "支付回调连续两个周期失败"),
        communication_plan=("通知电商、支付和安全值守", "灰度后确认第三方回调", "完成后同步出口容量"),
        historical_outcome="历史合成迁移完成后出口容量从 86% 降至 31%，未发现支付回调丢失。",
        search_query="生产 NAT 网关 蓝绿 默认路由 公网出口 支付回调 回退",
    ),
    ChangeCase(
        case_id="firewall-cluster-maintenance",
        ticket_id="CHG-DEMO-CFW-003",
        label="云防火墙集群维护切流",
        category="安全链路",
        description="共享服务流量从待维护防火墙主节点切到已预热备用节点。",
        risk_level="高",
        risk_score=88,
        region="cn-south-1",
        vpc_id="vpc-prod-shared",
        vpc_cidr="10.60.0.0/16",
        destination="10.80.0.0/16",
        route_type="cloud_firewall",
        route_tables=(
            {"id": "rtb-prod-sec-a", "az": "az-a", "subnet": "subnet-prod-sec-a"},
            {"id": "rtb-prod-sec-b", "az": "az-b", "subnet": "subnet-prod-sec-b"},
        ),
        from_next_hop="cfw-primary",
        from_next_hop_type="cloud_firewall",
        from_status="DEGRADED",
        from_capacity_percent=67.0,
        to_next_hop="cfw-standby",
        to_next_hop_type="cloud_firewall",
        to_status="UP",
        to_capacity_percent=41.0,
        affected_services=("iam-service", "audit-service"),
        service_ports=(443, 5432),
        title="生产共享 VPC 云防火墙维护切流（合成演示）",
        summary="在主防火墙维护前，将 10.80.0.0/16 的双 AZ 流量灰度切换至 cfw-standby。",
        knowledge_title="历史案例：云防火墙集群无损维护切流（合成）",
        knowledge_summary="切流前需完成策略一致性校验、会话预热和审计日志验证。",
        procedure_steps=("比对主备安全策略哈希", "预热备用节点", "灰度切换 AZ-A", "验证审计后扩展 AZ-B"),
        risks=("策略不一致可能导致误阻断", "长连接重建可能影响认证服务"),
        rollback_triggers=("安全策略哈希不一致", "认证或审计请求成功率越过阈值"),
        communication_plan=("通知安全、IAM 与审计值守", "灰度后核查拦截日志", "维护窗口结束后通报"),
        historical_outcome="历史合成维护中主备策略哈希一致，认证请求 P95 为 17 ms。",
        search_query="云防火墙 集群 维护 切流 策略一致性 会话预热 回退",
    ),
    ChangeCase(
        case_id="cross-region-dr-activation",
        ticket_id="CHG-DEMO-DR-004",
        label="跨区域灾备链路启用",
        category="区域容灾",
        description="数据服务路由从异常对等连接切换到已验证的灾备 VPN。",
        risk_level="极高",
        risk_score=93,
        region="cn-east-2",
        vpc_id="vpc-prod-data",
        vpc_cidr="10.72.0.0/16",
        destination="172.31.64.0/20",
        route_type="vpn_gateway",
        route_tables=(
            {"id": "rtb-prod-data-a", "az": "az-a", "subnet": "subnet-prod-data-a"},
            {"id": "rtb-prod-data-b", "az": "az-b", "subnet": "subnet-prod-data-b"},
        ),
        from_next_hop="peering-primary",
        from_next_hop_type="vpc_peering",
        from_status="DEGRADED",
        from_capacity_percent=74.0,
        to_next_hop="vpn-dr",
        to_next_hop_type="vpn_gateway",
        to_status="UP",
        to_capacity_percent=38.0,
        affected_services=("reporting-api", "ledger-reader"),
        service_ports=(443, 5432),
        title="生产数据 VPC 跨区域灾备链路启用（合成演示）",
        summary="将 172.31.64.0/20 的跨区域访问从 peering-primary 灰度切换至 vpn-dr。",
        knowledge_title="历史案例：跨区域灾备 VPN 启用（合成）",
        knowledge_summary="灾备链路启用需核对路由通告、加密隧道、容量和数据只读保护。",
        procedure_steps=("确认灾备处于只读保护", "核对 VPN 路由通告", "切换 AZ-A 并验证", "切换 AZ-B 并观察"),
        risks=("跨区域时延可能影响数据查询", "错误路由可能造成双写风险"),
        rollback_triggers=("灾备只读保护失效", "跨区域时延或丢包连续越过阈值"),
        communication_plan=("通知灾备指挥、数据和应用负责人", "灰度后确认只读状态", "结束后发布灾备状态"),
        historical_outcome="历史合成演练保持只读保护，跨区域 P95 时延 24 ms，路由按计划生效。",
        search_query="跨区域 灾备 VPN 路由 启用 只读保护 延迟 回退",
    ),
    ChangeCase(
        case_id="partner-extranet-migration",
        ticket_id="CHG-DEMO-B2B-005",
        label="合作方 VPN 外联迁移",
        category="B2B 外联",
        description="合作方接口网段从旧 VPN 隧道迁移至双运营商新隧道。",
        risk_level="高",
        risk_score=76,
        region="cn-north-9",
        vpc_id="vpc-prod-b2b",
        vpc_cidr="10.91.0.0/16",
        destination="192.168.120.0/24",
        route_type="vpn_gateway",
        route_tables=(
            {"id": "rtb-prod-b2b-a", "az": "az-a", "subnet": "subnet-prod-b2b-a"},
            {"id": "rtb-prod-b2b-b", "az": "az-b", "subnet": "subnet-prod-b2b-b"},
        ),
        from_next_hop="vpn-legacy",
        from_next_hop_type="vpn_gateway",
        from_status="DEGRADED",
        from_capacity_percent=63.0,
        to_next_hop="vpn-new",
        to_next_hop_type="vpn_gateway",
        to_status="UP",
        to_capacity_percent=32.0,
        affected_services=("partner-order", "settlement-api"),
        service_ports=(443, 5432),
        title="生产 B2B VPC 合作方 VPN 外联迁移（合成演示）",
        summary="将合作方网段 192.168.120.0/24 从 vpn-legacy 灰度迁移至 vpn-new。",
        knowledge_title="历史案例：合作方 VPN 双隧道迁移（合成）",
        knowledge_summary="迁移需与合作方联合验证白名单、接口回执和结算文件传输。",
        procedure_steps=("冻结合作方白名单", "核对新隧道路由", "切换 AZ-A 联调", "切换 AZ-B 并验证结算"),
        risks=("合作方白名单未同步会导致接口拒绝", "结算文件中断会形成业务积压"),
        rollback_triggers=("合作方接口回执异常", "结算传输连续两个周期失败"),
        communication_plan=("通知 B2B、结算和合作方联系人", "灰度后完成联合验收", "迁移完成后发送结果"),
        historical_outcome="历史合成联调完成 200 笔接口回执和 3 个结算文件传输，均成功。",
        search_query="合作方 VPN 外联 迁移 白名单 接口回执 结算 回退",
    ),
)

DEFAULT_CASE_ID = CHANGE_CASES[0].case_id
_CASE_INDEX = {item.case_id: item for item in CHANGE_CASES}


def get_change_case(case_id: str | None = None) -> ChangeCase:
    key = (case_id or DEFAULT_CASE_ID).strip()
    try:
        return _CASE_INDEX[key]
    except KeyError as exc:
        allowed = ", ".join(_CASE_INDEX)
        raise ValueError(f"未知变更案例：{key}；可选值：{allowed}") from exc


def list_change_cases() -> list[dict[str, Any]]:
    return [item.public_dict() for item in CHANGE_CASES]


def seed_case_catalog_knowledge(store: KnowledgeStore) -> list[dict[str, Any]]:
    """Idempotently publish synthetic historical cases as APPROVED fixtures."""

    seeded: list[dict[str, Any]] = []
    for case in CHANGE_CASES:
        content = (
            f"# {case.knowledge_title}\n\n"
            "> 合成历史案例：不代表任何真实云资源或生产执行记录。\n\n"
            f"{case.knowledge_summary}\n\n"
            f"执行证据：{case.historical_outcome}\n"
            f"对象：{case.region} / {case.vpc_id} / {case.destination}\n"
        )
        checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
        document_id, created = store.add_document(
            case.knowledge_title,
            "synthetic-change-case",
            f"synthetic://change-cases/{case.case_id}",
            checksum,
            content,
        )
        existing_ids = store.card_ids_for_document(document_id)
        if not created and existing_ids:
            card_id = existing_ids[0]
        else:
            chunk_id = store.add_chunk(document_id, 0, 0, len(content), content)
            card_id = store.add_card(
                KnowledgeCardDraft(
                    title=case.knowledge_title,
                    summary=case.knowledge_summary,
                    knowledge_type="case",
                    scenario=case.label,
                    object_type="cloud_network_change",
                    object_name=f"{case.vpc_id} / {case.destination}",
                    applicable_versions=["synthetic-v1"],
                    prerequisites=["仅用于合成演示，不连接或操作真实云资源"],
                    procedure_steps=list(case.procedure_steps),
                    risks=list(case.risks),
                    rollback_steps=[
                        f"按 {case.route_tables[1]['az']}、{case.route_tables[0]['az']} 逆序恢复 {case.from_next_hop}"
                    ],
                    validation_steps=[
                        f"有效下一跳为 {case.to_next_hop}",
                        "TCP 成功率不低于 99.5%",
                        "丢包不高于 1%，P95 时延不高于 30 ms",
                    ],
                    keywords=[case.category, case.label, case.vpc_id, case.destination, "合成案例"],
                    evidence_quote=case.historical_outcome,
                ),
                document_id=document_id,
                chunk_id=chunk_id,
                evidence_locator=f"synthetic://change-cases/{case.case_id}#outcome",
                status=CardStatus.PENDING_REVIEW,
                quality_score=98.0,
                quality_issues=[],
                comparison=ComparisonResult(
                    decision=ComparisonDecision.NEW,
                    confidence=1.0,
                    reason="合成历史案例基线",
                ),
            )
            store.review_card(
                card_id,
                action="APPROVE",
                reviewer="demo-fixture-reviewer",
                comment="合成历史案例夹具，预置为已人工审核知识",
            )
        card = store.get_card(card_id)
        seeded.append(
            {
                "case_id": case.case_id,
                "knowledge_card_id": card_id,
                "knowledge_status": str(card["status"]) if card else "UNKNOWN",
            }
        )
    return seeded
