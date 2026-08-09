# Web 与资源安全基线

本项目默认按“受控内网、共享管理员令牌”部署。共享令牌只能证明请求持有平台密钥，不能区分真实个人；所有 Web 审计主体固定为 `shared-operator`，并允许演示自审批。需要个人身份或职责分离时，应升级到 OIDC/RBAC，而不是在请求体中填写姓名。

## 信任边界

- Python 服务必须保持 `PLATFORM_HOST=127.0.0.1`。
- 内网访问必须经过 HTTPS 反向代理，不得直接开放 8765。
- 除静态资源和 `/api/health/live` 外，所有 API 都要求 `Authorization: Bearer <token>`。
- `Host` 与浏览器 `Origin` 必须分别匹配配置白名单。
- JSON 写接口拒绝 `text/plain`；上传只接受 `multipart/form-data`。
- 共享令牌只保存在浏览器 `sessionStorage`，关闭标签页或点击“退出”即清除。

生成令牌：

```text
python run.py generate-access-token
```

命令会输出一次明文令牌和一行 `PLATFORM_ACCESS_TOKEN_HASH=...`。只把哈希写入 `.env`；明文通过受保护渠道交给操作者，不要写入 Git、日志、命令历史或网页 URL。

## 费用与解析限制

默认每份文档最多 120,000 字符、20 个分片、60 次模型调用和 30 张候选卡片；同时只处理一个知识导入。相同 checksum 在首次模型调用前通过 SQLite 原子声明，重复并发请求不会重复产生费用。

上传文件默认最大 10 MiB。DOCX 会限制 ZIP 条目、压缩比、解压后大小和 XML 大小，并拒绝 DTD/实体；PDF 限制总页数和 OCR 页数；图片限制像素。DOCX、PDF 和图片在独立工作进程中解析，超时后终止。

## 变更会话

默认最多 3 个活动会话和 20 个保留会话。活动会话最长 2 小时，终态会话保留 24 小时；终态后立即停止专属 Worker，过期或被淘汰时只删除 `artifacts/change_demos` 下经过路径校验的对应工作区。

## 健康检查

- `/api/health/live`：无鉴权，只返回最小存活状态，供 systemd/代理探针使用。
- `/api/health`：要求 Bearer 令牌，返回配置能力和运行时详情。

## 安全响应

常见状态码：`401` 未鉴权、`403/421` 来源或 Host 非法、`413` 资源超限、`415` Content-Type 错误、`409` 相同文档处理中、`429` 速率/并发/会话额度耗尽。
