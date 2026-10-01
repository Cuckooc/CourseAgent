# Config 静态配置目录

本目录存放随代码发布的静态配置：全局参数设置与敏感词表。**环境变量类配置不在此处**，见 [env/README.md](../env/README.md)。

## 📁 目录结构

```
config/
├── setting.py        # 全局静态参数（与 core/config.py 环境变量配置互补）
└── banned_words.txt  # 敏感词表（每行一个词，供 core/content_filter.py 加载）
```

## 📄 文件说明

### `setting.py` - 静态参数

代码内固化的全局参数（与 `core/config.py` 的环境变量配置互补：环境变量可覆盖部署差异，此处存放不随部署变化的业务参数）。

### `banned_words.txt` - 敏感词表

由 `core/content_filter.py` 惰性加载并进程内缓存一次，用于输入/输出的内容安全过滤。新增敏感词直接追加一行即可，无需改代码。
