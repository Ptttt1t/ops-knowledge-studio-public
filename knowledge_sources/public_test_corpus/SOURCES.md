# 公开测试语料来源

抓取日期：2026-07-17（Asia/Shanghai）

本语料集用于本地知识工程功能测试。公开仓库仅保留规模受控的 `prepared/` 导入版本，不分发下载原件。使用和再分发时应继续遵守各原始站点的条款并保留来源。

| 导入文件 | 格式 | 公开来源 | 导入前处理 | SHA-256 |
|---|---|---|---|---|
| `01_kubernetes_update_intro.md` | Markdown | [Kubernetes Website：Rolling Update 教程](https://github.com/kubernetes/website/blob/main/content/en/docs/tutorials/kubernetes-basics/update/update-intro.md) | 原文导入 | `B9E4062A5848611AA189EDF6C5AD75C0297C9F02636192C46542094342CAED48` |
| `02_ingress_nginx_deployment.yaml` | YAML | [ingress-nginx 示例 Deployment](https://github.com/kubernetes/ingress-nginx/blob/main/docs/examples/chashsubset/deployment.yaml) | 原文导入 | `65F36D759797459B271A66A5E10BC038659670E0569F4A55852EA9943A01874D` |
| `03_github_recent_incidents.json` | JSON | [GitHub Status API](https://www.githubstatus.com/api/v2/incidents.json) | 保留抓取时最前面的 5 个事件 | `40B02221FD3DBC9D352241E7444849AD5E7182BD38C6AD1EE477BCB58A38802C` |
| `04_cisa_kev_latest_sample.csv` | CSV | [CISA Known Exploited Vulnerabilities Catalog](https://www.cisa.gov/known-exploited-vulnerabilities-catalog) | 保留表头和前 12 条记录 | `206E877D4DBA412E6F2663EFF623F753FF5E89811E28597A90B7B9B12D2D1025` |
| `05_rfc9110_operations_excerpt.txt` | TXT | [RFC 9110: HTTP Semantics](https://www.rfc-editor.org/rfc/rfc9110.html) | 提取幂等方法及部分 4xx/5xx 段落 | `5727713220C531A3312454356D0246E7A77B2D049E551D809AC36893A8BF7B4F` |
| `06_dfe_cyber_response_plan.docx` | DOCX | [UK Department for Education：Cyber response plan template](https://cyber-security-hub.education.gov.uk/cyber-response-plan-template) | 原文件导入 | `647CC2C63912CEB1790EACB72C7E3C851C41104948735F17F80A5248CAA8584A` |

## 文件规模

| 文件 | 字节数 |
|---|---:|
| `01_kubernetes_update_intro.md` | 6,960 |
| `02_ingress_nginx_deployment.yaml` | 1,695 |
| `03_github_recent_incidents.json` | 47,659 |
| `04_cisa_kev_latest_sample.csv` | 15,389 |
| `05_rfc9110_operations_excerpt.txt` | 15,684 |
| `06_dfe_cyber_response_plan.docx` | 160,014 |
