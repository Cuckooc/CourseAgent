# -*- coding: utf-8 -*-
"""
模块：version.py
作用：统一管理项目版本信息与元数据（单一来源），供应用入口、API 文档与监控标签引用。
主要成员：
- __version__ / __version_info__：语义化版本号；
- PROJECT_NAME / REPO_URL：项目元数据。
被谁使用：control/app.py（FastAPI 实例 version 参数）。
"""

__version__ = "2.0.0"
__version_info__ = (2, 0, 0)

# 项目元数据
PROJECT_NAME = "CourseAgent 智能课程咨询服务"
REPO_URL = "https://github.com/Cuckooc/CourseAgent"
