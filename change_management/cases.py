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
    window_minutes: int
    region: str
    vpc_id: str
    vpc_cidr: str
    destination: str
    route_type: str
    route_tables: tuple[dict[str, str], ...]
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

    def step_id(self, index: int) -> str:
        table = self.route_tables[index]
        configured = str(table.get("step_id") or "").strip()
        if configured:
            return configured
        return f"route-switch-{table['az']}"

    def step_phase(self, index: int) -> str:
        configured = str(self.route_tables[index].get("phase") or "").strip()
        if configured:
            return configured
        return "CANARY" if index == 0 else "ROLLOUT"

    @property
    def execution_step_ids(self) -> tuple[str, ...]:
        return tuple(self.step_id(index) for index in range(len(self.route_tables)))

    def public_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["route_tables"] = [dict(item) for item in self.route_tables]
        payload["affected_services"] = list(self.affected_services)
        payload["service_ports"] = list(self.service_ports)
        payload["procedure_steps"] = list(self.procedure_steps)
        payload["risks"] = list(self.risks)
        payload["rollback_triggers"] = list(self.rollback_triggers)
        payload["communication_plan"] = list(self.communication_plan)
        payload["execution_step_count"] = len(self.route_tables)
        payload["failure_injection_points"] = [
            {
                "step_id": self.step_id(index),
                "label": f"{self.step_phase(index)} · {table['id']} ({table['az']})",
            }
            for index, table in enumerate(self.route_tables)
        ]
        return payload


def _wave_route_tables(
    prefix: str,
    waves: tuple[tuple[str, tuple[tuple[str, str], ...]], ...],
) -> tuple[dict[str, str], ...]:
    """Build readable multi-wave route fixtures with stable operation IDs."""

    tables: list[dict[str, str]] = []
    for phase, members in waves:
        for segment, az in members:
            az_suffix = az.removeprefix("az-")
            tables.append(
                {
                    "id": f"rtb-{prefix}-{segment}-{az_suffix}",
                    "az": az,
                    "subnet": f"subnet-{prefix}-{segment}-{az_suffix}",
                    "step_id": f"route-switch-{prefix}-{segment}-{az_suffix}",
                    "phase": phase,
                }
            )
    return tuple(tables)


