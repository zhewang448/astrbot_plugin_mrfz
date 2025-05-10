# 明日方舟角色语音插件 astrbot_plugin_mrfz

v3.0.0
这是一个高级的自动化的明日方舟所有角色语音的 AstrBot 插件。

## 访问统计

## <a href="https://count.getloli.com/"><img src="https://count.getloli.com/get/@:astrbot_plugin_mrfz?theme=rule34"></a>

# 注意！

## 若你用过 v1.5.0 及之前的版本 ，请在更新到 v1.6.0 后，在"AstrBot\data\config"中删除 astrbot_plugin_mrfz_config.json 文件，否则会导致配置显示异常。

## 功能特点

- 支持自动下载角色语音
- 支持中文 日语 方言 三语音
- 支持随机播放语音
- 智能语音文件管理
- 配置通过 Schema 系统管理

## 指令说明

- `/mrfz [角色名] [语音名] [jp/cn/fy]` 随机或指定播放角色语音（空的值填 0）
- `/mrfz_list` 查看所有可用语音和已下载角色
- `/mrfz_fetch [角色名]` 下载指定角色及其皮肤的全部语音

## 配置项

- `auto_download`: 是否自动下载未找到的角色语音（默认：true）
- `auto_download_skin`: 是否自动下载角色的皮肤语音（默认：true）
- `default_language_rank`: 语言优先级设置（如 123，1:方言，2:中文，3:日语）

## 目录结构

```
voices/
  干员名/
    jp/
    cn/
    fy/
    skin/
      jp/
      cn/
      fy/
```

## 更新日志

明日方舟语音插件 - 用于获取和播放明日方舟角色语音的 AstrBot 插件

## 许可证

MIT

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

更新日志:

### v3.0.0
- 优化 mrfz_list 的图片，更加清晰美观
- 增加获取干员头像的功能
- 添加对物理文件但未索引情况的处理，自动重新扫描
- 增强日志输出，便于问题排查
- 优化文件下载逻辑，避免重复下载已存在的文件

### v2.0.0

- 优化皮肤 ID 自动识别，支持所有皮肤 ID 格式
- 皮肤语音与普通语音分目录存储
- 下载统计更准确，支持失败数统计
- 新增`auto_download_skin`配置项
- 代码结构优化，提升健壮性

### v1.6.0 及更早

- 支持明日方舟干员语音下载与播放
- 支持多语言
- 支持自动下载

## 注意事项

1. 语音文件来自 PRTS Wiki，请遵守相关使用规则
2. 下载过程中请保持网络连接稳定
3. 语音文件会占用一定磁盘空间，请确保有足够存储空间
   若想删除可以在 AstrBot\data\plugin_data\astrbot_plugin_mrfz\voices 目录中手动删除，并在之后重载插件

## ⭐ Stars

> [!TIP]
> 如果本项目对您的生活 / 工作产生了帮助，或者您关注本项目的未来发展，请给项目 Star，这是我维护这个开源项目的动力 <3>
