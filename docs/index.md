---
layout: home

hero:
  name: 抖音转发解析
  text: 视频 · 图文 · 实况图
  tagline: 基于 parse_hub_bot 的抖音私信转发解析服务；自动接收、解析、归档，并推送到 Telegram。
  image:
    src: /logo.svg
    alt: 抖音转发解析
  actions:
    - theme: brand
      text: 立即了解
      link: /guide/introduction
    - theme: alt
      text: 快速开始
      link: /guide/getting-started
    - theme: alt
      text: GitHub
      link: https://github.com/yss161/parse_hub_bot_douyin_forward

features:
  - icon: 📥
    title: 私信自动接收
    details: 大号转发 → 小号私信 → 自动发现；支持多会话、多账号监测。
  - icon: ⚡
    title: 双检测模式
    details: reload 最稳；probe 用 DOM 探针 + 定期取数，请求量约降 95%。
  - icon: 🎞️
    title: 实况图支持
    details: 识别 LivePhotoFile，按 video / live / both 三种模式推送。
  - icon: 🗂️
    title: 归档与去重
    details: 原始媒体 + metadata.json；消息/链接/视频 ID 三层去重。
  - icon: 📨
    title: Telegram 推送
    details: 视频、图文相册、GIF、Live Photo；失败自动降级文档。
  - icon: 🛠️
    title: 部署友好
    details: 一键 Ubuntu 脚本 + systemd + 交互式配置。
---

## 快速导航

- [项目简介](/guide/introduction) — 了解整体架构与工作流程
- [快速开始](/guide/getting-started) — 10 分钟跑起来
- [检测模式](/guide/detection) — reload 与 probe 怎么选
- [实况图](/guide/live-photo) — Live Photo 三种模式与限制
- [部署到 Ubuntu](/guide/deploy) — 服务器一键部署
- [常见问题](/guide/faq) — 先看这里
