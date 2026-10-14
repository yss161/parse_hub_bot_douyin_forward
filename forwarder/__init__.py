# -*- coding: utf-8 -*-
"""
转发链路（forwarder）—— 抖音大号 → 小号 → 服务器解析 的接收与桥接实现。

本包不修改 parse_hub_bot 原项目的任何代码：
  - 解析部分通过 `parser_bridge` 直接调用原项目的 services.ParseService / services.ParsePipeline；
  - 本包仅实现"接收端"（抖音网页版小号收私信）与"归档端"（把解析结果落盘）。
"""
