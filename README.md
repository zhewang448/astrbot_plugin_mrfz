# 明日方舟角色语音插件

这是一个高级的自动化的明日方舟所有角色语音的 AstrBot 插件。

## 访问统计
## <a href="https://count.getloli.com/"><img src="https://count.getloli.com/get/@:astrbot_plugin_mrfz?theme=rule34"></a>

## 功能特点

- 支持自动下载角色语音
- 支持中文和日语双语音
- 支持随机播放语音
- 智能语音文件管理
- 配置通过 Schema 系统管理

## 命令列表

1. `/mrfz [角色名] [语音名] [jp/cn]`
   - 播放指定角色的语音
   - 不指定语音名则随机播放
   - 语言参数可选，默认值可在配置中设置

2. `/mrfz_list`
   - 显示所有可用的语音类型
   - 显示已下载角色的列表（包括语言信息）

3. `/mrfz_fetch [角色名]`
   - 从网络获取并下载指定角色的全部语音

## 配置说明

插件使用 AstrBot 的配置系统，可在 `_conf_schema.json` 中查看配置项：

- `auto_download`: 是否自动下载未找到的角色语音（默认：true）
- `default_language`: 默认语音语言（默认：jp）

## 目录结构

```
astrbot_plugin_mrfz/
├── main.py            # 主程序
├── _conf_schema.json  # 配置模式
└── voices/           # 语音文件目录
    └── [角色名]/
        ├── jp/      # 日语语音
        └── cn/      # 中文语音
```

## 使用示例

1. 播放语音：
   ```
   /mrfz 阿 任命助理 jp
   /mrfz 阿米娅 交谈1
   /mrfz 凯尔希    # 随机播放
   ```

2. 查看语音列表：
   ```
   /mrfz_list
   ```

3. 下载角色语音：
   ```
   /mrfz_fetch 阿米娅
   ```

## 注意事项

1. 语音文件来自 PRTS Wiki，请遵守相关使用规则 
2. 下载过程中请保持网络连接稳定
3. 语音文件会占用一定磁盘空间，请确保有足够存储空间
   若想删除可以在AstrBot\data\plugin_data\astrbot_plugin_mrfz\voices目录中手动删除，并在之后重载插件   

## ⭐ Stars

> [!TIP] 
> 如果本项目对您的生活 / 工作产生了帮助，或者您关注本项目的未来发展，请给项目 Star，这是我维护这个开源项目的动力 <3>
