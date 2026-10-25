import { defineConfig } from 'vitepress'

const REPO = 'parse_hub_bot_douyin_forward'
const base = process.env.DOCS_BASE ?? (process.env.NODE_ENV === 'production' ? `/${REPO}/` : '/')

export default defineConfig({
  lang: 'zh-CN',
  title: '抖音转发解析',
  titleTemplate: ':title | 抖音转发解析',
  description: '基于 parse_hub_bot 的抖音私信转发解析服务文档：视频、图文、实况图（Live Photo）自动接收、解析、归档与 Telegram 推送。',
  base,
  appearance: 'dark',
  lastUpdated: true,
  head: [
    ['link', { rel: 'icon', type: 'image/svg+xml', href: `${base}favicon.svg` }],
    ['meta', { name: 'theme-color', content: '#e64a83' }],
    ['meta', { property: 'og:type', content: 'website' }],
    ['meta', { property: 'og:title', content: '抖音转发解析服务' }],
    ['meta', { property: 'og:description', content: '视频、图文、实况图自动接收、解析、归档，并推送到 Telegram。' }]
  ],
  markdown: {
    lineNumbers: true
  },
  themeConfig: {
    logo: '/logo.svg',
    siteTitle: '抖音转发解析',
    nav: [
      { text: '首页', link: '/' },
      { text: '文档', link: '/guide/introduction' },
      { text: '部署', link: '/guide/deploy' },
      { text: 'GitHub', link: 'https://github.com/yss161/parse_hub_bot_douyin_forward' }
    ],
    sidebar: {
      '/guide/': [
        {
          text: '介绍',
          items: [
            { text: '项目简介', link: '/guide/introduction' },
            { text: '功能特性', link: '/guide/introduction#功能特性' },
            { text: '工作流程', link: '/guide/introduction#工作流程' }
          ]
        },
        {
          text: '快速开始',
          items: [
            { text: '环境要求', link: '/guide/getting-started#环境要求' },
            { text: '安装依赖', link: '/guide/getting-started#安装依赖' },
            { text: '登录小号', link: '/guide/getting-started#登录小号' },
            { text: '启动服务', link: '/guide/getting-started#启动服务' }
          ]
        },
        {
          text: '核心功能',
          items: [
            { text: '检测模式', link: '/guide/detection' },
            { text: '实况图（Live Photo）', link: '/guide/live-photo' },
            { text: '归档与去重', link: '/guide/archive' },
            { text: 'Telegram 推送', link: '/guide/telegram' }
          ]
        },
        {
          text: '部署运维',
          items: [
            { text: 'Ubuntu 部署', link: '/guide/deploy' },
            { text: '完整配置', link: '/guide/configuration' },
            { text: '升级与回滚', link: '/guide/upgrade' },
            { text: '故障排查', link: '/guide/troubleshooting' }
          ]
        },
        {
          text: '常见问题',
          items: [{ text: 'FAQ', link: '/guide/faq' }]
        },
        {
          text: '进阶',
          items: [{ text: '开发与测试', link: '/guide/development' }]
        }
      ]
    },
    outline: { level: [2, 3], label: '本页导航' },
    search: {
      provider: 'local',
      options: {
        translations: {
          button: { buttonText: '搜索文档', buttonAriaLabel: '搜索文档' },
          modal: {
            noResultsText: '没有找到结果',
            resetButtonTitle: '清除查询条件',
            footer: { selectText: '选择', navigateText: '切换', closeText: '关闭' }
          }
        }
      }
    },
    socialLinks: [
      { icon: 'github', link: 'https://github.com/yss161/parse_hub_bot_douyin_forward' }
    ],
    footer: {
      message: '基于 MIT 协议开源',
      copyright: 'Copyright © 2026 抖音转发解析服务'
    },
    docFooter: { prev: '上一页', next: '下一页' },
    lastUpdated: { text: '最后更新于', formatOptions: { dateStyle: 'short', timeStyle: 'short' } },
    returnToTopLabel: '回到顶部',
    sidebarMenuLabel: '菜单',
    darkModeSwitchLabel: '外观',
    lightModeSwitchTitle: '切换到浅色模式',
    darkModeSwitchTitle: '切换到深色模式',
    externalLinkIcon: true
  }
})