CHANGE_CASES: tuple[ChangeCase, ...] = (
    ChangeCase(
        case_id="dc-route-failover",
        ticket_id="CHG-DEMO-ROUTE-001",
        label="专线路由主备切换",
        category="专线容灾",
        description="核心数据库专线路由从劣化主链路灰度切换到备用链路。",
        risk_level="高",
        risk_score=82,
        window_minutes=30,
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
        window_minutes=30,
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
        window_minutes=30,
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
        window_minutes=30,
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
        window_minutes=30,
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
    ChangeCase(
        case_id="transit-hub-route-domain-migration",
        ticket_id="CHG-DEMO-TGW-006",
        label="云骨干路由域分批迁移",
        category="云骨干 / TGW",
        description="12 张业务路由表按四个波次迁移到新版云骨干连接。",
        risk_level="极高",
        risk_score=91,
        window_minutes=90,
        region="cn-central-1",
        vpc_id="vpc-prod-transit-hub",
        vpc_cidr="10.100.0.0/16",
        destination="10.200.0.0/16",
        route_type="transit_gateway",
        route_tables=_wave_route_tables(
            "tgw",
            (
                ("CANARY", (("ops", "az-a"), ("ops", "az-b"))),
                (
                    "WAVE-1",
                    (("app", "az-a"), ("app", "az-b"), ("api", "az-a"), ("api", "az-b")),
                ),
                (
                    "WAVE-2",
                    (("data", "az-a"), ("data", "az-b"), ("bi", "az-a"), ("bi", "az-b")),
                ),
                ("FINAL", (("shared", "az-a"), ("shared", "az-b"))),
            ),
        ),
        from_next_hop="tgw-core-v1",
        from_next_hop_type="transit_gateway",
        from_status="DEGRADED",
        from_capacity_percent=79.0,
        to_next_hop="tgw-core-v2",
        to_next_hop_type="transit_gateway",
        to_status="UP",
        to_capacity_percent=34.0,
        affected_services=("order-platform", "data-platform", "bi-reporting", "shared-services"),
        service_ports=(443, 5432),
        title="生产云骨干 TGW 路由域四波次迁移（合成演示）",
        summary="将 12 张生产路由表中 10.200.0.0/16 的下一跳从 tgw-core-v1 分四波迁移至 tgw-core-v2。",
        knowledge_title="历史案例：云骨干 TGW 路由域分批迁移（合成）",
        knowledge_summary="大规模路由域迁移必须先冻结传播策略，再按运维、应用、数据和共享服务四波次放量。",
        procedure_steps=(
            "冻结 TGW 路由传播、静态路由和关联关系变更",
            "导出 12 张业务路由表及有效下一跳快照",
            "核对新版 TGW 连接、BGP 会话和目标前缀通告",
            "校验新旧路由域无更具体前缀冲突",
            "切换运维网 AZ-A 路由并验证堡垒机连接",
            "切换运维网 AZ-B 路由并观察一个采样周期",
            "切换应用与 API 网段四张路由表",
            "验证订单链路、服务发现和东西向调用",
            "切换数据与 BI 网段四张路由表",
            "验证数据库只读连接、任务积压和报表查询",
            "切换共享服务双 AZ 路由表",
            "执行 12 张路由表有效下一跳全量核对",
            "观察丢包、P95 时延和 TGW 容量两个周期",
            "比对变更后状态哈希与操作日志",
            "解除路由策略冻结并发送变更完成通知",
        ),
        risks=("传播路由与静态路由重叠可能形成黑洞", "跨业务域一次性迁移会放大故障半径", "数据域长连接重建可能产生任务积压"),
        rollback_triggers=("任一波次出现有效下一跳偏离", "核心业务成功率或时延连续两个周期越过阈值", "TGW 新连接容量高于60%"),
        communication_plan=("T-30 分钟通知网络、应用、数据和安全值守", "每个波次完成后发布验证结论", "最终观察结束后同步状态哈希和遗留项"),
        historical_outcome="历史合成演练完成 12 张路由表四波次迁移，最大 P95 时延 23 ms，未触发逆序回退。",
        search_query="TGW 云骨干 路由域 传播路由 四波次 大规模迁移 回退",
    ),
    ChangeCase(
        case_id="east-west-firewall-service-chain",
        ticket_id="CHG-DEMO-EWFW-007",
        label="东西向防火墙服务链插入",
        category="零信任分段",
        description="14 张路由表分四波插入新版东西向防火墙服务链。",
        risk_level="极高",
        risk_score=95,
        window_minutes=120,
        region="cn-southwest-2",
        vpc_id="vpc-prod-service-mesh",
        vpc_cidr="10.120.0.0/16",
        destination="10.160.0.0/12",
        route_type="cloud_firewall",
        route_tables=_wave_route_tables(
            "ewfw",
            (
                ("CANARY", (("shared", "az-a"), ("shared", "az-b"))),
                (
                    "WAVE-1",
                    (("web", "az-a"), ("web", "az-b"), ("api", "az-a"), ("api", "az-b")),
                ),
                (
                    "WAVE-2",
                    (("iam", "az-a"), ("iam", "az-b"), ("data", "az-a"), ("data", "az-b")),
                ),
                (
                    "WAVE-3",
                    (("ops", "az-a"), ("ops", "az-b"), ("cicd", "az-a"), ("cicd", "az-b")),
                ),
            ),
        ),
        from_next_hop="cfw-inline-v1",
        from_next_hop_type="cloud_firewall",
        from_status="DEGRADED",
        from_capacity_percent=71.0,
        to_next_hop="cfw-inline-v2",
        to_next_hop_type="cloud_firewall",
        to_status="UP",
        to_capacity_percent=37.0,
        affected_services=("api-gateway", "iam-service", "database-proxy", "cicd-runner"),
        service_ports=(443, 5432),
        title="生产东西向防火墙服务链十四步切换（合成演示）",
        summary="将 14 张生产路由表的东西向流量分四波导入 cfw-inline-v2，并逐波验证策略、会话和审计。",
        knowledge_title="历史案例：东西向防火墙服务链分批插入（合成）",
        knowledge_summary="服务链变更需锁定策略版本、完成会话预热，并以共享、前台、核心和运维域逐级放量。",
        procedure_steps=(
            "冻结安全策略、地址组和路由表并记录审批版本",
            "导出 14 张路由表、会话量和策略命中基线",
            "比对新旧防火墙策略及对象组哈希",
            "预热新版防火墙节点并验证日志投递",
            "切换共享服务 AZ-A 与 AZ-B 作为金丝雀",
            "验证 DNS、时间同步、制品库和审计链路",
            "切换 Web 与 API 域四张路由表",
            "验证用户登录、网关调用和长连接重建",
            "切换 IAM 与数据域四张路由表",
            "验证认证、数据库代理及最小权限策略",
            "切换运维与 CI/CD 域四张路由表",
            "验证堡垒机、流水线和镜像拉取",
            "全量核对 14 张路由表的有效下一跳",
            "检查策略命中、误阻断、丢包和时延两个周期",
            "归档策略哈希、执行日志和最终网络快照",
        ),
        risks=("策略差异可能导致大面积误阻断", "非对称路由会造成有状态会话丢失", "审计链路异常会形成合规证据缺口"),
        rollback_triggers=("发现非对称路径或策略哈希不一致", "关键服务成功率连续两个周期低于阈值", "防火墙会话或容量高于安全水位"),
        communication_plan=("联合网络、安全、IAM、数据和研发效能团队值守", "每个安全域切换后由业务负责人签字确认", "完成后发布误阻断与审计日志核查结论"),
        historical_outcome="历史合成演练完成 14 张路由表切换，策略哈希一致，未发现误阻断，审计日志完整。",
        search_query="东西向 防火墙 服务链 零信任 14步 策略哈希 会话 回退",
    ),
    ChangeCase(
        case_id="kubernetes-egress-pool-migration",
        ticket_id="CHG-DEMO-K8S-008",
        label="多集群容器出口池迁移",
        category="容器网络",
        description="10 张子网路由表按集群和业务域迁移至新版 NAT 出口池。",
        risk_level="高",
        risk_score=89,
        window_minutes=90,
        region="cn-east-5",
        vpc_id="vpc-prod-k8s-fleet",
        vpc_cidr="10.140.0.0/16",
        destination="0.0.0.0/0",
        route_type="nat_gateway",
        route_tables=_wave_route_tables(
            "k8s",
            (
                ("CANARY", (("observe", "az-a"), ("observe", "az-b"))),
                (
                    "WAVE-1",
                    (("catalog", "az-a"), ("catalog", "az-b"), ("checkout", "az-a"), ("checkout", "az-b")),
                ),
                (
                    "WAVE-2",
                    (("order", "az-a"), ("order", "az-b"), ("payment", "az-a"), ("payment", "az-b")),
                ),
            ),
        ),
        from_next_hop="nat-k8s-legacy",
        from_next_hop_type="nat_gateway",
        from_status="DEGRADED",
        from_capacity_percent=89.0,
        to_next_hop="nat-k8s-pool-v2",
        to_next_hop_type="nat_gateway",
        to_status="UP",
        to_capacity_percent=33.0,
        affected_services=("catalog-cluster", "checkout-cluster", "order-cluster", "payment-cluster"),
        service_ports=(443, 5432),
        title="生产 Kubernetes 多集群出口池十步迁移（合成演示）",
        summary="将 10 张容器子网默认路由按观测、非资金和资金业务三波迁移至 nat-k8s-pool-v2。",
        knowledge_title="历史案例：Kubernetes 多集群 NAT 出口池迁移（合成）",
        knowledge_summary="多集群出口迁移需核对白名单、SNAT 端口容量、连接追踪和第三方回调，再按业务等级分波放量。",
        procedure_steps=(
            "冻结集群节点池扩缩容和出口白名单变更",
            "导出 10 张子网路由表与 NAT 会话基线",
            "确认新版 NAT 地址池已加入全部第三方白名单",
            "验证 SNAT 端口、连接追踪和日志容量",
            "切换观测集群双 AZ 默认路由",
            "验证日志、指标和镜像仓库访问",
            "切换商品与结算前台四张路由表",
            "验证公网 API、Webhook 和依赖下载",
            "切换订单与支付四张路由表",
            "验证支付回调、风控和消息投递",
            "核对十张路由表及出口公网地址",
            "观察 SNAT 端口利用率和失败连接两个周期",
            "解除冻结并归档迁移证据",
        ),
        risks=("第三方白名单遗漏会导致回调失败", "SNAT 端口耗尽会造成间歇性连接失败", "长连接重建可能影响消息消费"),
        rollback_triggers=("资金类回调或风控请求失败", "SNAT 端口利用率高于60%", "连接失败率连续两个周期越过阈值"),
        communication_plan=("通知容器平台、支付、风控和外联值守", "每个集群波次同步出口 IP 与回调结果", "完成后发送 NAT 容量和连接统计"),
        historical_outcome="历史合成演练完成 10 张默认路由迁移，SNAT 峰值利用率 42%，支付回调全部成功。",
        search_query="Kubernetes 多集群 NAT 出口池 SNAT 白名单 十步迁移 回退",
    ),
    ChangeCase(
        case_id="dns-resolver-endpoint-migration",
        ticket_id="CHG-DEMO-DNS-009",
        label="混合云 DNS 出站端点迁移",
        category="DNS / 混合云",
        description="6 张业务路由表迁移到新版 DNS 出站解析端点。",
        risk_level="高",
        risk_score=84,
        window_minutes=60,
        region="cn-northwest-1",
        vpc_id="vpc-prod-dns-hub",
        vpc_cidr="10.180.0.0/16",
        destination="10.250.53.0/24",
        route_type="resolver_endpoint",
        route_tables=_wave_route_tables(
            "dns",
            (
                ("CANARY", (("shared", "az-a"), ("shared", "az-b"))),
                ("WAVE-1", (("app", "az-a"), ("app", "az-b"))),
                ("FINAL", (("data", "az-a"), ("data", "az-b"))),
            ),
        ),
        from_next_hop="dns-outbound-v1",
        from_next_hop_type="resolver_endpoint",
        from_status="DEGRADED",
        from_capacity_percent=68.0,
        to_next_hop="dns-outbound-v2",
        to_next_hop_type="resolver_endpoint",
        to_status="UP",
        to_capacity_percent=29.0,
        affected_services=("service-discovery", "database-client", "ad-authentication"),
        service_ports=(443, 5432),
        title="生产混合云 DNS 出站端点六步迁移（合成演示）",
        summary="将共享、应用和数据域 6 张路由表中的企业 DNS 网段迁移至 dns-outbound-v2。",
        knowledge_title="历史案例：混合云 DNS 出站解析端点迁移（合成）",
        knowledge_summary="DNS 迁移需提前降低 TTL，核对条件转发规则，并逐域验证解析正确率和缓存行为。",
        procedure_steps=("提前降低关键域名 TTL", "冻结条件转发规则", "导出查询成功率与缓存基线", "切换共享服务双 AZ", "验证 AD 与服务发现", "切换应用域双 AZ", "验证内部 API 解析", "切换数据域双 AZ", "观察 SERVFAIL、NXDOMAIN 和时延", "恢复 TTL 并归档证据"),
        risks=("条件转发遗漏会导致局部解析失败", "负缓存可能延长故障影响", "AD 域名解析异常会影响认证"),
        rollback_triggers=("关键域名解析结果不一致", "SERVFAIL 比例连续两个周期越过阈值", "AD 或数据库连接出现解析错误"),
        communication_plan=("通知 DNS、AD、应用和数据库值守", "每个业务域切换后发布解析抽样结果", "完成后同步 TTL 恢复时间"),
        historical_outcome="历史合成迁移完成 6 张路由表切换，关键域名解析正确率 100%，无 SERVFAIL 增量。",
        search_query="混合云 DNS 出站端点 条件转发 TTL SERVFAIL 分批迁移 回退",
    ),
    ChangeCase(
        case_id="private-endpoint-service-cutover",
        ticket_id="CHG-DEMO-PES-010",
        label="私网终端节点服务切换",
        category="PrivateLink",
        description="批处理与分析域四张路由表切换到新版私网终端节点服务。",
        risk_level="高",
        risk_score=80,
        window_minutes=45,
        region="cn-south-4",
        vpc_id="vpc-prod-analytics",
        vpc_cidr="10.210.0.0/16",
        destination="10.254.80.0/24",
        route_type="private_endpoint",
        route_tables=_wave_route_tables(
            "pes",
            (
                ("CANARY", (("batch", "az-a"), ("batch", "az-b"))),
                ("ROLLOUT", (("analytics", "az-a"), ("analytics", "az-b"))),
            ),
        ),
        from_next_hop="endpoint-service-legacy",
        from_next_hop_type="private_endpoint",
        from_status="DEGRADED",
        from_capacity_percent=62.0,
        to_next_hop="endpoint-service-v2",
        to_next_hop_type="private_endpoint",
        to_status="UP",
        to_capacity_percent=26.0,
        affected_services=("etl-runner", "analytics-api", "object-storage-proxy"),
        service_ports=(443, 5432),
        title="生产私网终端节点服务四步切换（合成演示）",
        summary="将批处理和分析域 4 张路由表迁移至 endpoint-service-v2，验证私网解析和对象访问。",
        knowledge_title="历史案例：私网终端节点服务蓝绿切换（合成）",
        knowledge_summary="PrivateLink 切换需同时核对服务接受状态、私网 DNS、后端健康和跨 AZ 流量。",
        procedure_steps=("冻结终端节点连接审批", "核对新服务后端健康", "验证私网 DNS 返回", "切换批处理双 AZ", "验证 ETL 与对象访问", "切换分析域双 AZ", "核对跨 AZ 流量与时延", "归档连接与路由快照"),
        risks=("终端节点连接未接受会导致黑洞", "私网 DNS 缓存可能仍指向旧服务", "跨 AZ 流量可能增加成本与时延"),
        rollback_triggers=("终端节点连接状态异常", "对象访问成功率低于阈值", "跨 AZ 流量或时延连续两个周期升高"),
        communication_plan=("通知数据平台、存储和网络值守", "金丝雀完成后确认 ETL 作业", "全量完成后同步私网访问指标"),
        historical_outcome="历史合成切换完成 4 张路由表迁移，ETL 作业和对象访问全部成功，未产生跨 AZ 异常。",
        search_query="PrivateLink 私网终端节点 蓝绿切换 私网DNS 对象存储 回退",
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
                        f"按执行日志逆序恢复 {len(case.route_tables)} 张路由表至 {case.from_next_hop}"
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
