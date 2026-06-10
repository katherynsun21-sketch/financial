# 财经热榜数据抓取

定时抓取 [今日热榜 - 财经分类](https://tophub.today/c/finance) 页面的多个财经榜单，维护每日 JSON 数据档案和 `latest.json` 最新数据索引。

## 数据文件

| 文件路径 | 说明 |
|---------|------|
| `data/latest.json` | **最新抓取数据**，便于程序直接引用 |
| `data/2026-06-10.json` | 按日期归档的历史数据，每天一份 |

## 本地运行

```bash
pip install -r requirements.txt
python fetch_finance.py
```

## GitHub Actions 自动部署

- 每 2 小时自动运行一次
- 抓取结果会自动 commit 回仓库
- 在 Actions 页面可以手动触发

## 配置步骤

1. 推送代码到 GitHub 公开仓库
2. 进入 Settings → Actions → General → Workflow permissions
3. 勾选 "Read and write permissions" → Save
4. 在 Actions 页面手动 Run workflow 验证一次

## 免责声明

本项目仅用于学习和研究目的。请遵守目标网站的使用条款和 robots.txt。
